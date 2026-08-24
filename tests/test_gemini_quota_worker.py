from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gemini_quota_worker import parse_amount, parse_rate_limit_rows


class GeminiQuotaParserTests(unittest.TestCase):
    def test_parse_amount_supports_compact_ai_studio_values(self) -> None:
        self.assertEqual(parse_amount("25.39K"), 25_390)
        self.assertEqual(parse_amount("1.5M"), 1_500_000)
        self.assertEqual(parse_amount("-"), None)

    def test_active_model_uses_most_constrained_real_meter(self) -> None:
        rows = [
            ["check", "Gemini 2.5 Flash", "Text-out models", "1 / 5", "408 / 250K", "2 / 20", "bar_chart"],
            ["check", "Gemini 3 Flash", "Text-out models", "1 / 5", "25.39K / 250K", "1 / 20", "bar_chart"],
        ]

        snapshot = parse_rate_limit_rows(rows, "gemini-3-flash-preview", fetched_at=1234)

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["model"], "Gemini 3 Flash")
        self.assertEqual(snapshot["limit"]["metric"], "RPM")
        self.assertEqual(snapshot["limit"]["used_percent"], 20.0)
        self.assertEqual(snapshot["limit"]["remaining_percent"], 80.0)
        self.assertEqual(snapshot["limit"]["usage_text"], "RPM 1 / 5")
        self.assertIn("TPM 25.39K / 250K", snapshot["limit"]["details"])
        self.assertEqual(snapshot["fetched_at"], 1234)

    def test_unknown_model_is_reported_without_inventing_usage(self) -> None:
        snapshot = parse_rate_limit_rows(
            [["check", "Gemini 2.5 Flash", "Text-out models", "1 / 5", "408 / 250K", "2 / 20", "bar_chart"]],
            "gemini-9-pro",
        )

        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["limits"], [])


if __name__ == "__main__":
    unittest.main()
