from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import GROK_USAGE_URL, choose_current_limit, compact_number, launch_grok_usage, reset_text, visible_limits


class FormattingTests(unittest.TestCase):
    def test_compact_number(self) -> None:
        self.assertEqual(compact_number(999), "999")
        self.assertEqual(compact_number(12_300), "12.3K")
        self.assertEqual(compact_number(4_000_000), "4M")

    def test_reset_text(self) -> None:
        self.assertEqual(reset_text(1_000 + 61, now=1_000), "resets in 2m")
        self.assertEqual(reset_text(1_000 + 7_200, now=1_000), "resets in 2h 0m")

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


if __name__ == "__main__":
    unittest.main()
