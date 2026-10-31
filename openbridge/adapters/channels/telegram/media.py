import os
import re
import tempfile
import urllib.request

from runtime_policy import bot_id


class TelegramMedia:
    limit = 20 * 1000 * 1000

    def __init__(self, home, call, log, opener=urllib.request.urlopen):
        self.directory, self.call, self.log, self.opener = home / "media", call, log, opener

    def receive(self, bot, message):
        kind = next((name for name in ("photo", "document", "video", "voice", "audio", "sticker") if message.get(name)), None)
        if not kind:
            return "", ""
        item = message[kind][-1] if kind == "photo" and isinstance(message[kind], list) else message[kind]
        if not item.get("file_id") or item.get("file_size", 0) > self.limit:
            return "", ""
        response = self.call(bot["token"], "getFile", {"file_id": item["file_id"]})
        remote = (response.get("result") or {}).get("file_path")
        if not remote:
            return "", ""
        extension = str(remote).rsplit(".", 1)[-1]
        extension = extension if re.fullmatch(r"[a-zA-Z0-9]{1,10}", extension) else "bin"
        self.directory.mkdir(parents=True, exist_ok=True)
        name = f"{bot_id(bot)}-{message['chat']['id']}-{message['message_id']}-{kind}.{extension}"
        destination = self.directory / name
        fd, temporary = tempfile.mkstemp(prefix=".download-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output, self.opener(f"https://api.telegram.org/file/bot{bot['token']}/{remote}", timeout=30) as source:
                count = 0
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > self.limit:
                        raise ValueError("Attachment exceeds limit")
                    output.write(chunk)
            os.replace(temporary, destination)
            description = f"{kind} {item.get('file_name') or extension!r} saved locally at {destination}"
            return description, str(destination)
        except Exception as error:
            self.log("Attachment download failed", type(error).__name__)
            return "", ""
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
