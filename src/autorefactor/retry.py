from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

RETRYABLE_HTTP_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 8
DEFAULT_BACKOFF_BASE = 1.8
DEFAULT_BACKOFF_CAP_SEC = 60.0


def is_retryable_http(status: int) -> bool:
    return status in RETRYABLE_HTTP_STATUS


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delta = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError, OverflowError):
        return None


def backoff_seconds(
    attempt: int,
    *,
    status_code: int | None = None,
    retry_after: str | None = None,
    base: float = DEFAULT_BACKOFF_BASE,
    cap: float = DEFAULT_BACKOFF_CAP_SEC,
    jitter: bool = True,
) -> float:
    hinted = parse_retry_after(retry_after)
    if hinted is not None:
        delay = min(cap, hinted)
    else:
        delay = min(cap, base**attempt)
        if status_code == 429:
            # Stronger backoff under rate limits
            delay = min(cap, max(delay * 2.0, 5.0 * (attempt + 1)))

    if jitter:
        delay *= 0.75 + random.random() * 0.5
    return min(cap, max(0.25, delay))


def retryable_runtime_error(exc: BaseException) -> bool:
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc)
    for code in RETRYABLE_HTTP_STATUS:
        if re.search(rf"\bHTTP {code}\b", message):
            return True
    lowered = message.lower()
    return "connection error" in lowered or "timed out" in lowered
