"""Tier2 ROI 캡 제거 + best_profit 오염 회귀 테스트.

배경:
- 이전 코드는 ROI > 100% 사이즈를 알림에서 제외했지만, best_profit/best_roi
  갱신은 필터 *전*에 일어나서, 필터된 사이즈가 best로 보고되는 오염 버그가 있었다.
- 또한 Nike 하이프 상품처럼 ROI 100~200% 구간에서도 정상 차익이 빈번한데
  ROI 캡이 이를 통째로 차단했다. 5배 안전망(line 130)이 이미 진짜 오매칭을 막는다.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.models.product import KreamProduct, KreamSizePrice
from src.tier2_monitor import Tier2Monitor, Tier2Result
from src.watchlist import Watchlist, WatchlistItem


def _make_item(model: str = "TEST123", source_price: int = 80_000) -> WatchlistItem:
    return WatchlistItem(
        kream_product_id="999",
        model_number=model,
        kream_name="Test Sneaker",
        musinsa_product_id="m999",
        musinsa_price=source_price,
        kream_price=0,
        gap=0,
        source="musinsa",
        source_product_id="m999",
        source_price=source_price,
        source_url="https://example.com/m999",
    )


def _make_kream_product(size_prices: list[KreamSizePrice]) -> KreamProduct:
    return KreamProduct(
        product_id="999",
        name="Test Sneaker",
        model_number="TEST123",
        size_prices=size_prices,
    )


@pytest.fixture
def monitor(tmp_path):
    wl_path = tmp_path / "watchlist.json"
    wl = Watchlist(path=wl_path)
    alert_cb = AsyncMock()
    return Tier2Monitor(watchlist=wl, alert_callback=alert_cb), alert_cb


@pytest.mark.asyncio
async def test_high_roi_size_passes_through(monitor):
    """ROI 120% 정상 케이스: 캡에 막히지 않고 알림 발생."""
    mon, alert_cb = monitor
    item = _make_item(source_price=80_000)

    # sell_now 190,000 → 수수료 차감 후 net ~95k, ROI ~120%
    sizes = [KreamSizePrice(size="270", buy_now_price=200_000, sell_now_price=190_000)]
    fake_kream = _make_kream_product(sizes)

    with patch(
        "src.tier2_monitor.kream_crawler.get_full_product_info",
        new=AsyncMock(return_value=fake_kream),
    ):
        result = Tier2Result()
        await mon._check_one(item, result)

    assert alert_cb.await_count == 1, "ROI 120% 정상 차익이 캡에 막히면 안 됨"
    opportunity = alert_cb.await_args.args[0]
    assert opportunity.best_confirmed_roi > 100
    assert len(opportunity.size_profits) == 1
    assert opportunity.size_profits[0].size == "270"


@pytest.mark.asyncio
async def test_best_profit_only_from_alerted_sizes(monitor):
    """best_profit 오염 버그: 5배 필터에 막힌 사이즈가 best로 보고되면 안 됨.

    사이즈 260: sell_now 500,000 → retail*5 초과로 필터됨 (오매칭 방어)
    사이즈 270: sell_now 130,000 → ROI 약 51%, 정상 알림 후보
    → best_profit/best_roi는 270의 값이어야 함
    """
    mon, alert_cb = monitor
    item = _make_item(source_price=80_000)

    sizes = [
        KreamSizePrice(size="260", buy_now_price=520_000, sell_now_price=500_000),  # 필터됨
        KreamSizePrice(size="270", buy_now_price=135_000, sell_now_price=130_000),  # 정상
    ]
    fake_kream = _make_kream_product(sizes)

    with patch(
        "src.tier2_monitor.kream_crawler.get_full_product_info",
        new=AsyncMock(return_value=fake_kream),
    ):
        result = Tier2Result()
        await mon._check_one(item, result)

    assert alert_cb.await_count == 1
    opportunity = alert_cb.await_args.args[0]
    # 알림에 260이 들어가면 안 되고, best는 270 기반이어야 함
    sizes_in_alert = {sp.size for sp in opportunity.size_profits}
    assert "260" not in sizes_in_alert
    assert "270" in sizes_in_alert
    # 270 기준 net_profit ≈ 130k - retail 80k - fees ≈ 40k 부근
    assert 30_000 <= opportunity.best_confirmed_profit <= 50_000
