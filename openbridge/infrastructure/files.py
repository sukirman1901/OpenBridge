import json
import os
from pathlib import Path
import tempfile


def read_json(path, default=None):
    try:
        with Path(path).open() as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json(path, value):
    """Private same-directory atomic replacement; no torn configuration files."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".openbridge-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_name(name):
    name = str(name).strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ValueError("Name cannot contain a path")
    return name
