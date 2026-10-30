import unittest
from runtime_policy import authorized, bot_id, schedule_key, schedule_owner


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.bots = [{"token": "101:example"}, {"token": "202:example"}]

    def test_owner_only(self):
        config = {"owner_ids": [55]}
        self.assertTrue(authorized(config, {"id": 55}))
        self.assertFalse(authorized(config, {"id": 99}))
        self.assertFalse(authorized(config, {"id": 55, "is_bot": True}))
        self.assertFalse(authorized({}, {"id": 55}))

    def test_bot_id_not_chat_id(self):
        self.assertEqual(bot_id(self.bots[0]), "101")

    def test_legacy_requires_unambiguous_owner(self):
        self.assertIsNone(schedule_owner({}, self.bots))
        self.assertEqual(schedule_owner({}, self.bots[:1]), "101")

    def test_explicit_owner(self):
        self.assertEqual(schedule_owner({"bot_id": "202"}, self.bots), "202")
        self.assertIsNone(schedule_owner({"bot_id": "999"}, self.bots))

    def test_same_time_different_bots_not_suppressed(self):
        a = {"bot_id": "101", "jam": "08:00"}
        b = {"bot_id": "202", "jam": "08:00"}
        self.assertNotEqual(schedule_key(a, 'minute'), schedule_key(b, 'minute'))
        self.assertEqual(schedule_key(a, 'minute'), schedule_key(dict(a), 'minute'))


if __name__ == '__main__':
    unittest.main()
