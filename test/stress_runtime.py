"""Isolated, repeatable load harness. Never calls real Telegram or a provider."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock

from test.support import make_application


def exercise(messages=1000, chats=20, faults=True, acknowledge=True):
    case = unittest.TestCase()
    lock = threading.Lock()
    sequences, executed, received, attempts = defaultdict(list), Counter(), Counter(), Counter()
    destinations, final_messages = {}, set()
    stale = []
    serial = [100]
    calls = Counter()
    def call(token, method, payload=None, **options):
        text, chat = (payload or {}).get('text', ''), str((payload or {}).get('chat_id', ''))
        time.sleep(.0005)
        with lock:
            calls[method] += 1
            if text.startswith('reply:'):
                job = int(text.split(':')[1])
                case.assertEqual(destinations[job], chat, 'Cross-chat delivery')
                attempts[job] += 1
                if faults and job % 17 == 0 and attempts[job] == 1:
                    return {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 1}}
                received[job] += 1
                if method == 'editMessageText':
                    final_messages.add((chat, payload['message_id']))
                if faults and job % 29 == 0 and method == 'sendMessage':
                    return {'error': 'URLError'}  # Simulate accepted send, response lost.
            elif method == 'editMessageText' and (chat, payload.get('message_id')) in final_messages:
                stale.append((chat, payload['message_id']))
            serial[0] += 1
            return {'ok': True, 'result': {'message_id': serial[0]}}
    app = make_application(case, call=call, async_presence=True, changes={
        'telegram': {'acknowledge': acknowledge},
        'runtime': {'interactive_workers': 4, 'background_workers': 2,
                    'max_pending_messages': 16, 'max_pending_per_conversation': 4},
        'budget': {'max_call_per_hari': messages * 3},
        'rate_limiter': {'max_per_menit': messages * 3}})
    p = app.components
    p.conversations.get = Mock(side_effect=lambda bot, chat: f'sid-{chat}')
    p.conversations.rate = None
    clock = [time.time()]
    p.jobs.clock = lambda: clock[0]
    def prompt(sid, text, **options):
        job = p.runtime.current_job()
        with lock:
            executed[job['id']] += 1
            sequences[job['conversation_id']].append(job['id'])
        options['on_accepted'](f'user-{job["id"]}')
        for _ in range(20):
            options['on_state']('working')
        time.sleep(.001 if job['id'] % 7 else .003)
        return f'reply:{job["id"]}'
    p.backend.run_prompt.side_effect = prompt
    bot = p.configuration.data['bots'][0]
    def ingest(number):
        update = {'update_id': number, 'message': {'message_id': number,
            'chat': {'id': 1000 + number % chats, 'type': 'private'},
            'from': {'id': 42, 'first_name': 'Load tester'}, 'text': f'plain message {number}'}}
        p.gateway.accept(bot, update)
        if number % 10 == 0:
            p.gateway.accept(bot, update)  # duplicate redelivery must do nothing
    start = time.monotonic()
    max_presence, max_scheduled = 0, 0
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(ingest, range(1, messages + 1)))
        for row in p.jobs.db.execute('SELECT id,conversation_id FROM runtime_jobs'):
            destinations[row['id']] = row['conversation_id']
        deadline = time.monotonic() + max(60, messages / 4)
        while time.monotonic() < deadline:
            clock[0] += 5  # Deterministic accelerated retry clock, not execution retries.
            p.runtime.pump()
            for _ in range(32):
                if not p.runtime.deliver_one():
                    break
            max_presence = max(max_presence, p.presence.size)
            with p.runtime.lock:
                max_scheduled = max(max_scheduled, len(p.runtime.scheduled))
            with p.jobs.lock:
                unfinished = p.jobs.db.execute("SELECT COUNT(*) FROM runtime_jobs WHERE status!='succeeded'").fetchone()[0]
                pending = p.jobs.db.execute("SELECT COUNT(*) FROM runtime_deliveries WHERE status IN ('pending','sending')").fetchone()[0]
            if not unfinished and not pending:
                break
            time.sleep(.001)
        case.assertEqual(unfinished, 0, 'Accepted jobs did not settle')
        case.assertEqual(pending, 0, 'Delivery pump did not settle')
        case.assertEqual(len(executed), messages)
        case.assertTrue(all(count == 1 for count in executed.values()), 'Model work was replayed')
        case.assertEqual(len(received), messages)
        case.assertTrue(all(count == 1 for count in received.values()), 'Duplicate final delivery')
        case.assertTrue(all(seq == sorted(seq) for seq in sequences.values()), 'Conversation FIFO violated')
        case.assertEqual(stale, [], 'Cosmetic status overwrote a final answer')
        case.assertLessEqual(max_scheduled, 16)
        case.assertLessEqual(max_presence, p.presence.capacity)
        with p.jobs.lock:
            counts = dict(p.jobs.db.execute('SELECT status,COUNT(*) FROM runtime_deliveries GROUP BY status'))
            receipts = p.jobs.db.execute('SELECT COUNT(*) FROM runtime_prompts WHERE message_id IS NOT NULL').fetchone()[0]
            integrity = p.jobs.db.execute('PRAGMA quick_check').fetchone()[0]
            foreign_keys = p.jobs.db.execute('PRAGMA foreign_key_check').fetchall()
        case.assertEqual(receipts, messages)
        case.assertEqual(integrity, 'ok')
        case.assertEqual(foreign_keys, [])
        return {'messages': messages, 'chats': chats, 'duplicate_ingress': messages // 10,
                'text_acknowledgements': acknowledge,
                'execution_replays': 0, 'duplicate_final_deliveries': 0, 'stale_status_overwrites': 0,
                'seconds': round(time.monotonic() - start, 3), 'delivery_outcomes': counts,
                'max_presence_entries': max_presence, 'max_scheduled': max_scheduled,
                'channel_calls': dict(calls), 'receipts': receipts}
    finally:
        case.doCleanups()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--messages', type=int, default=1000)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--no-acks', action='store_true')
    args = parser.parse_args()
    results = [exercise(args.messages, acknowledge=not args.no_acks) for _ in range(args.rounds)]
    text = json.dumps(results, indent=2)
    if args.output:
        args.output.write_text(text + '\n')
    print(text)
