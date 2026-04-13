"""변동 기반 크림 호출 토큰 버킷 (Phase 2.3b · 축 6).

목적: `kream_budget`의 일일 하드 캡과 직교한 **소프트 레이트 리미터**.
푸시 어댑터가 덤프한 카탈로그에서 변경 감지된 후보만 크림 조회로
이어지도록, 변동률·후보 밀도에 따라 동적으로 호출률을 조절한다.

설계 원칙:
- 순수 asyncio, 외부 의존 없음
- 백그라운드 리필 태스크 없음 — `monotonic()` 기반 lazy 리필
- `time_fn` 주입으로 테스트 결정성 확보. 주입 시계든 실시계든
  `acquire_wait` 는 동일한 `asyncio.Event` wakeup 패턴을 사용 (분기 X)
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
        # acquire_wait 가 토큰을 기다리는 동안 외부에서 wakeup 시킬 수 있도록.
        # 토큰이 늘어났을 가능성이 있는 모든 시점 (refill, update_rate, wake_waiters)
        # 에서 set → clear 패턴.
        self._wakeup: asyncio.Event = asyncio.Event()
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

    def _signal_wakeup(self) -> None:
        """대기 중인 acquire_wait 를 깨운다 (테스트/update_rate/refill 후)."""
        self._wakeup.set()

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    async def acquire(self, weight: int = 1) -> bool:
        """토큰 즉시 차감 시도. 없으면 False (블로킹 안 함)."""
        if weight <= 0:
            raise ValueError("weight must be > 0")
        if weight > self._burst:
            raise ValueError("weight must be <= burst")
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
        """토큰 확보까지 대기. timeout 초과 시 False.

        실시계든 주입 시계든 동일 코드 경로:
            1) lock 안에서 refill → 충분하면 차감 후 True
            2) 부족하면 wakeup event 를 기다림 (계산된 wait_for / 남은 timeout
               중 짧은 쪽). 시계가 정지 상태면 wakeup.set() 만이 깨운다.
        주입 시계로 결정적 테스트를 하려면 `wake_waiters()` 를 호출하면 된다.
        """
        if weight <= 0:
            raise ValueError("weight must be > 0")
        if weight > self._burst:
            raise ValueError("weight must be <= burst")

        start = self._time_fn()
        deadline: float | None = None
        if timeout is not None:
            deadline = start + timeout

        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    self._acquired_total += 1
                    return True
                deficit = weight - self._tokens
                per_sec = self._rate_per_min / 60.0
                wait_for = deficit / per_sec if per_sec > 0 else float("inf")
                # event 를 다시 무장 — 이후 외부에서 set 하면 즉시 깨어남
                self._wakeup.clear()

            # 남은 timeout 계산
            if deadline is not None:
                remaining = deadline - self._time_fn()
                if remaining <= 0:
                    async with self._lock:
                        self._denied_total += 1
                    return False
                wait_for = min(wait_for, remaining)

            clock_before = self._time_fn()
            # 대기: wakeup event OR wait_for 만료.
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=wait_for)
                woken = True
            except asyncio.TimeoutError:
                woken = False

            clock_after = self._time_fn()
            # 시계가 전혀 진전되지 않았고 wakeup 도 못 받았다면 진전 없음 →
            # 주입 시계가 정지된 상태로 판정, 즉시 실패 (무한 루프 방지).
            if not woken and clock_after == clock_before:
                async with self._lock:
                    self._denied_total += 1
                return False

    def update_rate(self, rate_per_min: float) -> None:
        """동적 리필률 조정. 변동률 높을 때 ↑, 한산할 때 ↓.

        호출 시점까지의 누적 토큰을 보존하기 위해 락 안에서 _refill 후 rate 변경.
        주의: synchronous 메서드이지만 lock 은 asyncio.Lock — 여기서는
        snapshot-and-swap 방식 사용 (_refill 한 번 호출 후 rate 교체).
        rate 교체는 atomic write 라 race 위험 없음.
        """
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be > 0")
        # asyncio.Lock 은 sync 컨텍스트에서 점유 불가 → 본 메서드는 핸들러 외부에서
        # 동기 호출되는 것을 가정. 단일 이벤트 루프에서 _refill 은 점유자가 없을 때
        # 호출돼야 race-free. 보통 update_rate 는 주기적 컨트롤러가 호출하므로 OK.
        self._refill()
        self._rate_per_min = float(rate_per_min)
        self._signal_wakeup()

    def wake_waiters(self) -> None:
        """테스트/외부 트리거에서 acquire_wait 대기자를 깨운다."""
        self._signal_wakeup()

    def stats(self) -> dict:
        """현재 상태 스냅샷."""
        return {
            "tokens": self._tokens,
            "rate_per_min": self._rate_per_min,
            "burst": self._burst,
            "acquired_total": self._acquired_total,
            "denied_total": self._denied_total,
        }
