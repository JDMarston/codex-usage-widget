from __future__ import annotations

import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

PROVIDER_LABELS = {"codex": "Codex", "grok": "Grok", "gemini": "Gemini"}
PROVIDER_ORDER = tuple(PROVIDER_LABELS)
PROVIDER_ENV_KEYS = {"gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY")}
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _active_dotenv_keys(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return set()
    active: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not _ENV_ASSIGNMENT.fullmatch(key):
            continue
        if not value or value in {'""', "''"} or value.startswith("#"):
            continue
        active.add(key)
    return active


def _active_auth_provider_names(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    if not isinstance(payload, dict):
        return set()
    providers = payload.get("providers")
    if isinstance(providers, dict):
        return {str(name).strip().lower() for name in providers if str(name).strip()}
    if isinstance(providers, list):
        names: set[str] = set()
        for item in providers:
            if isinstance(item, str) and item.strip():
                names.add(item.strip().lower())
            elif isinstance(item, dict):
                name = item.get("provider") or item.get("name") or item.get("id")
                if name:
                    names.add(str(name).strip().lower())
        return names
    return set()


def configured_provider_ids(hermes_home: Path) -> list[str]:
    """Return quota providers that Hermes can currently authenticate.

    Historical model rows intentionally do not participate. A removed
    credential must stop its collector even though token history remains.
    """
    configured: set[str] = set()
    for name in _active_auth_provider_names(hermes_home / "auth.json"):
        kind = provider_kind("", [name])
        if kind in PROVIDER_LABELS:
            configured.add(kind)

    # Read the profile's files, not this widget process's inherited
    # environment. A long-running parent can retain a key after Hermes has
    # removed it on disk, which would resurrect the retired collector.
    env_keys = _active_dotenv_keys(hermes_home / ".env")
    for provider, keys in PROVIDER_ENV_KEYS.items():
        if any(key in env_keys for key in keys):
            configured.add(provider)
    return [provider for provider in PROVIDER_ORDER if provider in configured]


def provider_kind(model: Any, providers: Any) -> str:
    model_name = str(model or "").lower()
    provider_names = " ".join(str(item).lower() for item in (providers or []))
    if "grok" in model_name or "xai" in provider_names or "x.ai" in provider_names:
        return "grok"
    if "gemini" in model_name or "gemini" in provider_names or "google" in provider_names:
        return "gemini"
    if "openai-codex" in provider_names or "codex" in provider_names:
        return "codex"
    return "other"


def detected_provider_ids(session: dict[str, Any] | None, models: dict[str, Any] | None) -> list[str]:
    detected: set[str] = set()
    items = [session or {}, *((models or {}).get("models") or [])]
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("provider_kind") or "").strip().lower()
        if not kind or kind == "other":
            kind = provider_kind(item.get("model"), item.get("providers"))
        if kind in PROVIDER_LABELS:
            detected.add(kind)
    return [provider for provider in PROVIDER_ORDER if provider in detected]


def provider_model_for(
    provider: str,
    session: dict[str, Any] | None,
    models: dict[str, Any] | None,
) -> str:
    active = session or {}
    active_kind = str(active.get("provider_kind") or "").strip().lower()
    if not active_kind or active_kind == "other":
        active_kind = provider_kind(active.get("model"), active.get("providers"))
    if active_kind == provider and active.get("model"):
        return str(active["model"])

    candidates = []
    for item in (models or {}).get("models") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("provider_kind") or "").strip().lower()
        if not kind or kind == "other":
            kind = provider_kind(item.get("model"), item.get("providers"))
        if kind == provider and item.get("model"):
            candidates.append(item)
    if not candidates:
        return ""
    latest = max(candidates, key=lambda item: float(item.get("last_seen") or 0))
    return str(latest.get("model") or "")


