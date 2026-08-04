from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any


def provider_kind(model: Any, providers: Any) -> str:
    model_name = str(model or "").lower()
    provider_names = " ".join(str(item).lower() for item in (providers or []))
    if "grok" in model_name or "xai" in provider_names or "x.ai" in provider_names:
        return "grok"
    if "openai-codex" in provider_names or "codex" in provider_names:
        return "codex"
    return "other"


class HermesUsageData:
    def __init__(self, hermes_home: Path, quota_worker: Path) -> None:
        self.hermes_home = hermes_home
        self.db_path = hermes_home / "state.db"
        self.quota_worker = quota_worker

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
        try:
            with self._connect() as connection:
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
        try:
            with self._connect() as connection:
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
        except sqlite3.Error:
            return {"available": False, "models": [], "error": "Hermes model history is unavailable."}

        grouped: dict[str, dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            model = str(row.get("model") or "unknown")
            entry = grouped.setdefault(
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

    def fetch_quota(self) -> dict[str, Any]:
        python_exe = self.hermes_home / "hermes-agent" / "venv" / "Scripts" / "python.exe"
        if not python_exe.exists() or not self.quota_worker.exists():
            return {"available": False, "limits": [], "error": "Codex quota collector is unavailable."}
        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(self.hermes_home)
        try:
            completed = subprocess.run(
                [str(python_exe), str(self.quota_worker)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=environment,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("quota worker failed")
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            return payload if isinstance(payload, dict) else {"available": False, "limits": []}
        except Exception:
            return {"available": False, "limits": [], "error": "Codex quota is temporarily unavailable."}
