"""Tests for the wizard's config builder (pure function part)."""
import unittest
from wizard import build_config


class TestWizardConfig(unittest.TestCase):
    def test_shape_and_indirection(self):
        cfg = build_config("My Bot", "my-bot.token", 42)
        bot = cfg["bots"][0]
        self.assertEqual(bot["token"], "{file:my-bot.token}")
        self.assertEqual(bot["nama"], "My Bot")
        self.assertEqual(cfg["owner_ids"], [42])

    def test_budget_and_trigger_defaults(self):
        cfg = build_config("b", "t.token", 1)
        self.assertEqual(cfg["budget"]["max_call_per_hari"], 80)
        self.assertEqual(cfg["trigger"]["initiatif_detik"], 300)
        self.assertEqual(cfg["rate_limiter"]["max_per_menit"], 6)

    def test_owner_id_is_int(self):
        cfg = build_config("b", "t.token", "7")
        self.assertEqual(cfg["owner_ids"], [7])


if __name__ == "__main__":
    unittest.main()
