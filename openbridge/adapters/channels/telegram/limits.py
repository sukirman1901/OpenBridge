"""Conservative Telegram retry-after parsing shared by all wire paths."""
import json


def retry_after(response, default=30):
    try:
        details = response if response.get('parameters') else json.loads(response.get('body', '{}'))
        raw = (details.get('parameters') or {}).get('retry_after', default)
        delay = int(raw)
        if isinstance(raw, bool) or delay < 1 or delay != float(raw):
            return default
        return delay
    except (ValueError, TypeError, AttributeError, OverflowError):
        return default
