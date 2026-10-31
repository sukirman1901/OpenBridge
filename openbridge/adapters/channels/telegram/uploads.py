"""Multipart file uploads stay outside the runtime; no external dependencies."""
import os
import stat
import uuid

from openbridge.adapters.http import JsonHttpClient


def multipart(fields, field, filename, blob):
    """Serialize one file part plus plain fields; returns (content_type, body)."""
    boundary = '----openbridge' + uuid.uuid4().hex
    filename = str(filename).replace('"', '_').replace('\r', '_').replace('\n', '_')
    chunks = []
    for name, value in fields.items():
        if value is None:
            continue
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                       f"{value}\r\n").encode("utf-8"))
    chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                   f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                   ).encode("utf-8"))
    chunks.append(bytes(blob))
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def read_artifact(filepath, limit):
    """Bounded no-symlink reads, including all ancestor directory components."""
    path = filepath.absolute()
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    directory = os.open(path.anchor, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | nofollow, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | nofollow | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise ValueError('Invalid outbound artifact')
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('Artifact exceeds upload limit')
            return data
    finally:
        os.close(directory)


def post_multipart(token, method, fields, field, filepath, timeout=60):
    """Post a multipart upload and return the parsed Telegram response.

    The JSON transport cannot carry binary parts, so uploads bypass it directly;
    HTTP errors surface as Telegram-shaped {"ok": False} dictionaries."""
    try:
        blob = read_artifact(filepath, 10_000_000 if field == 'photo' else 50_000_000)
    except (OSError, ValueError, NotImplementedError):
        return {'ok': False, 'error_code': 400, 'description': 'Outbound artifact unavailable or exceeds upload limit'}
    content_type, body = multipart(fields, field, filepath.name, blob)
    return JsonHttpClient().post_bytes(f'https://api.telegram.org/bot{token}/{method}', body, content_type, timeout)
