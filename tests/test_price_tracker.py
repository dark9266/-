"""가격 변동 감지 단위 테스트."""

import os
import tempfile

import pytest

from src.models.database import Database
from src.models.product import KreamProduct, KreamSizePrice, RetailProduct, RetailSizeInfo, Signal
from src.price_tracker import PriceChange, PriceTracker


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path=db_path)
    await database.connect()
    yield database
    await database.close()
    os.unlink(db_path)


@pytest.fixture
def tracker(db):
    return PriceTracker(db)


def _make_kream(prices: list[tuple[str, int]], product_id: str = "12345") -> KreamProduct:
    return KreamProduct(
        product_id=product_id,
        name="테스트 상품",
        model_number="XX-100",
        volume_7d=10,
        size_prices=[
            KreamSizePrice(size=s, sell_now_price=p) for s, p in prices
        ],
    )


class TestPriceTracker:
    async def test_first_fetch_no_changes(self, tracker: PriceTracker):
        """최초 수집 시 변동 없음."""
        kream = _make_kream([("260", 130000), ("270", 120000)])
        changes = await tracker.check_kream_price_changes(kream)
        assert changes == []

    async def test_detect_price_increase(self, tracker: PriceTracker, db: Database):
        """가격 상승 감지."""
        # 1차 수집
        kream1 = _make_kream([("260", 130000)])
        await tracker.check_kream_price_changes(kream1)

        # 2차 수집 — 가격 상승
        kream2 = _make_kream([("260", 140000)])
        changes = await tracker.check_kream_price_changes(kream2)

        assert len(changes) == 1
        assert changes[0].old_price == 130000
        assert changes[0].new_price == 140000
        assert changes[0].price_diff == 10000

    async def test_detect_price_decrease(self, tracker: PriceTracker):
        """가격 하락 감지."""
        kream1 = _make_kream([("260", 130000)])
        await tracker.check_kream_price_changes(kream1)

        kream2 = _make_kream([("260", 118000)])
        changes = await tracker.check_kream_price_changes(kream2)

        assert len(changes) == 1
        assert changes[0].price_diff == -12000

    async def test_ignore_small_change(self, tracker: PriceTracker):
        """소폭 변동(1000원 미만, 1% 미만)은 무시."""
        kream1 = _make_kream([("260", 130000)])
        await tracker.check_kream_price_changes(kream1)

        kream2 = _make_kream([("260", 130500)])
        changes = await tracker.check_kream_price_changes(kream2)
        assert changes == []

    async def test_profit_change_with_retail(self, tracker: PriceTracker):
        """리테일 가격이 있을 때 수익 변동도 계산."""
        kream1 = _make_kream([("260", 130000)])
        await tracker.check_kream_price_changes(kream1, retail_price_map={"260": 80000})

        kream2 = _make_kream([("260", 150000)])
        changes = await tracker.check_kream_price_changes(kream2, retail_price_map={"260": 80000})

        assert len(changes) == 1
        assert changes[0].new_profit > changes[0].old_profit
        assert changes[0].profit_diff > 0

    async def test_retail_price_change(self, tracker: PriceTracker):
        """리테일 가격 변동 감지."""
        retail1 = RetailProduct(
            source="musinsa", product_id="m1", name="테스트", model_number="XX-100",
            sizes=[RetailSizeInfo(size="260", price=90000, original_price=110000)],
        )
        await tracker.check_retail_price_changes(retail1)

        retail2 = RetailProduct(
            source="musinsa", product_id="m1", name="테스트", model_number="XX-100",
            sizes=[RetailSizeInfo(size="260", price=80000, original_price=110000)],
        )
        changes = await tracker.check_retail_price_changes(retail2)

        assert len(changes) == 1
        assert changes[0].price_diff == -10000
        assert changes[0].change_source == "musinsa"


class TestFilterSignificantChanges:
    def test_filter_by_profit_diff(self):
        tracker = PriceTracker.__new__(PriceTracker)
        changes = [
            PriceChange(
                product_name="A", model_number="X", kream_product_id="1",
                change_source="kream", size="260",
                old_price=130000, new_price=140000, price_diff=10000,
                old_profit=20000, new_profit=30000, profit_diff=10000,
                old_signal=Signal.BUY, new_signal=Signal.STRONG_BUY,
                detected_at=None,
            ),
            PriceChange(
                product_name="B", model_number="Y", kream_product_id="2",
                change_source="kream", size="270",
                old_price=130000, new_price=131500, price_diff=1500,
                old_profit=20000, new_profit=20500, profit_diff=500,
                old_signal=Signal.BUY, new_signal=Signal.BUY,
                detected_at=None,
            ),
        ]

        result = tracker.filter_significant_changes(changes, min_profit_diff=3000)
        assert len(result) == 1
        assert result[0].product_name == "A"

    def test_filter_by_signal_change(self):
        """시그널 변경은 수익 변동이 적어도 포함."""
        tracker = PriceTracker.__new__(PriceTracker)
        changes = [
            PriceChange(
                product_name="C", model_number="Z", kream_product_id="3",
                change_source="kream", size="260",
                old_price=130000, new_price=131000, price_diff=1000,
                old_profit=14000, new_profit=15500, profit_diff=1500,
                old_signal=Signal.WATCH, new_signal=Signal.BUY,
                detected_at=None,
            ),
        ]

        result = tracker.filter_significant_changes(changes, min_profit_diff=3000)
        assert len(result) == 1  # 시그널 변경으로 포함됨
