"""KreamPacer 단위 테스트 — mock 시계로 결정적 (조각 2-0).

FakeClock + fake sleep_fn(시계를 sleep 시간만큼 전진) 조합으로 실제 대기 없이
간격 하한/상한을 검증한다. tests/test_call_throttle.py 관례와 동일.
"""

from __future__ import annotations

import asyncio

import pytest

from src.core.kream_pacer import (
    JITTER_MAX_SEC,
    MIN_INTERVAL_SEC,
    KreamPacer,
    get_pacer,
    reset_pacer,
)


class FakeClock:
    """수동 전진 시계."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _fake_sleep(clock: FakeClock):
    """sleep_fn 자리에 주입 — 실제로 기다리지 않고 시계만 전진시킨다."""

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)

    return _sleep


async def test_first_call_no_wait():
    clock = FakeClock()
    pacer = KreamPacer(time_fn=clock, sleep_fn=_fake_sleep(clock), rand_fn=lambda a, b: 0.0)
    waited = await pacer.wait_turn()
    assert waited == 0.0
    assert pacer.wait_count == 1


async def test_min_interval_enforced_lower_bound():
    """지터 고정 0 → 두 호출 사이 간격이 정확히 하한(2.5s)."""
    clock = FakeClock()
    pacer = KreamPacer(time_fn=clock, sleep_fn=_fake_sleep(clock), rand_fn=lambda a, b: 0.0)
    await pacer.wait_turn()
    t1 = clock.now
    waited = await pacer.wait_turn()
    t2 = clock.now
    assert t2 - t1 == pytest.approx(MIN_INTERVAL_SEC)
    assert waited == pytest.approx(MIN_INTERVAL_SEC)
    assert t2 - t1 >= 2.5


async def test_jitter_upper_bound():
    """지터 고정 최대치(1.0s) → 간격이 상한(3.5s)을 넘지 않는다."""
    clock = FakeClock()
    pacer = KreamPacer(
        time_fn=clock, sleep_fn=_fake_sleep(clock), rand_fn=lambda a, b: JITTER_MAX_SEC
    )
    await pacer.wait_turn()
    t1 = clock.now
    await pacer.wait_turn()
    t2 = clock.now
    assert t2 - t1 == pytest.approx(MIN_INTERVAL_SEC + JITTER_MAX_SEC)
    assert t2 - t1 <= 3.5


async def test_no_extra_wait_when_natural_elapsed_already_sufficient():
    """호출 사이 실제 작업이 오래 걸려 이미 간격을 채웠으면 추가 대기 없음."""
    clock = FakeClock()
    pacer = KreamPacer(time_fn=clock, sleep_fn=_fake_sleep(clock), rand_fn=lambda a, b: 0.0)
    await pacer.wait_turn()
    clock.advance(5.0)  # 다른 작업으로 5초 자연 경과 (sleep_fn 을 거치지 않음)
    waited = await pacer.wait_turn()
    assert waited == 0.0


async def test_concurrency_one_serializes_three_calls():
    """asyncio.Lock 이 동시 진입한 3개 호출을 순차적으로 간격 유지시킨다."""
    clock = FakeClock()
    pacer = KreamPacer(time_fn=clock, sleep_fn=_fake_sleep(clock), rand_fn=lambda a, b: 0.0)

    results = await asyncio.gather(*(pacer.wait_turn() for _ in range(3)))

    assert len(results) == 3
    assert pacer.wait_count == 3
    # 첫 호출은 대기 0, 이후 두 번은 하한(2.5s)씩 대기 — 총 경과 5.0s
    assert clock.now == pytest.approx(2 * MIN_INTERVAL_SEC)


def test_get_pacer_singleton_and_reset():
    reset_pacer()
    p1 = get_pacer()
    p2 = get_pacer()
    assert p1 is p2
    reset_pacer()
    p3 = get_pacer()
    assert p3 is not p1
    reset_pacer()
