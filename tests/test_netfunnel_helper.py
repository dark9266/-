"""NetFunnelCookieCache 단위 테스트 — mock fetcher 주입."""

from __future__ import annotations

import pytest

from src.crawlers._netfunnel_helper import NetFunnelCookieCache


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_fetcher(values):
    """호출 시마다 values 리스트에서 순차 반환. 각 항목은 str | None."""
    queue = list(values)
    calls = {"n": 0}

    async def fetcher(home_url, cookie_name, user_agent, timeout_ms):
        calls["n"] += 1
        if not queue:
            return None
        return queue.pop(0)

    return fetcher, calls


@pytest.mark.asyncio
async def test_fresh_cached_value_reused_within_ttl():
    clock = _FakeClock()
    fetcher, calls = _make_fetcher(["COOKIE-A"])
    cache = NetFunnelCookieCache(
        ttl_seconds=100.0,
        cooldown_seconds=5.0,
        fetcher=fetcher,
    )
    cache._now = clock  # type: ignore[assignment]

    v1 = await cache.get()
    assert v1 == "COOKIE-A"
    clock.advance(50)
    v2 = await cache.get()
    assert v2 == "COOKIE-A"
    assert calls["n"] == 1  # 두 번째 호출은 캐시에서 바로


@pytest.mark.asyncio
async def test_expired_cookie_refetched():
    clock = _FakeClock()
    fetcher, calls = _make_fetcher(["COOKIE-A", "COOKIE-B"])
    cache = NetFunnelCookieCache(
        ttl_seconds=100.0,
        cooldown_seconds=5.0,
        fetcher=fetcher,
    )
    cache._now = clock  # type: ignore[assignment]

    assert await cache.get() == "COOKIE-A"
    clock.advance(150)  # TTL 초과
    assert await cache.get() == "COOKIE-B"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_failure_enters_cooldown_then_recovers():
    clock = _FakeClock()
    fetcher, calls = _make_fetcher([None, "COOKIE-OK"])
    cache = NetFunnelCookieCache(
        ttl_seconds=100.0,
        cooldown_seconds=20.0,
        fetcher=fetcher,
    )
    cache._now = clock  # type: ignore[assignment]

    # 첫 호출 실패 → cooldown 진입
    assert await cache.get() is None
    # 쿨다운 중 재호출 — fetcher 호출 안 됨
    clock.advance(5)
    assert await cache.get() is None
    assert calls["n"] == 1
    # 쿨다운 경과 후 재시도 → 성공
    clock.advance(20)
    assert await cache.get() == "COOKIE-OK"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_invalidate_forces_refetch_and_updates_ema():
    clock = _FakeClock()
    fetcher, calls = _make_fetcher(["COOKIE-A", "COOKIE-B"])
    cache = NetFunnelCookieCache(
        ttl_seconds=300.0,
        ttl_min=10.0,
        ttl_max=600.0,
        cooldown_seconds=1.0,
        fetcher=fetcher,
    )
    cache._now = clock  # type: ignore[assignment]

    assert await cache.get() == "COOKIE-A"
    clock.advance(60)  # 60초 살아있었음
    cache.invalidate()
    # EMA 기반 TTL 보정 적용 — 첫 관측(α=1 equivalent): 60*0.9 = 54, clamp → 54.0
    assert cache.ttl_seconds == pytest.approx(54.0, rel=1e-3)
    # 다음 get 은 재획득
    assert await cache.get() == "COOKIE-B"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_invalidate_ttl_clamped_to_min():
    clock = _FakeClock()
    fetcher, calls = _make_fetcher(["COOKIE-A"])
    cache = NetFunnelCookieCache(
        ttl_seconds=300.0,
        ttl_min=30.0,
        ttl_max=600.0,
        fetcher=fetcher,
    )
    cache._now = clock  # type: ignore[assignment]

    assert await cache.get() == "COOKIE-A"
    clock.advance(5)  # 매우 짧게 살았음
    cache.invalidate()
    # 5*0.9=4.5 → ttl_min(30) 으로 clamp
    assert cache.ttl_seconds == 30.0
