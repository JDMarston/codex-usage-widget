from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_client import provider_kind


class ProviderClassificationTests(unittest.TestCase):
    def test_xai_models_are_grok(self) -> None:
        self.assertEqual(provider_kind("grok-4", ["xai"]), "grok")
        self.assertEqual(provider_kind("grok-3", []), "grok")

    def test_codex_provider(self) -> None:
        self.assertEqual(provider_kind("gpt-5.6-sol", ["openai-codex"]), "codex")

    def test_unknown_provider(self) -> None:
        self.assertEqual(provider_kind("qwen2.5", ["custom"]), "other")


if __name__ == "__main__":
    unittest.main()
