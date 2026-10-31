"""Standard-library JSON HTTP transport with conservative retry semantics."""
import base64
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class JsonHttpClient:
    def __init__(self, opener=None, sleep=time.sleep, telegram_factory=None):
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.telegram_factory = telegram_factory or http.client.HTTPSConnection
        proxies = urllib.request.getproxies()
        self.direct_telegram = telegram_factory is not None or (opener is None and not any(
            proxies.get(key) for key in ('http', 'https', 'all')))

    def _telegram(self, request, data, timeout):
        """Retry only failed TCP/TLS setup, before any HTTP request bytes exist.

        Once request() starts, a failed POST is ambiguous and never retried here.
        Proxy-configured deployments keep urllib rather than bypassing proxies.
        """
        target = urllib.parse.urlsplit(request.full_url)
        deadline = time.monotonic() + timeout
        for attempt in range(3):
            conn = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {'error': 'TimeoutError', 'not_sent': True}
                conn = self.telegram_factory(target.hostname, target.port or 443, timeout=remaining)
                conn.connect()  # Explicitly separate TLS from HTTP/body transmission.
            except Exception as error:
                if conn:
                    conn.close()
                if attempt == 2 or time.monotonic() >= deadline:
                    return {'error': type(error).__name__, 'not_sent': True}
                self.sleep(min(.2 * (attempt + 1), max(0, deadline - time.monotonic())))
                continue
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {'error': 'TimeoutError', 'not_sent': True}
                conn.sock.settimeout(remaining)
                path = target.path + ('?' + target.query if target.query else '')
                conn.request(request.get_method(), path, body=data, headers=dict(request.header_items()))
                response = conn.getresponse()
                raw = response.read().decode()
                if response.status >= 400:
                    return {'error': response.status, 'body': raw[:300]}
                return json.loads(raw) if raw.strip() else {}
            except Exception as error:
                return {'error': type(error).__name__}  # No POST retry after request().
            finally:
                conn.close()

    def __call__(self, url, method="GET", body=None, auth=None, timeout=45):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if auth:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            request.add_header("Authorization", "Basic " + token)
        target = urllib.parse.urlsplit(url)
        if self.direct_telegram and target.scheme == 'https' and target.hostname == 'api.telegram.org':
            return self._telegram(request, data, timeout)
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

    def post_bytes(self, url, body, content_type, timeout=60):
        """Multipart uses the same explicit pre-HTTP Telegram connection guard."""
        request = urllib.request.Request(url, data=body, method='POST')
        request.add_header('Content-Type', content_type)
        target = urllib.parse.urlsplit(url)
        if self.direct_telegram and target.scheme == 'https' and target.hostname == 'api.telegram.org':
            return self._telegram(request, body, timeout)
        try:
            with self.opener(request, timeout=timeout) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as error:
            try:
                return {'error': error.code, 'body': error.read().decode()[:300]}
            finally:
                error.close()
        except Exception as error:
            return {'error': type(error).__name__}
