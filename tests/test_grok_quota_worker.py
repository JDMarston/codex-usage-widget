from __future__ import annotations

import unittest

from grok_quota_worker import parse_credits_response


class GrokQuotaParserTests(unittest.TestCase):
    def test_parses_captured_weekly_usage_payload(self) -> None:
        payload = bytes.fromhex(
            "0a5d0d0000904212001a00220b08f491c5d20610a896a277"
            "2a0b08f486ead20610a896a2773a07080115000048423a07"
            "0802150000b0413a020804421c0802120b08f491c5d20610"
            "a896a2771a0b08f486ead20610a896a277580162006801"
        )

        snapshot = parse_credits_response(payload)

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["limit"]["used_percent"], 72.0)
        self.assertEqual(snapshot["limit"]["remaining_percent"], 28.0)
        self.assertEqual(snapshot["limit"]["reset_at"], 1_784_316_788)
        self.assertEqual(snapshot["products"][0]["label"], "API")
        self.assertEqual(snapshot["products"][1]["label"], "Grok Build")


if __name__ == "__main__":
    unittest.main()