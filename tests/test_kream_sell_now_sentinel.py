"""크림 sell_now 센티넬 차단 회귀 테스트.

크림은 bid_count=0 일 때 placeholder 가격(9,990,000 / 5,500,000 등)을
sell_now_price 로 반환한다. 이를 실제 호가로 오인하면 허위 차익 알림이 발생한다.
`KREAM_SELL_NOW_MAX` 이상 가격은 None 으로 드롭되어야 한다.
"""
from __future__ import annotations

import pytest

from src.crawlers.kream_delta_client import (
    KREAM_SELL_NOW_MAX,
    KreamDeltaClient,
    build_snapshot_fn,
)


class _FakeClient:
    def __init__(self, snap: dict):
        self._snap = snap

    async def get_snapshot(self, pid: int):
        return self._snap


@pytest.mark.asyncio
async def test_all_sentinel_returns_none():
    """모든 사이즈가 센티넬이면 → None (알림 차단)."""
    snap = {
        "volume_7d": 0,
        "size_prices": [
            {"size": "270", "sell_now_price": 9_990_000, "buy_now_price": 9_990_000},
            {"size": "275", "sell_now_price": 9_990_000, "buy_now_price": 9_990_000},
        ],
    }
    fn = build_snapshot_fn(_FakeClient(snap))  # type: ignore[arg-type]
    result = await fn(36, "ALL")
    assert result is None


@pytest.mark.asyncio
async def test_mixed_sentinel_returns_legit_min():
    """센티넬 섞여 있어도 정상 가격만 남고 MIN 을 반환."""
    snap = {
        "volume_7d": 10,
        "size_prices": [
            {"size": "270", "sell_now_price": 9_990_000, "buy_now_price": 9_990_000},
            {"size": "275", "sell_now_price": 250_000, "buy_now_price": 240_000},
            {"size": "280", "sell_now_price": 300_000, "buy_now_price": 290_000},
        ],
    }
    fn = build_snapshot_fn(_FakeClient(snap))  # type: ignore[arg-type]
    result = await fn(36, "ALL")
    assert result is not None
    assert result["sell_now_price"] == 250_000
    assert len(result["size_prices"]) == 2


@pytest.mark.asyncio
async def test_specific_size_sentinel_returns_none():
    """특정 사이즈 요청했는데 그 사이즈가 센티넬이면 → None."""
    snap = {
        "volume_7d": 5,
        "size_prices": [
            {"size": "270", "sell_now_price": 9_990_000, "buy_now_price": 9_990_000},
            {"size": "275", "sell_now_price": 250_000, "buy_now_price": 240_000},
        ],
    }
    fn = build_snapshot_fn(_FakeClient(snap))  # type: ignore[arg-type]
    result = await fn(36, "270")
    assert result is None


@pytest.mark.asyncio
async def test_just_below_ceiling_is_legit():
    """상한 바로 아래(7,999,999)는 정상 가격으로 허용."""
    legit = KREAM_SELL_NOW_MAX - 1
    snap = {
        "volume_7d": 3,
        "size_prices": [
            {"size": "270", "sell_now_price": legit, "buy_now_price": legit - 1000},
        ],
    }
    fn = build_snapshot_fn(_FakeClient(snap))  # type: ignore[arg-type]
    result = await fn(22857, "270")
    assert result is not None
    assert result["sell_now_price"] == legit


def test_sentinel_constant_is_reasonable():
    """상한이 합리적 범위 — 정상 고가 상품 초과, 크림 센티넬 미만."""
    assert 5_000_000 < KREAM_SELL_NOW_MAX < 9_000_000