def provider_context_key(
    session: dict[str, Any] | None,
    models: dict[str, Any] | None,
    providers: list[str] | tuple[str, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    provider_ids = detected_provider_ids(session, models) if providers is None else providers
    return tuple(
        (provider, provider_model_for(provider, session, models))
        for provider in provider_ids
        if provider in PROVIDER_LABELS
    )


class HermesUsageData:
    def __init__(self, hermes_home: Path, quota_worker: Path) -> None:
        self.hermes_home = hermes_home
        self.db_path = hermes_home / "state.db"
        self._processes: set[subprocess.Popen] = set()
        self._process_lock = threading.Lock()
        self._closed = threading.Event()
        self.quota_worker = quota_worker
        self.grok_quota_worker = quota_worker.with_name("grok_quota_worker.py")
        self.gemini_quota_worker = quota_worker.with_name("gemini_quota_worker.py")

    def configured_provider_ids(self) -> list[str]:
        return configured_provider_ids(self.hermes_home)

    def provider_context_key(
        self,
        session: dict[str, Any] | None,
        models: dict[str, Any] | None,
    ) -> tuple[tuple[str, str], ...]:
        return provider_context_key(session, models, self.configured_provider_ids())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.db_path.as_posix()}?mode=ro",
            uri=True,
            timeout=3.0,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def fetch_session(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"available": False, "error": "Hermes session data is unavailable."}
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            row = connection.execute(
                """
                SELECT model,
                       billing_provider,
                       input_tokens,
                       output_tokens,
                       cache_read_tokens,
                       cache_write_tokens,
                       reasoning_tokens,
                       api_call_count,
                       last_activity_at
                  FROM sessions
                 WHERE model IS NOT NULL AND model != ''
                 ORDER BY COALESCE(last_activity_at, started_at) DESC
                 LIMIT 1
                """
            ).fetchone()
        except sqlite3.Error:
            return {"available": False, "error": "Hermes session data is unavailable."}
        finally:
            if connection is not None:
                connection.close()
        if row is None:
            return {"available": False, "error": "No Hermes session has been recorded yet."}
        item = dict(row)
        input_tokens = int(item.get("input_tokens") or 0)
        output_tokens = int(item.get("output_tokens") or 0)
        cache_read = int(item.get("cache_read_tokens") or 0)
        cache_write = int(item.get("cache_write_tokens") or 0)
        provider = str(item.get("billing_provider") or "")
        model = str(item.get("model") or "unknown")
        return {
            "available": True,
            "model": model,
            "providers": [provider] if provider else [],
            "provider_kind": provider_kind(model, [provider]),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "reasoning_tokens": int(item.get("reasoning_tokens") or 0),
            "total_tokens": input_tokens + output_tokens + cache_read + cache_write,
            "api_calls": int(item.get("api_call_count") or 0),
            "last_activity_at": item.get("last_activity_at"),
        }

    def fetch_models(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"available": False, "models": [], "error": "Hermes model history is unavailable."}
        connection: sqlite3.Connection | None = None
        active_session: sqlite3.Row | None = None
        try:
            connection = self._connect()
            rows = connection.execute(
                """
                SELECT model,
                       billing_provider,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cache_read_tokens) AS cache_read_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens,
                       SUM(reasoning_tokens) AS reasoning_tokens,
                       SUM(api_call_count) AS api_calls,
                       MAX(last_seen) AS last_seen
                  FROM session_model_usage
                 WHERE model IS NOT NULL AND model != ''
                 GROUP BY model, billing_provider
                """
            ).fetchall()
            try:
                active_session = connection.execute(
                    """
                    SELECT model,
                           billing_provider,
                           COALESCE(last_activity_at, started_at) AS last_seen
                      FROM sessions
                     WHERE model IS NOT NULL AND model != ''
                     ORDER BY COALESCE(last_activity_at, started_at) DESC
                     LIMIT 1
                    """
                ).fetchone()
            except sqlite3.Error:
                # Older Hermes databases can still provide historical model
                # usage even when their session table lacks a newer column.
                active_session = None
        except sqlite3.Error:
            return {"available": False, "models": [], "error": "Hermes model history is unavailable."}
        finally:
            if connection is not None:
                connection.close()

        grouped: dict[str, dict[str, Any]] = {}

        def entry_for(model: str) -> dict[str, Any]:
            return grouped.setdefault(
                model,
                {
                    "model": model,
                    "providers": set(),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "api_calls": 0,
                    "last_seen": None,
                },
            )

        for raw in rows:
            row = dict(raw)
            model = str(row.get("model") or "unknown")
            entry = entry_for(model)
            provider = str(row.get("billing_provider") or "").strip()
            if provider:
                entry["providers"].add(provider)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "api_calls",
            ):
                entry[key] += int(row.get(key) or 0)
            seen = row.get("last_seen")
            if seen is not None:
                entry["last_seen"] = max(float(seen), float(entry["last_seen"] or 0))

        if active_session is not None:
            active = dict(active_session)
            model = str(active.get("model") or "unknown")
            entry = entry_for(model)
            provider = str(active.get("billing_provider") or "").strip()
            if provider:
                entry["providers"].add(provider)
            seen = active.get("last_seen")
            if seen is not None:
                entry["last_seen"] = max(float(seen), float(entry["last_seen"] or 0))

        models: list[dict[str, Any]] = []
        provider_totals: dict[str, int] = {"codex": 0, "grok": 0, "other": 0}
        for entry in grouped.values():
            providers = sorted(entry.pop("providers"))
            cache_tokens = entry["cache_read_tokens"] + entry["cache_write_tokens"]
            total_tokens = entry["input_tokens"] + entry["output_tokens"] + cache_tokens
            kind = provider_kind(entry["model"], providers)
            entry.update(
                {
                    "providers": providers,
                    "provider_kind": kind,
                    "cache_tokens": cache_tokens,
                    "total_tokens": total_tokens,
                }
            )
            provider_totals[kind] = provider_totals.get(kind, 0) + total_tokens
            models.append(entry)
        models.sort(key=lambda item: int(item.get("total_tokens") or 0), reverse=True)
        return {
            "available": bool(models),
            "models": models,
            "overall_tokens": sum(int(item.get("total_tokens") or 0) for item in models),
            "provider_totals": provider_totals,
            "generated_at": time.time(),
        }

    def _run_worker(self, worker: Path, *args: str, timeout: float = 35) -> dict[str, Any]:
        python_exe = self.hermes_home / "hermes-agent" / "venv" / "Scripts" / "python.exe"
        if not python_exe.exists() or not worker.exists() or self._closed.is_set():
            return {"status": "error", "error": "Quota collector is unavailable."}
        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(self.hermes_home)
        process = None
        try:
            with self._process_lock:
                if self._closed.is_set():
                    return {"status": "error", "error": "Collector stopped."}
                process = subprocess.Popen(
                    [str(python_exe), str(worker), *args], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=environment,
                )
                self._processes.add(process)
            output, _ = process.communicate(timeout=timeout)
            if process.returncode or len(output) > 1_048_576:
                raise ValueError("invalid worker output")
            payload = json.loads(output.strip().splitlines()[-1])
            if not isinstance(payload, dict):
                raise ValueError("invalid snapshot")
            return payload
        except Exception:
            if process and process.poll() is None:
                self._stop_process(process)
            return {"status": "error", "error": "Quota refresh failed. Retrying automatically."}
        finally:
            if process:
                with self._process_lock:
                    self._processes.discard(process)

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            # Only our exact child tree; Hermes itself is never targeted.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    def close(self) -> None:
        self._closed.set()
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            try:
                self._stop_process(process)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def fetch_quota(self) -> dict[str, Any]:
        return self._run_worker(self.quota_worker)

    def fetch_grok_quota(self) -> dict[str, Any]:
        return self._run_worker(self.grok_quota_worker)

    def fetch_gemini_quota(self, model: str) -> dict[str, Any]:
        if not model:
            return {"status": "error", "error": "No Gemini model recorded yet."}
        return self._run_worker(self.gemini_quota_worker, "--model", model, timeout=75)

    def fetch_provider_quotas(self, session, models, *, on_provider=None) -> dict[str, Any]:
        configured = self.configured_provider_ids()
        collectors = {
            "codex": self.fetch_quota,
            "grok": self.fetch_grok_quota,
            "gemini": lambda: self.fetch_gemini_quota(provider_model_for("gemini", session, models)),
        }
        results = {}
        # Short-lived parallel work: a slow provider cannot hold the other cards.
        if configured:
            with ThreadPoolExecutor(max_workers=len(configured), thread_name_prefix="quota") as executor:
                pending = {executor.submit(collectors[provider]): provider for provider in configured}
                for future in as_completed(pending):
                    provider = pending[future]
                    try:
                        payload = future.result()
                    except Exception:
                        payload = {"status": "error", "error": "Quota refresh failed."}
                    item = normalize_provider(provider, payload)
                    results[provider] = item
                    if on_provider and not self._closed.is_set():
                        on_provider(item)
        providers = [results[provider] for provider in configured if provider in results]
        return {"available": any(item["available"] for item in providers), "providers": providers,
                "context": list(provider_context_key(session, models, configured)), "generated_at": time.time()}


