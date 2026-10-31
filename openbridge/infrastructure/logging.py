from datetime import datetime
import threading


class FileLogger:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def __call__(self, *parts):
        line = datetime.now().strftime("%m-%d %H:%M:%S") + " " + " ".join(map(str, parts))
        with self.lock:
            print(line, flush=True)
            try:
                with self.path.open("a") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass
