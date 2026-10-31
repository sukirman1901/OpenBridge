"""Standard-library JSON HTTP transport with conservative retry semantics."""
import base64
import json
import time
import urllib.error
import urllib.request


class JsonHttpClient:
    def __init__(self, opener=urllib.request.urlopen, sleep=time.sleep):
        self.opener = opener
        self.sleep = sleep

    def __call__(self, url, method="GET", body=None, auth=None, timeout=45):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if auth:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            request.add_header("Authorization", "Basic " + token)
        # A disconnected POST may already have reached the server. Retrying it
        # could duplicate a prompt, a tool action, or a Telegram message.
        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                with self.opener(request, timeout=timeout) as response:
                    raw = response.read().decode()
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as error:
                try:
                    return {"error": error.code, "body": error.read().decode()[:300]}
                finally:
                    error.close()
            except Exception as error:
                if attempt + 1 == attempts:
                    # urllib exceptions can contain a credential-bearing URL.
                    return {"error": type(error).__name__}
                self.sleep(2)
