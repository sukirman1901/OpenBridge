import unittest

from test.stress_runtime import exercise


class RuntimeStress(unittest.TestCase):
    def test_fast_default_path_quarantines_ambiguous_sends_without_replay(self):
        result = exercise(messages=120, chats=12, acknowledge=False)
        self.assertEqual(result['execution_replays'], 0)
        self.assertEqual(result['delivery_outcomes'].get('uncertain'), 4)

    def test_bounded_multi_chat_burst_with_duplicate_ingress_and_network_faults(self):
        result = exercise(messages=120, chats=12)
        self.assertEqual(result['receipts'], 120)
        self.assertEqual(result['execution_replays'], 0)
