"""Capture job output before delivery; direct control responses bypass jobs."""
from openbridge.ports.delivery import DeliveryChannel


class OutputService:
    def __init__(self, runtime, channel: DeliveryChannel):
        self.runtime = runtime
        self.channel = channel

    def _scope(self, account_id, conversation_id):
        job = self.runtime.current_job()
        if job and (job["channel"] != self.channel.channel or str(account_id) != job["account_id"]
                    or str(conversation_id) != job["conversation_id"]):
            raise RuntimeError("Cross-destination output requires a separate job")
        return job

    def send(self, account_id, conversation_id, text, buttons=None, columns=2):
        if self._scope(account_id, conversation_id):
            chunks = [text[index:index + 3500] for index in range(0, len(text), 3500)]
            for index, chunk in enumerate(chunks):
                self.runtime.emit({"operation": "send", "text": chunk,
                    "buttons": buttons if index == len(chunks) - 1 else None,
                    "cols": columns, "parse": False})
            return
        return self.channel.send_text(account_id, conversation_id, text, buttons, columns)

    def send_file(self, account_id, conversation_id, path, caption=None, kind="document"):
        """Hand a produced file to the chat; durable when a job is in scope."""
        if self._scope(account_id, conversation_id):
            self.runtime.emit({"operation": kind, "path": str(path), "caption": caption})
            return
        return self.channel.send_file(account_id, conversation_id, str(path), caption)

    def edit(self, account_id, conversation_id, message_id, text, buttons=None, columns=2, parse=False):
        if self._scope(account_id, conversation_id):
            self.runtime.emit({"operation": "edit", "message_id": message_id, "text": text[:4000],
                               "buttons": buttons, "cols": columns, "parse": parse})
            return
        return self.channel.edit_text(account_id, conversation_id, message_id, text, buttons, columns, parse)
