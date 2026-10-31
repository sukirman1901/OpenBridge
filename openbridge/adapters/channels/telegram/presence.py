"""Bounded, coalesced Telegram acknowledgements and cosmetic presence.

Model execution never waits for a status edit or typing action. A per-job wire
lock fences final delivery against any already-in-flight cosmetic edit.
"""
from dataclasses import dataclass, field
from contextlib import nullcontext
import threading
import time


LABELS = {'working': '⚙️ Processing…', 'waiting_approval': '⏳ Waiting for your approval…',
          'checking_backend': '🔄 Checking backend connection…'}


@dataclass
class _Entry:
    job: dict = field(repr=False)
    wire: object = field(default_factory=threading.Lock, repr=False)
    ack: object = field(default=None, repr=False)
    ack_sent: bool = False
    ack_mid: object = None
    next_ack: float = 0
    active: bool = False
    busy: bool = False
    desired: object = None
    sent: object = None
    typing: bool = False
    next_typing: float = 0
    final_note: bool = False


class TelegramPresence:
    def __init__(self, call, resolve, log, capacity=256, workers=2):
        if min(capacity, workers) < 1:
            raise ValueError('Presence limits must be positive')
        self.call, self.resolve, self.log = call, resolve, log
        self.capacity, self.workers = capacity, workers
        self.condition = threading.Condition()
        self.entries, self.threads = {}, []
        self.closed = False

    @property
    def size(self):
        with self.condition:
            return len(self.entries)

    def _room(self):
        if len(self.entries) >= self.capacity:
            for identity, item in list(self.entries.items()):
                if not item.active and not item.busy and item.ack is None:
                    del self.entries[identity]
                    if len(self.entries) < self.capacity:
                        break
        return len(self.entries) < self.capacity

    def acknowledge(self, identity, bot, chat, text, callback):
        queued = False
        with self.condition:
            if identity in self.entries:
                return  # The existing once-only ACK owns this callback.
            if not self.closed and identity not in self.entries and self._room():
                item = _Entry({'id': identity, 'account_id': bot['token'].split(':', 1)[0],
                               'conversation_id': str(chat), 'status_message_id': None})
                item.ack = (bot['token'], text, callback)
                self.entries[identity] = item
                queued = True
                self.condition.notify_all()
        if not queued:
            callback(None)  # Shed cosmetic load, never lose a durable request.

    def open(self, job):
        with self.condition:
            if self.closed:
                return
            item = self.entries.get(job['id'])
            if item is None:
                if not self._room():
                    return
                item = self.entries[job['id']] = _Entry(dict(job))
            item.job = dict(job)
            item.active = True
            item.final_note = False

    def update(self, identity, sid, state):
        if state not in LABELS:
            return
        with self.condition:
            item = self.entries.get(identity)
            if item and item.active and item.job.get('status_message_id'):
                item.desired = (LABELS[state], sid)
                self.condition.notify_all()

    def typing(self, identity):
        with self.condition:
            item = self.entries.get(identity)
            if item and item.active:
                item.typing = True
                self.condition.notify_all()

    def finish(self, identity, deferred=False):
        with self.condition:
            item = self.entries.get(identity)
            if item:
                item.active, item.typing = False, False
                note = deferred and bool(item.job.get('status_message_id'))
                item.desired = ('⏳ Waiting to retry after a backend limit…', None) if note else None
                item.final_note = note
                self.condition.notify_all()

    def finalize(self, identity, deliver):
        self.finish(identity)
        with self.condition:
            item = self.entries.get(identity)
        if item is None:
            return deliver()
        with item.wire:
            result = deliver()
        with self.condition:
            if self.entries.get(identity) is item:
                del self.entries[identity]
        return result

    def _next(self):
        now = time.monotonic()
        # ACKs release durable jobs and take priority over purely cosmetic work.
        for item in self.entries.values():
            if not item.busy and item.ack is not None and now >= item.next_ack:
                item.busy = True
                return item, 'ack', item.ack
        for item in self.entries.values():
            if item.busy:
                continue
            if (item.active or item.final_note) and item.desired and item.desired != item.sent:
                item.busy = True
                return item, 'status', item.desired
            if item.active and item.typing and now >= item.next_typing:
                item.busy = True
                item.next_typing = now + 4
                return item, 'typing', None
        return None

    def _work(self):
        while True:
            with self.condition:
                work = self._next()
                while not self.closed and work is None:
                    self.condition.wait(.1)
                    work = self._next()
                if self.closed:
                    return
            item, kind, payload = work
            mid = None
            try:
                with item.wire if kind != 'typing' else nullcontext():
                    if kind == 'ack' and not item.ack_sent:
                        token, text, callback = payload
                        result = self.call(token, 'sendMessage', {'chat_id': item.job['conversation_id'], 'text': text}, timeout=5)
                        if result.get('ok'):
                            mid = (result.get('result') or {}).get('message_id')
                    elif kind != 'ack' and (item.active or (kind == 'status' and item.final_note)):
                        bot = self.resolve(item.job['account_id'])
                        if bot:
                            if kind == 'typing':
                                self.call(bot['token'], 'sendChatAction', {'chat_id': item.job['conversation_id'],
                                          'action': 'typing'}, timeout=5)
                            else:
                                text, sid = payload
                                keys = [[{'text': 'Stop', 'callback_data': f'st-{sid}'}]] if sid else []
                                self.call(bot['token'], 'editMessageText', {'chat_id': item.job['conversation_id'],
                                    'message_id': item.job['status_message_id'], 'text': text,
                                    'reply_markup': {'inline_keyboard': keys}}, timeout=2)
            except Exception as error:
                self.log('Telegram presence unavailable', type(error).__name__)
            finally:
                if kind == 'ack':
                    # Never retry an ambiguous send. A late cosmetic ACK is
                    # harmless; replaying a model execution would not be.
                    if not item.ack_sent:
                        item.ack_mid, item.ack_sent = mid, True
                    persisted = False
                    try:
                        payload[2](item.ack_mid)
                        persisted = True
                    except Exception as error:
                        self.log('Durable acknowledgement needs recovery', type(error).__name__)
                with self.condition:
                    if kind == 'ack':
                        if persisted:
                            item.ack = None
                        else:
                            item.next_ack = time.monotonic() + .5
                    elif kind == 'status':
                        item.sent = payload
                        item.final_note = False
                    item.busy = False
                    self.condition.notify_all()

    def start(self):
        with self.condition:
            if self.threads or self.closed:
                return
            self.threads = [threading.Thread(target=self._work, name=f'telegram-presence-{i}', daemon=True)
                            for i in range(self.workers)]
            for thread in self.threads:
                thread.start()

    def close(self):
        with self.condition:
            self.closed = True
            pending = [(item.ack[2], item.ack_mid) for item in self.entries.values() if item.ack and not item.busy]
            for item in self.entries.values():
                item.active, item.typing = False, False
                if not item.busy:
                    item.ack = None
            self.condition.notify_all()
        for callback, mid in pending:
            try:
                callback(mid)
            except Exception:
                pass
        for thread in self.threads:
            thread.join(3)

    def run(self, stopped):
        self.start()
        stopped.wait()
        self.close()
