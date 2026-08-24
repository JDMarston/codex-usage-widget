from __future__ import annotations

import json
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_client import (
    HermesUsageData,
    configured_provider_ids,
    detected_provider_ids,
    provider_kind,
    provider_model_for,
)


class ProviderClassificationTests(unittest.TestCase):
    def test_xai_models_are_grok(self) -> None:
        self.assertEqual(provider_kind("grok-4", ["xai"]), "grok")
        self.assertEqual(provider_kind("grok-3", []), "grok")

    def test_codex_provider(self) -> None:
        self.assertEqual(provider_kind("gpt-5.6-sol", ["openai-codex"]), "codex")

    def test_gemini_provider(self) -> None:
        self.assertEqual(provider_kind("gemini-3-flash-preview", ["gemini"]), "gemini")

    def test_unknown_provider(self) -> None:
        self.assertEqual(provider_kind("qwen2.5", ["custom"]), "other")


class ModelDiscoveryTests(unittest.TestCase):
    def test_active_session_model_appears_before_it_has_usage_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            db_path = home / "state.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE session_model_usage (
                        model TEXT,
                        billing_provider TEXT,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        cache_read_tokens INTEGER,
                        cache_write_tokens INTEGER,
                        reasoning_tokens INTEGER,
                        api_call_count INTEGER,
                        last_seen REAL
                    );
                    CREATE TABLE sessions (
                        model TEXT,
                        billing_provider TEXT,
                        last_activity_at REAL,
                        started_at REAL
                    );
                    INSERT INTO session_model_usage VALUES
                    ('gpt-5.6-sol', 'openai-codex', 10, 20, 0, 0, 0, 1, 1000);
                    INSERT INTO sessions VALUES
                    ('gemini-3-flash-preview', 'gemini', 2000, 1900);
                    """
                )
            finally:
                connection.close()

            snapshot = HermesUsageData(home, home / "quota_worker.py").fetch_models()
            gemini = next(item for item in snapshot["models"] if item["model"] == "gemini-3-flash-preview")

            self.assertTrue(snapshot["available"])
            self.assertEqual(gemini["providers"], ["gemini"])
            self.assertEqual(gemini["total_tokens"], 0)

    def test_detected_providers_drive_registered_quota_collectors(self) -> None:
        session = {"model": "gpt-5.6-sol", "provider_kind": "codex", "providers": ["openai-codex"]}
        models = {
            "models": [
                {"model": "grok-4.6", "provider_kind": "grok", "providers": ["xai-oauth"], "last_seen": 10},
                {"model": "gemini-3-flash-preview", "provider_kind": "gemini", "providers": ["gemini"], "last_seen": 20},
            ]
        }

        self.assertEqual(detected_provider_ids(session, models), ["codex", "grok", "gemini"])
        self.assertEqual(provider_model_for("gemini", session, models), "gemini-3-flash-preview")

    def test_provider_quota_snapshot_is_dynamic_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "auth.json").write_text(
                json.dumps({"providers": {"openai-codex": {}, "xai-oauth": {}}}),
                encoding="utf-8",
            )
            (home / ".env").write_text("GEMINI_API_KEY=test-only\n", encoding="utf-8")
            data = HermesUsageData(home, home / "quota_worker.py")
            captured: list[str] = []
            data.fetch_quota = lambda: {"available": True, "limits": [{"id": "codex", "remaining_percent": 70}]}
            data.fetch_grok_quota = lambda: {"available": True, "limit": {"id": "grok", "remaining_percent": 60}}
            data.fetch_gemini_quota = lambda model: captured.append(model) or {
                "available": True,
                "limit": {"id": "gemini", "remaining_percent": 80},
            }
            session = {"model": "gpt-5.6-sol", "provider_kind": "codex"}
            models = {
                "models": [
                    {"model": "grok-4.6", "provider_kind": "grok", "last_seen": 10},
                    {"model": "gemini-3-flash-preview", "provider_kind": "gemini", "last_seen": 20},
                ]
            }

            snapshot = data.fetch_provider_quotas(session, models)

            self.assertEqual([item["id"] for item in snapshot["providers"]], ["codex", "grok", "gemini"])
            self.assertEqual([len(item["limits"]) for item in snapshot["providers"]], [1, 1, 1])
            self.assertEqual(captured, ["gemini-3-flash-preview"])

    def test_removed_gemini_credential_cannot_be_resurrected_by_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "providers": {"openai-codex": {}, "xai-oauth": {}},
                        "credential_pool": {"gemini": [{"disabled": True}]},
                    }
                ),
                encoding="utf-8",
            )
            (home / ".env").write_text(
                "# GOOGLE_API_KEY=removed\n# GEMINI_API_KEY=\n",
                encoding="utf-8",
            )
            data = HermesUsageData(home, home / "quota_worker.py")
            data.fetch_quota = lambda: {"limits": [{"id": "codex"}]}
            data.fetch_grok_quota = lambda: {"limits": [{"id": "grok"}]}
            data.fetch_gemini_quota = lambda _model: self.fail("Gemini collector must not run")
            session = {"model": "gpt-5.6-sol", "provider_kind": "codex"}
            models = {
                "models": [
                    {"model": "gemini-3-flash-preview", "provider_kind": "gemini", "last_seen": 20}
                ]
            }

            snapshot = data.fetch_provider_quotas(session, models)

            self.assertEqual(configured_provider_ids(home), ["codex", "grok"])
            self.assertEqual([item["id"] for item in snapshot["providers"]], ["codex", "grok"])
            self.assertEqual(data.provider_context_key(session, models), (("codex", "gpt-5.6-sol"), ("grok", "")))


if __name__ == "__main__":
    unittest.main()
