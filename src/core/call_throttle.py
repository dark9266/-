"""변동 기반 크림 호출 토큰 버킷 (Phase 2.3b · 축 6).

목적: `kream_budget`의 일일 하드 캡과 직교한 **소프트 레이트 리미터**.
푸시 어댑터가 덤프한 카탈로그에서 변경 감지된 후보만 크림 조회로
이어지도록, 변동률·후보 밀도에 따라 동적으로 호출률을 조절한다.

설계 원칙:
- 순수 asyncio, 외부 의존 없음
- 백그라운드 리필 태스크 없음 — `monotonic()` 기반 lazy 리필
- `time_fn` 주입으로 테스트 결정성 확보
- `acquire()`는 non-blocking, `acquire_wait()`는 timeout 기반 대기
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class CallThrottle:
    """토큰 버킷 기반 비동기 호출 스로틀러.

    Parameters
    ----------
    rate_per_min:
        분당 토큰 리필률. 동적 업데이트 가능.
    burst:
        버킷 최대 용량 (초기 토큰 수).
    time_fn:
        시계 함수. 테스트 시 주입 가능. 기본 `time.monotonic`.
    """

    def __init__(
        self,
        rate_per_min: float,
        burst: int,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self._rate_per_min: float = float(rate_per_min)
        self._burst: int = int(burst)
        self._time_fn = time_fn
        self._tokens: float = float(burst)
        self._last_refill: float = time_fn()
        self._lock = asyncio.Lock()
        self._acquired_total: int = 0
        self._denied_total: int = 0

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------
    def _refill(self) -> None:
        """경과 시간에 따라 토큰 리필 (lock 보유 상태에서 호출)."""
        now = self._time_fn()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        per_sec = self._rate_per_min / 60.0
        self._tokens = min(float(self._burst), self._tokens + elapsed * per_sec)
        self._last_refill = now

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    async def acquire(self, weight: int = 1) -> bool:
        """토큰 즉시 차감 시도. 없으면 False (블로킹 안 함)."""
        if weight <= 0:
            raise ValueError("weight must be > 0")
        async with self._lock:
            self._refill()
            if self._tokens >= weight:
                self._tokens -= weight
                self._acquired_total += 1
                return True
            self._denied_total += 1
            return False

    async def acquire_wait(
        self,
        weight: int = 1,
        timeout: float | None = None,
    ) -> bool:
        """토큰 확보까지 대기. timeout 초과 시 False."""
        if weight <= 0:
            raise ValueError("weight must be > 0")
        if weight > self._burst:
            # 버킷 용량보다 큰 weight는 영원히 채울 수 없음
            async with self._lock:
                self._denied_total += 1
            return False

        deadline: float | None = None
        if timeout is not None:
            deadline = self._time_fn() + timeout

        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    self._acquired_total += 1
                    return True
                # 부족분 → 대기 시간 산정
                deficit = weight - self._tokens
                per_sec = self._rate_per_min / 60.0
                wait_for = deficit / per_sec if per_sec > 0 else float("inf")

            if deadline is not None:
                remaining = deadline - self._time_fn()
                if remaining <= 0:
                    async with self._lock:
                        self._denied_total += 1
                    return False
                wait_for = min(wait_for, remaining)

            # 시계가 주입된 경우 sleep도 짧게 — 테스트는 시계를 직접 전진시킴
            if self._time_fn is time.monotonic:
                await asyncio.sleep(wait_for)
            else:
                # 결정적 테스트: 한 번 양보 후 재검사
                await asyncio.sleep(0)
                # 시계가 진전되지 않으면 무한 루프 → 즉시 실패 처리
                async with self._lock:
                    self._refill()
                    if self._tokens >= weight:
                        self._tokens -= weight
                        self._acquired_total += 1
                        return True
                    if deadline is not None and self._time_fn() >= deadline:
                        self._denied_total += 1
                        return False
                    # 시계 정지 상태 → 호출측이 시계를 전진시키지 않으면 실패
                    self._denied_total += 1
                    return False

    def update_rate(self, rate_per_min: float) -> None:
        """동적 리필률 조정. 변동률 높을 때 ↑, 한산할 때 ↓."""
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be > 0")
        # 다음 _refill 호출 시 새 비율 적용. 누적된 토큰은 보존.
        self._rate_per_min = float(rate_per_min)

    def stats(self) -> dict:
        """현재 상태 스냅샷."""
        return {
            "tokens": self._tokens,
            "rate_per_min": self._rate_per_min,
            "burst": self._burst,
            "acquired_total": self._acquired_total,
            "denied_total": self._denied_total,
        }
