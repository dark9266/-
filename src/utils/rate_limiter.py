"""비동기 요청 속도 제한기."""

import asyncio
import time
from contextlib import asynccontextmanager


class AsyncRateLimiter:
    """도메인별 동시 요청 제한 + 최소 간격.

    max_concurrent개까지 동시 실행, 각 요청 사이 min_interval초 간격.
    """

    def __init__(self, max_concurrent: int = 10, min_interval: float = 0.5):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    @asynccontextmanager
    async def acquire(self):
        async with self._sem:
            async with self._lock:
                now = time.monotonic()
                wait = self._interval - (now - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            yield
