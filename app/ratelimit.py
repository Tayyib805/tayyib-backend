"""Small in-memory rate limiter for expensive endpoints.

Protects the Claude API budget from bots and scripted abuse. Counts are kept in
process memory, so they reset on deploy and are per-instance. That is fine for a
single Render instance; move to Redis if the backend ever scales horizontally.
"""

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request, status

_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()
_MAX_KEYS = 50_000


def client_ip(request: Request) -> str:
    """Return the caller's IP, honoring the proxy header Render sets."""

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check(key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    with _lock:
        if len(_hits) > _MAX_KEYS:
            _hits.clear()
        bucket = _hits[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = int(window_seconds - (now - bucket[0])) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


def rate_limit(name: str, limit: int, window_seconds: int):
    """Build a FastAPI dependency limiting each IP to `limit` calls per window."""

    async def dependency(request: Request) -> None:
        _check(f"{name}:{client_ip(request)}", limit, window_seconds)

    return dependency
