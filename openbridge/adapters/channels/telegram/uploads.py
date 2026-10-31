"""Multipart file uploads stay outside the runtime; no external dependencies."""
import json
import urllib.error
import urllib.request


def multipart(fields, field, filename, blob):
    """Serialize one file part plus plain fields; returns (content_type, body)."""
    boundary = "----openbridge7f3a2c1d9e84b6a5"
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


def post_multipart(token, method, fields, field, filepath, timeout=60):
    """Post a multipart upload and return the parsed Telegram response.

    The JSON transport cannot carry binary parts, so uploads bypass it directly;
    HTTP errors surface as Telegram-shaped {"ok": False} dictionaries."""
    content_type, body = multipart(fields, field, filepath.name, filepath.read_bytes())
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                     data=body, method="POST")
    request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as error:
        try:
            return {"ok": False, "error_code": error.code, "description": error.read().decode()[:300]}
        finally:
            error.close()
    except Exception as error:
        return {"ok": None, "description": type(error).__name__}
