import threading
import unittest
from unittest.mock import Mock

from openbridge.adapters.persistence.sqlite.jobs import JobStore
from openbridge.domain.messages import ConversationKey
from openbridge.runtime.jobs import DurableRuntime
from openbridge.runtime.dispatcher import ConversationDispatcher


class DeliveryIsolation(unittest.TestCase):
    def test_slow_file_upload_cannot_hold_text_delivery(self):
        store = JobStore()
        self.addCleanup(store.close)
        for source, operation in ((1, 'document'), (2, 'send')):
            job, _ = store.accept(ConversationKey('telegram', '123', str(source)), source, {})
            store.acknowledge(job)
            store.claim(job)
            store.finish(job, [{'operation': operation, 'text': 'final', 'path': 'hasil/file.txt'}])
        upload, release, text = threading.Event(), threading.Event(), threading.Event()
        def deliver(item):
            if item['payload']['operation'] == 'document':
                upload.set()
                release.wait(3)
            else:
                text.set()
            return 'sent', None, None
        dispatcher = ConversationDispatcher()
        runtime = DurableRuntime(store, dispatcher, Mock(), deliver, Mock())
        try:
            runtime.start()
            self.assertTrue(upload.wait(1))
            self.assertTrue(text.wait(1), 'Text reply blocked behind an upload')
        finally:
            release.set()
            runtime.stop()
            dispatcher.shutdown(3)
