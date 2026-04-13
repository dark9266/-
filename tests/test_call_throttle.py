"""CallThrottle 단위 테스트 — 시계 주입으로 결정적."""

from __future__ import annotations

import pytest

from src.core.call_throttle import CallThrottle


class FakeClock:
    """수동 전진 시계."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_initial_burst_full():
    clock = FakeClock()
    t = CallThrottle(rate_per_min=60, burst=3, time_fn=clock)
    assert await t.acquire() is True
    assert await t.acquire() is True
    assert await t.acquire() is True
    s = t.stats()
    assert s["acquired_total"] == 3
    assert s["denied_total"] == 0


async def test_acquire_denied_when_empty():
    clock = FakeClock()
    t = CallThrottle(rate_per_min=60, burst=2, time_fn=clock)
    assert await t.acquire() is True
    assert await t.acquire() is True
    assert await t.acquire() is False
    s = t.stats()
    assert s["acquired_total"] == 2
    assert s["denied_total"] == 1


async def test_refill_after_time_passes():
    clock = FakeClock()
    # 60/min = 1/sec
    t = CallThrottle(rate_per_min=60, burst=2, time_fn=clock)
    await t.acquire()
    await t.acquire()
    assert await t.acquire() is False
    clock.advance(1.5)  # 1.5 토큰 리필
    assert await t.acquire() is True
    # 0.5 토큰 남음 → 다음 acquire 실패
    assert await t.acquire() is False
    clock.advance(10)  # 충분히 → burst까지만
    assert await t.acquire() is True
    assert await t.acquire() is True
    assert await t.acquire() is False  # burst=2 캡


async def test_acquire_wait_timeout_on_stuck_clock():
    clock = FakeClock()
    t = CallThrottle(rate_per_min=60, burst=1, time_fn=clock)
    await t.acquire()
    # 시계 전진 없음 → 즉시 timeout 실패
    ok = await t.acquire_wait(timeout=0.5)
    assert ok is False
    s = t.stats()
    assert s["denied_total"] >= 1


async def test_acquire_wait_weight_exceeds_burst():
    clock = FakeClock()
    t = CallThrottle(rate_per_min=60, burst=3, time_fn=clock)
    with pytest.raises(ValueError):
        await t.acquire_wait(weight=5, timeout=10)


async def test_acquire_weight_exceeds_burst():
    t = CallThrottle(rate_per_min=60, burst=3)
    with pytest.raises(ValueError):
        await t.acquire(weight=5)


async def test_acquire_wait_woken_by_wake_waiters():
    """주입 시계라도 wake_waiters() 호출로 재검사 → 토큰 있으면 통과."""
    import asyncio as _asyncio

    clock = FakeClock()
    t = CallThrottle(rate_per_min=60, burst=1, time_fn=clock)
    await t.acquire()  # 소진
    # acquire_wait 는 대기 상태 → 시계 전진 + wake
    task = _asyncio.create_task(t.acquire_wait(timeout=5.0))
    await _asyncio.sleep(0)  # 태스크 진입
    clock.advance(2.0)  # 충분한 토큰
    t.wake_waiters()
    ok = await task
    assert ok is True


async def test_update_rate_takes_effect():
    clock = FakeClock()
    t = CallThrottle(rate_per_min=60, burst=1, time_fn=clock)
    await t.acquire()
    t.update_rate(600)  # 10/sec
    clock.advance(0.2)  # 2 토큰 → burst=1로 캡
    assert await t.acquire() is True
    s = t.stats()
    assert s["rate_per_min"] == 600


async def test_update_rate_invalid():
    t = CallThrottle(rate_per_min=60, burst=1)
    with pytest.raises(ValueError):
        t.update_rate(0)
    with pytest.raises(ValueError):
        t.update_rate(-5)


async def test_init_invalid():
    with pytest.raises(ValueError):
        CallThrottle(rate_per_min=0, burst=1)
    with pytest.raises(ValueError):
        CallThrottle(rate_per_min=10, burst=0)


async def test_acquire_invalid_weight():
    t = CallThrottle(rate_per_min=60, burst=2)
    with pytest.raises(ValueError):
        await t.acquire(weight=0)
    with pytest.raises(ValueError):
        await t.acquire_wait(weight=-1)


async def test_stats_shape():
    clock = FakeClock()
    t = CallThrottle(rate_per_min=120, burst=4, time_fn=clock)
    await t.acquire()
    s = t.stats()
    assert set(s.keys()) == {
        "tokens",
        "rate_per_min",
        "burst",
        "acquired_total",
        "denied_total",
    }
    assert s["burst"] == 4
    assert s["rate_per_min"] == 120
    assert s["acquired_total"] == 1


async def test_weighted_acquire():
    clock = FakeClock()
    t = CallThrottle(rate_per_min=60, burst=5, time_fn=clock)
    assert await t.acquire(weight=3) is True
    assert await t.acquire(weight=2) is True
    assert await t.acquire(weight=1) is False
    clock.advance(1.0)  # 1 토큰
    assert await t.acquire(weight=1) is True
