"""스케줄러 단위 테스트."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.product import (
    KreamProduct,
    KreamSizePrice,
    ProfitOpportunity,
    RetailProduct,
    Signal,
    SizeProfitResult,
)
from src.scheduler import Scheduler
from src.utils.resilience import ErrorAggregator


# --- 헬퍼 ---


def _make_opportunity(
    product_id: str = "1",
    name: str = "테스트 신발",
    model_number: str = "TEST-001",
    best_profit: int = 35000,
    signal: Signal = Signal.STRONG_BUY,
) -> ProfitOpportunity:
    kp = KreamProduct(
        product_id=product_id,
        name=name,
        model_number=model_number,
        volume_7d=15,
        volume_30d=50,
    )
    return ProfitOpportunity(
        kream_product=kp,
        retail_products=[],
        size_profits=[],
        best_profit=best_profit,
        best_roi=20.0,
        signal=signal,
    )


def _make_mock_bot():
    """테스트용 mock KreamBot."""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.log_to_channel = AsyncMock()
    bot.send_profit_alert = AsyncMock()
    bot.send_auto_scan_alert = AsyncMock()
    bot.send_price_change_alert = AsyncMock()
    bot.get_channel = MagicMock(return_value=MagicMock())
    bot.daily_stats = {
        "scan_count": 0,
        "product_count": 0,
        "opportunity_count": 0,
        "opportunities": [],
    }
    bot.scanner = MagicMock()
    bot.scanner.scan_all_keywords = AsyncMock()
    bot.scanner.scan_single_product = AsyncMock()
    bot.scanner.price_tracker = MagicMock()
    bot.scanner.price_tracker.check_kream_price_changes = AsyncMock(return_value=[])
    bot.scanner.price_tracker.filter_significant_changes = MagicMock(return_value=[])
    bot.scanner.scan_cache = MagicMock()
    # 2티어 아키텍처 mock
    bot.tier1_scanner = None
    bot.tier2_monitor = None
    bot.watchlist = None
    return bot


# --- Scheduler 테스트 ---


class TestSchedulerInit:
    """스케줄러 초기화 테스트."""

    def test_scheduler_creates(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)
        assert scheduler.bot is bot

    def test_scheduler_has_three_loops(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)
        assert hasattr(scheduler, "tier1_loop")
        assert hasattr(scheduler, "tier2_loop")
        assert hasattr(scheduler, "daily_report")


# --- ErrorAggregator 테스트 ---


class TestErrorAggregator:
    """에러 집계기 테스트."""

    def test_add_error(self):
        agg = ErrorAggregator()
        agg.add("test_source", ValueError("테스트 에러"))
        assert len(agg._errors) == 1
        assert agg._errors[0]["source"] == "test_source"

    def test_max_errors_limit(self):
        agg = ErrorAggregator(max_errors=3)
        for i in range(5):
            agg.add("src", ValueError(f"err {i}"))
        assert len(agg._errors) == 3
        assert agg._errors[0]["error"] == "err 2"

    def test_get_recent(self):
        agg = ErrorAggregator()
        agg.add("src", ValueError("recent"))
        # 과거 에러 수동 추가
        agg._errors.insert(0, {
            "source": "old",
            "error": "old error",
            "traceback": "",
            "time": datetime.now() - timedelta(hours=2),
        })

        recent = agg.get_recent(minutes=30)
        assert len(recent) == 1
        assert recent[0]["error"] == "recent"

    def test_get_summary_no_errors(self):
        agg = ErrorAggregator()
        summary = agg.get_summary()
        assert "에러 없음" in summary

    def test_get_summary_with_errors(self):
        agg = ErrorAggregator()
        agg.add("scanner", ValueError("err1"))
        agg.add("scanner", ValueError("err2"))
        agg.add("crawler", ValueError("err3"))

        summary = agg.get_summary(minutes=60)
        assert "3건" in summary
        assert "scanner: 2건" in summary
        assert "crawler: 1건" in summary

    def test_clear(self):
        agg = ErrorAggregator()
        agg.add("src", ValueError("err"))
        agg.clear()
        assert len(agg._errors) == 0


# --- 일일 통계 리셋 테스트 ---


class TestDailyStats:
    """일일 통계 관련 테스트."""

    def test_stats_accumulation(self):
        bot = _make_mock_bot()
        stats = bot.daily_stats

        stats["scan_count"] += 1
        stats["product_count"] += 10
        stats["opportunity_count"] += 3
        stats["opportunities"].extend([
            _make_opportunity(product_id="1"),
            _make_opportunity(product_id="2"),
            _make_opportunity(product_id="3"),
        ])

        assert stats["scan_count"] == 1
        assert stats["product_count"] == 10
        assert stats["opportunity_count"] == 3
        assert len(stats["opportunities"]) == 3

    def test_stats_reset(self):
        bot = _make_mock_bot()
        bot.daily_stats["scan_count"] = 48
        bot.daily_stats["product_count"] = 500
        bot.daily_stats["opportunity_count"] = 25
        bot.daily_stats["opportunities"] = [_make_opportunity()]

        # 리셋 (일일 리포트 후 실행되는 로직)
        bot.daily_stats = {
            "scan_count": 0,
            "product_count": 0,
            "opportunity_count": 0,
            "opportunities": [],
        }

        assert bot.daily_stats["scan_count"] == 0
        assert bot.daily_stats["opportunities"] == []

    def test_top_products_sorting(self):
        """일일 리포트 TOP 5 정렬 검증."""
        opportunities = [
            _make_opportunity(product_id="1", best_profit=10000),
            _make_opportunity(product_id="2", best_profit=50000),
            _make_opportunity(product_id="3", best_profit=30000),
            _make_opportunity(product_id="4", best_profit=80000),
            _make_opportunity(product_id="5", best_profit=5000),
            _make_opportunity(product_id="6", best_profit=45000),
        ]

        top = sorted(opportunities, key=lambda o: -o.best_profit)[:5]

        assert len(top) == 5
        assert top[0].best_profit == 80000
        assert top[1].best_profit == 50000
        assert top[4].best_profit == 10000
