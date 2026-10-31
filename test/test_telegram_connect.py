import unittest
from unittest.mock import Mock

from openbridge.adapters.http import JsonHttpClient
from openbridge.adapters.channels.telegram.delivery import TelegramDelivery


def connection():
    conn = Mock()
    response = Mock(status=200)
    response.read.return_value = b'{"ok":true}'
    conn.getresponse.return_value = response
    return conn


class TelegramConnect(unittest.TestCase):
    def test_tls_failure_before_http_bytes_can_retry_without_replaying_post(self):
        bad, good = connection(), connection()
        bad.connect.side_effect = ConnectionResetError('TLS handshake reset')
        factory = Mock(side_effect=[bad, good])
        client = JsonHttpClient(telegram_factory=factory, sleep=Mock())
        result = client('https://api.telegram.org/bot123:test/sendMessage', 'POST', {'text': 'hi'})
        self.assertTrue(result['ok'])
        bad.request.assert_not_called()
        good.request.assert_called_once()

    def test_disconnect_after_http_write_is_never_retried(self):
        conn = connection()
        conn.getresponse.side_effect = ConnectionResetError('response lost')
        factory = Mock(return_value=conn)
        result = JsonHttpClient(telegram_factory=factory)(
            'https://api.telegram.org/bot123:test/sendMessage', 'POST', {'text': 'hi'})
        factory.assert_called_once()
        conn.request.assert_called_once()
        self.assertFalse(result.get('not_sent'))

    def test_exhausted_tls_connections_report_definitely_not_sent(self):
        conn = connection()
        conn.connect.side_effect = ConnectionResetError('TLS handshake reset')
        result = JsonHttpClient(telegram_factory=Mock(return_value=conn), sleep=Mock())(
            'https://api.telegram.org/bot123:test/sendMessage', 'POST', {'text': 'hi'})
        self.assertTrue(result['not_sent'])
        conn.request.assert_not_called()

    def test_known_unsent_delivery_is_retryable_not_quarantined(self):
        channel = TelegramDelivery(Mock(return_value={'error': 'ConnectionResetError', 'not_sent': True}),
                                   lambda account: {'token': '123:test'}, Mock(), Mock())
        result = channel.deliver({'id': 1, 'job_id': 1, 'account_id': '123', 'conversation_id': '42',
                                  'payload': {'operation': 'send', 'text': 'hi'}})
        self.assertEqual(result[0], 'pending')
