"""전역 KREAM 백그라운드 페이서 — 동시성 1, 요청 간 최소 간격 강제 (조각 2-0).

목적:
    2-1(bootstrap)~2-4(라이브 배치) 등 모든 백그라운드 크림 호출이 반드시 이
    페이서를 통과하게 만들어, caller별 개별 sleep·이중 딜레이 없이 한 계층에서만
    호출 간격을 제어한다. 재시도도 예외 없이 이 경로를 통과해야 페이서·쿼터
    우회가 불가능해진다 (`src.core.kream_budget.acquire_background` 참조).

간격: `min_interval(기본 2.5s) + uniform(0, jitter_max(기본 1.0s))` → 하한 2.5s,
상한 3.5s.

스코프 (오케스트레이터 결정):
    프로세스 내 전역 asyncio 싱글턴이다. 크로스 프로세스 조율은 이번 조각
    스코프 밖 — standalone 배치(2-1~2-4 스크립트)를 실행하는 동안에는
    다른 크림 백그라운드 루프(스케줄러 등)를 **동시에 띄우지 말 것**.
    현재 봇은 정지 상태라 단순하지만, 봇을 다시 띄운 뒤 배치를 병행 실행하면
    이 페이서가 프로세스 경계를 넘어 공유되지 않아 간격 보장이 깨진다.

테스트 결정성: `time_fn`/`sleep_fn`/`rand_fn` 주입 (tests/test_call_throttle.py 의
FakeClock 관례와 동일).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

MIN_INTERVAL_SEC: float = 2.5
JITTER_MAX_SEC: float = 1.0


class KreamPacer:
    """asyncio 전역 싱글턴 페이서. 동시성 1, 요청 시작 간격 하한 강제."""

    def __init__(
        self,
        min_interval: float = MIN_INTERVAL_SEC,
        jitter_max: float = JITTER_MAX_SEC,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if jitter_max < 0:
            raise ValueError("jitter_max must be >= 0")
        self._min_interval = min_interval
        self._jitter_max = jitter_max
        self._time_fn = time_fn
        self._sleep_fn = sleep_fn
        self._rand_fn = rand_fn
        self._lock = asyncio.Lock()
        self._last_call_at: float | None = None
        self._wait_count: int = 0

    async def wait_turn(self) -> float:
        """다음 호출 차례를 확보할 때까지 대기하고, 실제 대기 시간(초)을 반환.

        동시성 1: 시각 계산 → sleep → 타임스탬프 갱신 전체가 lock 보유 구간 안에
        있어, 동시에 여러 호출자가 진입해도 한 번에 하나씩만 차례를 받는다.
        재시도를 포함한 모든 호출이 이 메서드를 다시 통과해야 간격이 보장된다.
        """
        async with self._lock:
            now = self._time_fn()
            waited = 0.0
            if self._last_call_at is not None:
                elapsed = now - self._last_call_at
                required = self._min_interval + self._rand_fn(0.0, self._jitter_max)
                if elapsed < required:
                    waited = required - elapsed
                    await self._sleep_fn(waited)
            self._last_call_at = self._time_fn()
            self._wait_count += 1
            return waited

    @property
    def wait_count(self) -> int:
        """지금까지 wait_turn 이 호출된 총 횟수 (쿼터 소비 카운터로 재사용 가능)."""
        return self._wait_count


_default_pacer: KreamPacer | None = None


def get_pacer() -> KreamPacer:
    """프로세스 전역 싱글턴 페이서.

    standalone 배치 실행 중에는 다른 백그라운드 크림 루프와 동시 실행 금지
    (모듈 docstring 참조 — 현재 봇 정지 상태 전제).
    """
    global _default_pacer
    if _default_pacer is None:
        _default_pacer = KreamPacer()
    return _default_pacer


def reset_pacer() -> None:
    """테스트 전용 — 싱글턴 리셋 (프로세스 재기동 시뮬레이션)."""
    global _default_pacer
    _default_pacer = None
