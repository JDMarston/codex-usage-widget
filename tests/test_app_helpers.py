from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (
    GROK_USAGE_URL,
    choose_current_limit,
    compact_number,
    compute_resized_geometry,
    launch_grok_usage,
    layout_dimensions,
    quota_tooltip_text,
    monitored_provider_labels,
    monitored_provider_text,
    normalize_window_sizes,
    ordered_model_rows,
    reset_text,
    snapshot_render_targets,
    tray_usage_title,
    visible_limits,
)


class FormattingTests(unittest.TestCase):
    def test_compact_number(self) -> None:
        self.assertEqual(compact_number(999), "999")
        self.assertEqual(compact_number(12_300), "12.3K")
        self.assertEqual(compact_number(4_000_000), "4M")

    def test_reset_text(self) -> None:
        self.assertEqual(reset_text(1_000 + 61, now=1_000), "resets in 2m")
        self.assertEqual(reset_text(1_000 + 7_200, now=1_000), "resets in 2h 0m")

    def test_quota_tooltip_uses_reported_period_start_and_next_reset(self) -> None:
        limit = {"used_percent": 24.6, "period_start_at": 1_000, "reset_at": 2_000}
        text = quota_tooltip_text(limit, formatter=lambda value: f"time-{value}")
        self.assertEqual(text, "24.6% used\nLast reset: time-1000\nNext reset: time-2000")

    def test_quota_tooltip_derives_last_reset_from_window_length(self) -> None:
        limit = {"used_percent": 40, "reset_at": 700_000, "window_seconds": 604_800}
        text = quota_tooltip_text(limit, formatter=lambda value: f"time-{int(value)}")
        self.assertEqual(text, "40% used\nLast reset: time-95200\nNext reset: time-700000")

    def test_quota_tooltip_omits_unavailable_reset_times(self) -> None:
        self.assertEqual(quota_tooltip_text({"used_percent": 9}), "9% used")

    def test_quota_tooltip_includes_provider_metric_details(self) -> None:
        limit = {
            "used_percent": 20,
            "details": "RPM 1 / 5\nTPM 25.39K / 250K\nRPD 1 / 20",
        }

        self.assertEqual(
            quota_tooltip_text(limit),
            "20% used\nRPM 1 / 5\nTPM 25.39K / 250K\nRPD 1 / 20",
        )

    def test_model_specific_limit_wins(self) -> None:
        quota = {
            "limits": [
                {"id": "account", "kind": "account", "group": "Codex account"},
                {"id": "spark", "kind": "model", "group": "GPT-5.3-Codex-Spark"},
            ]
        }
        self.assertEqual(choose_current_limit(quota, "gpt-5.3-codex-spark")["id"], "spark")
        self.assertEqual(choose_current_limit(quota, "gpt-5.6-sol")["id"], "account")
        self.assertEqual([item["id"] for item in visible_limits(quota, "gpt-5.3-codex-spark")], ["spark", "account"])

    def test_grok_usage_launcher_uses_official_usage_page(self) -> None:
        calls: list[tuple[str, int]] = []

        def opener(url: str, *, new: int) -> bool:
            calls.append((url, new))
            return True

        self.assertTrue(launch_grok_usage(opener))
        self.assertEqual(calls, [(GROK_USAGE_URL, 2)])

    def test_monitored_provider_labels_ignore_historical_providers(self) -> None:
        state = {
            "provider_quotas": {
                "providers": [
                    {"id": "codex", "label": "Codex", "available": True, "limits": []},
                    {"id": "grok", "label": "Grok", "available": True, "limits": []},
                    {"id": "gemini", "label": "Gemini", "available": True, "limits": []},
                ]
            },
            "session": {"provider_kind": "grok", "providers": ["xai-oauth"]},
            "models": {
                "models": [
                    {"provider_kind": "codex", "providers": ["openai-codex"]},
                    {"provider_kind": "other", "providers": ["anthropic"]},
                ]
            },
        }

        self.assertEqual(monitored_provider_labels(state), ["CODEX", "GROK", "GEMINI"])

        state["provider_quotas"]["providers"] = state["provider_quotas"]["providers"][:2]
        state["models"]["models"].append({"provider_kind": "gemini", "providers": ["gemini"]})
        self.assertEqual(monitored_provider_labels(state), ["CODEX", "GROK"])

    def test_monitored_provider_text_distinguishes_loading_from_disconnected(self) -> None:
        self.assertEqual(monitored_provider_text({"provider_quotas": None}), "DETECTING…")
        self.assertEqual(monitored_provider_text({"provider_quotas": {"providers": []}}), "NONE CONNECTED")

    def test_tray_title_includes_every_available_provider_quota(self) -> None:
        state = {
            "provider_quotas": {
                "providers": [
                    {"id": "codex", "label": "Codex", "limits": [{"kind": "account", "remaining_percent": 74}]},
                    {"id": "grok", "label": "Grok", "limits": [{"remaining_percent": 75}]},
                    {"id": "gemini", "label": "Gemini", "limits": [{"remaining_percent": 80}]},
                ]
            },
            "session": {"model": "gpt-5.6-sol"},
        }

        self.assertEqual(tray_usage_title(state), "API Usage — Codex 74% left · Grok 75% left · Gemini 80% left")

    def test_ordered_model_rows_keeps_every_model_and_promotes_active(self) -> None:
        snapshot = {
            "models": [
                {"model": "gpt-5.6-sol"},
                {"model": "grok-4.6"},
                {"model": "claude-sonnet"},
                {"model": "gemini-3-flash-preview"},
            ]
        }

        rows = ordered_model_rows(snapshot, "gemini-3-flash-preview")

        self.assertEqual(len(rows), 4)
        self.assertEqual([item["model"] for item in rows], [
            "gemini-3-flash-preview",
            "gpt-5.6-sol",
            "grok-4.6",
            "claude-sonnet",
        ])

    def test_session_token_refresh_does_not_rebuild_quota_cards(self) -> None:
        state = {"session": {"model": "gpt-5.6-sol", "total_tokens": 100}}
        snapshot = {"session": {"model": "gpt-5.6-sol", "total_tokens": 200}}

        self.assertEqual(snapshot_render_targets(state, snapshot), {"providers", "session"})

    def test_session_model_change_refreshes_quota_tray_and_model_rows(self) -> None:
        state = {"session": {"model": "gpt-5.6-sol", "total_tokens": 100}}
        snapshot = {"session": {"model": "gpt-5.3-codex-spark", "total_tokens": 200}}

        self.assertEqual(
            snapshot_render_targets(state, snapshot),
            {"providers", "quota", "session", "models", "tray"},
        )

    def test_provider_quota_change_rebuilds_all_quota_surfaces(self) -> None:
        state = {"provider_quotas": {"providers": []}}
        snapshot = {"provider_quotas": {"providers": [{"id": "gemini", "limits": []}]}}

        self.assertEqual(snapshot_render_targets(state, snapshot), {"providers", "quota", "tray"})

    def test_unchanged_snapshot_does_not_rebuild_any_section(self) -> None:
        session = {"model": "gpt-5.6-sol", "total_tokens": 100}

        self.assertEqual(snapshot_render_targets({"session": session}, {"session": dict(session)}), set())

    def test_normalize_window_sizes_clamps_to_minimums(self) -> None:
        sizes = normalize_window_sizes({"full": [100, 100], "small": [500, 300], "other": [1, 1]})
        self.assertEqual(sizes["full"], [320, 480])
        self.assertEqual(sizes["small"], [500, 300])

    def test_layout_dimensions_uses_saved_custom_size(self) -> None:
        self.assertEqual(layout_dimensions("full", {"full": [420, 800]}), (420, 800))
        self.assertEqual(layout_dimensions("small"), (340, 330))

    def test_compute_resized_geometry_south_east(self) -> None:
        x, y, width, height = compute_resized_geometry(
            "se",
            start_x=100,
            start_y=200,
            start_w=330,
            start_h=225,
            pointer_dx=40,
            pointer_dy=50,
            min_w=280,
            min_h=180,
            work_area=(0, 0, 1920, 1080),
        )
        self.assertEqual((x, y, width, height), (100, 200, 370, 275))

    def test_compute_resized_geometry_north_west_respects_minimum(self) -> None:
        x, y, width, height = compute_resized_geometry(
            "nw",
            start_x=100,
            start_y=200,
            start_w=330,
            start_h=225,
            pointer_dx=200,
            pointer_dy=200,
            min_w=280,
            min_h=180,
            work_area=(0, 0, 1920, 1080),
        )
        self.assertEqual(width, 280)
        self.assertEqual(height, 180)
        self.assertEqual(x, 150)
        self.assertEqual(y, 245)


if __name__ == "__main__":
    unittest.main()
