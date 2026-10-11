from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from hermes_constants import get_hermes_home

router = APIRouter()

_QUOTA_CACHE_SECONDS = 30.0
_quota_cache: tuple[float, dict[str, Any]] | None = None
_quota_lock = threading.Lock()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _epoch(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _window_label(seconds: float | None, fallback: str) -> str:
    if seconds is None or seconds <= 0:
        return fallback
    if 4 * 3600 <= seconds <= 6 * 3600:
        return "5-hour"
    if 20 * 3600 <= seconds <= 28 * 3600:
        return "Daily"
    if 6 * 86400 <= seconds <= 8 * 86400:
        return "Weekly"
    if 27 * 86400 <= seconds <= 32 * 86400:
        return "Monthly"
    if seconds < 86400:
        return f"{max(1, round(seconds / 3600))}-hour"
    return f"{max(1, round(seconds / 86400))}-day"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "limit"


def _parse_window(
    raw: Any,
    *,
    group: str,
    kind: str,
    slot: str,
    fallback_label: str,
    allowed: bool | None = None,
    limit_reached: bool | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("used_percent"))
    if used is None:
        return None
    used = max(0.0, min(100.0, used))
    seconds = _number(raw.get("limit_window_seconds"))
    return {
        "id": f"{kind}:{_slug(group)}:{slot}",
        "kind": kind,
        "group": group,
        "label": _window_label(seconds, fallback_label),
        "used_percent": round(used, 2),
        "remaining_percent": round(max(0.0, 100.0 - used), 2),
        "reset_at": _epoch(raw.get("reset_at")),
        "window_seconds": seconds,
        "allowed": allowed,
        "limit_reached": limit_reached,
    }


def _parse_quota_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Codex usage response was not an object")

    limits: list[dict[str, Any]] = []
    account_rate = payload.get("rate_limit") or {}
    if isinstance(account_rate, dict):
        allowed = account_rate.get("allowed")
        reached = account_rate.get("limit_reached")
        for slot, fallback in (("primary_window", "Session"), ("secondary_window", "Weekly")):
            item = _parse_window(
                account_rate.get(slot),
                group="Codex account",
                kind="account",
                slot=slot,
                fallback_label=fallback,
                allowed=allowed if isinstance(allowed, bool) else None,
                limit_reached=reached if isinstance(reached, bool) else None,
            )
            if item:
                limits.append(item)

    additional = payload.get("additional_rate_limits") or []
    if isinstance(additional, list):
        for index, entry in enumerate(additional):
            if not isinstance(entry, dict):
                continue
            group = str(
                entry.get("limit_name")
                or entry.get("model")
                or entry.get("metered_feature")
                or f"Model limit {index + 1}"
            ).strip()
            rate = entry.get("rate_limit") or {}
            if not isinstance(rate, dict):
                continue
            allowed = rate.get("allowed")
            reached = rate.get("limit_reached")
            for slot, fallback in (("primary_window", "Model window"), ("secondary_window", "Weekly")):
                item = _parse_window(
                    rate.get(slot),
                    group=group,
                    kind="model",
                    slot=slot,
                    fallback_label=fallback,
                    allowed=allowed if isinstance(allowed, bool) else None,
                    limit_reached=reached if isinstance(reached, bool) else None,
                )
                if item:
                    item["metered_feature"] = str(entry.get("metered_feature") or "")
                    limits.append(item)

    reset_data = payload.get("rate_limit_reset_credits") or {}
    reset_credits = 0
    if isinstance(reset_data, dict):
        reset_number = _number(reset_data.get("available_count"))
        if reset_number is not None:
            reset_credits = max(0, int(reset_number))

    credit_data = payload.get("credits") or {}
    credits: dict[str, Any] | None = None
    if isinstance(credit_data, dict) and credit_data.get("has_credits"):
        balance = _number(credit_data.get("balance"))
        credits = {
            "balance": balance,
            "unlimited": bool(credit_data.get("unlimited")),
        }

    raw_plan = str(payload.get("plan_type") or "").strip()
    plan = raw_plan.replace("_", " ").replace("-", " ").title() if raw_plan else None
    return {
        "available": bool(limits),
        "provider": "openai-codex",
        "plan": plan,
        "limits": limits,
        "reset_credits": reset_credits,
        "credits": credits,
        "fetched_at": time.time(),
        "source": "codex-usage-api",
    }


def _fallback_quota_snapshot(snapshot=None) -> dict[str, Any]:
    if snapshot is None:
        from agent.account_usage import fetch_account_usage
        snapshot = fetch_account_usage("openai-codex")
    if snapshot is None:
        return {
            "available": False,
            "provider": "openai-codex",
            "limits": [],
            "fetched_at": time.time(),
            "error": "Codex quota is temporarily unavailable.",
        }

    limits: list[dict[str, Any]] = []
    for index, window in enumerate(snapshot.windows):
        used = _number(window.used_percent)
        if used is None:
            continue
        used = max(0.0, min(100.0, used))
        limits.append(
            {
                "id": f"account:fallback:{index}",
                "kind": "account",
                "group": "Codex account",
                "label": window.label,
                "used_percent": round(used, 2),
                "remaining_percent": round(max(0.0, 100.0 - used), 2),
                "reset_at": window.reset_at.timestamp() if window.reset_at else None,
                "window_seconds": None,
                "allowed": None,
                "limit_reached": used >= 100,
            }
        )
    return {
        "available": bool(limits),
        "provider": "openai-codex",
        "plan": snapshot.plan,
        "limits": limits,
        "reset_credits": 0,
        "credits": None,
        "fetched_at": time.time(),
        "source": snapshot.source,
    }


def _quota_snapshot_uncached() -> dict[str, Any]:
    try:
        # Prefer Hermes's public snapshot; private endpoint helpers change names.
        from agent.account_usage import fetch_account_usage
        snapshot = fetch_account_usage("openai-codex")
        if snapshot is None:
            raise ValueError("No Codex quota snapshot")
        raw = getattr(snapshot, "raw", None)
        if isinstance(raw, dict):
            # Only allowlisted numeric/model fields reach the renderer.
            return _parse_quota_payload(raw)
        return _fallback_quota_snapshot(snapshot)
    except Exception:
        return {"available": False, "limits": [], "status": "error",
                "error": "Codex quota is temporarily unavailable."}


def _quota_snapshot() -> dict[str, Any]:
    global _quota_cache
    now = time.monotonic()
    with _quota_lock:
        if _quota_cache and now - _quota_cache[0] < _QUOTA_CACHE_SECONDS:
            return _quota_cache[1]
        snapshot = _quota_snapshot_uncached()
        _quota_cache = (time.monotonic(), snapshot)
        return snapshot


def _models_snapshot() -> dict[str, Any]:
    db_path = get_hermes_home() / "state.db"
    if not db_path.exists():
        return {
            "available": False,
            "models": [],
            "generated_at": time.time(),
            "error": "Hermes token history is not available yet.",
        }

    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT model,
                   billing_provider,
                   task,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(reasoning_tokens) AS reasoning_tokens,
                   SUM(api_call_count) AS api_calls,
                   MIN(first_seen) AS first_seen,
                   MAX(last_seen) AS last_seen
              FROM session_model_usage
             WHERE model IS NOT NULL AND model != ''
             GROUP BY model, billing_provider, task
            """
        ).fetchall()
    except sqlite3.Error:
        return {
            "available": False,
            "models": [],
            "generated_at": time.time(),
            "error": "Hermes per-model token history is unavailable.",
        }
    finally:
        connection.close()

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
                "first_seen": None,
                "last_seen": None,
                "tasks": {},
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
        first_seen = _number(row.get("first_seen"))
        last_seen = _number(row.get("last_seen"))
        if first_seen is not None:
            entry["first_seen"] = (
                first_seen
                if entry["first_seen"] is None
                else min(entry["first_seen"], first_seen)
            )
        if last_seen is not None:
            entry["last_seen"] = (
                last_seen
                if entry["last_seen"] is None
                else max(entry["last_seen"], last_seen)
            )
        task = str(row.get("task") or "main")
        task_entry = entry["tasks"].setdefault(
            task,
            {
                "task": task,
                "tokens": 0,
                "api_calls": 0,
            },
        )
        task_entry["tokens"] += int(row.get("input_tokens") or 0)
        task_entry["tokens"] += int(row.get("output_tokens") or 0)
        task_entry["tokens"] += int(row.get("cache_read_tokens") or 0)
        task_entry["tokens"] += int(row.get("cache_write_tokens") or 0)
        task_entry["api_calls"] += int(row.get("api_calls") or 0)

    models: list[dict[str, Any]] = []
    for entry in grouped.values():
        entry["providers"] = sorted(entry["providers"])
        entry["cache_tokens"] = entry["cache_read_tokens"] + entry["cache_write_tokens"]
        entry["total_tokens"] = (
            entry["input_tokens"]
            + entry["output_tokens"]
            + entry["cache_read_tokens"]
            + entry["cache_write_tokens"]
        )
        entry["tasks"] = sorted(
            entry["tasks"].values(),
            key=lambda item: item["tokens"],
            reverse=True,
        )
        models.append(entry)

    models.sort(key=lambda item: item["total_tokens"], reverse=True)
    return {
        "available": bool(models),
        "models": models,
        "overall": {
            "input_tokens": sum(item["input_tokens"] for item in models),
            "output_tokens": sum(item["output_tokens"] for item in models),
            "cache_tokens": sum(item["cache_tokens"] for item in models),
            "total_tokens": sum(item["total_tokens"] for item in models),
            "api_calls": sum(item["api_calls"] for item in models),
        },
        "tracked_since": min(
            (item["first_seen"] for item in models if item["first_seen"] is not None),
            default=None,
        ),
        "latest_usage_at": max(
            (item["last_seen"] for item in models if item["last_seen"] is not None),
            default=None,
        ),
        "generated_at": time.time(),
        "source": "hermes-state-db",
    }


def _latest_session_snapshot() -> dict[str, Any]:
    """Return read-only spend counters for the most recently active session.

    These are cumulative API-accounting counters, not current context-window
    occupancy. Keeping that distinction in the payload prevents the desktop
    companion from presenting session spend as a context percentage.
    """
    db_path = get_hermes_home() / "state.db"
    if not db_path.exists():
        return {
            "available": False,
            "generated_at": time.time(),
            "error": "Hermes session telemetry is not available yet.",
        }

    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        raw = connection.execute(
            """
            SELECT model,
                   billing_provider,
                   input_tokens,
                   output_tokens,
                   cache_read_tokens,
                   cache_write_tokens,
                   reasoning_tokens,
                   api_call_count,
                   started_at,
                   last_activity_at
              FROM sessions
             WHERE COALESCE(archived, 0) = 0
             ORDER BY COALESCE(last_activity_at, started_at) DESC
             LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return {
            "available": False,
            "generated_at": time.time(),
            "error": "Hermes session telemetry is unavailable.",
        }
    finally:
        connection.close()

    if raw is None:
        return {
            "available": False,
            "generated_at": time.time(),
            "error": "No Hermes session telemetry has been recorded yet.",
        }

    row = dict(raw)
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    cache_read_tokens = int(row.get("cache_read_tokens") or 0)
    cache_write_tokens = int(row.get("cache_write_tokens") or 0)
    return {
        "available": True,
        "model": str(row.get("model") or "unknown"),
        "provider": str(row.get("billing_provider") or ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_tokens": cache_read_tokens + cache_write_tokens,
        "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
        "total_tokens": (
            input_tokens
            + output_tokens
            + cache_read_tokens
            + cache_write_tokens
        ),
        "api_calls": int(row.get("api_call_count") or 0),
        "started_at": _number(row.get("started_at")),
        "last_activity_at": _number(row.get("last_activity_at")),
        "generated_at": time.time(),
        "source": "hermes-state-db",
        "metric": "session-spend",
    }


@router.get("/quota")
async def quota() -> dict[str, Any]:
    return await asyncio.to_thread(_quota_snapshot)


@router.get("/models")
async def models() -> dict[str, Any]:
    return await asyncio.to_thread(_models_snapshot)


@router.get("/session")
async def session() -> dict[str, Any]:
    return await asyncio.to_thread(_latest_session_snapshot)


@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "quota_cache_seconds": _QUOTA_CACHE_SECONDS,
        "state_db": (get_hermes_home() / "state.db").exists(),
    }