def normalize_provider(provider: str, payload: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    raw = payload.get("limits")
    if not isinstance(raw, list):
        raw = [payload["limit"]] if isinstance(payload.get("limit"), dict) else []
    limits = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        used, left = item.get("used_percent"), item.get("remaining_percent")
        # Derive complementary fields only from a genuine finite percentage.
        if used is None and isinstance(left, (int, float)) and not isinstance(left, bool):
            used = 100 - left
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        limits.append({**item, "used_percent": used, "remaining_percent": 100 - used})
    status = payload.get("status") or ("ok" if limits and payload.get("available") is not False else "error")
    if status == "ok" and not limits:
        status = "error"
    if status not in {"ok", "not_reported", "auth_required", "error"}:
        status = "error"
    if status != "ok":
        limits = []
    result = {"id": provider, "label": PROVIDER_LABELS[provider], "available": bool(limits),
              "limits": limits, "status": status, "checked_at": now, "stale": False,
              "fetched_at": (payload.get("fetched_at") or payload.get("collected_at") or now)
                            if status in {"ok", "not_reported"} else None}
    for key in ("plan", "source", "error", "notice", "period_start_at", "reset_at", "period_label"):
        if payload.get(key) is not None:
            result[key] = payload[key]
    return result


def merge_provider(previous: dict | None, current: dict) -> dict:
    """Retain last good numbers on transport failure, not on unpublished usage."""
    if current.get("status") == "error" and previous and previous.get("limits"):
        return {**previous, "status": "error", "stale": True,
                "checked_at": current["checked_at"], "error": current.get("error")}
    return current
