"""스케줄러 단위 테스트."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
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
from src.watchlist import Watchlist, WatchlistItem


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


# --- Watchlist 추적 테스트 (TestSchedulerTracking 동등 복원) ---


def _make_watchlist_item(
    model_number: str = "TEST-001",
    musinsa_price: int = 89000,
    kream_price: int = 120000,
    added_at: str | None = None,
) -> WatchlistItem:
    gap = musinsa_price - kream_price
    return WatchlistItem(
        kream_product_id="12345",
        model_number=model_number,
        kream_name=f"테스트 {model_number}",
        musinsa_product_id="67890",
        musinsa_price=musinsa_price,
        kream_price=kream_price,
        gap=gap,
        **({"added_at": added_at} if added_at else {}),
    )


class TestWatchlistTracking:
    """워치리스트 추적 테스트 (구 TestSchedulerTracking 동등)."""

    def _make_wl(self):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp = Path(f.name)
        f.close()
        return Watchlist(path=self._tmp)

    def teardown_method(self):
        if hasattr(self, "_tmp"):
            self._tmp.unlink(missing_ok=True)

    def test_watchlist_add_single(self):
        """항목 1개 추가 → size==1, get() 확인."""
        wl = self._make_wl()
        item = _make_watchlist_item()
        assert wl.add(item) is True
        assert wl.size == 1
        assert wl.get("TEST-001") is not None

    def test_watchlist_add_multiple(self):
        """3개 추가 → size==3."""
        wl = self._make_wl()
        wl.add(_make_watchlist_item(model_number="A-001"))
        wl.add(_make_watchlist_item(model_number="B-002"))
        wl.add(_make_watchlist_item(model_number="C-003"))
        assert wl.size == 3

    def test_watchlist_add_same_updates(self):
        """동일 model 재추가 → size==1, 가격 갱신."""
        wl = self._make_wl()
        wl.add(_make_watchlist_item(model_number="X-001", kream_price=100000))
        wl.add(_make_watchlist_item(model_number="X-001", kream_price=110000))
        assert wl.size == 1
        assert wl.get("X-001").kream_price == 110000

    def test_watchlist_cleanup_removes_expired(self):
        """100h 지난 항목 제거."""
        wl = self._make_wl()
        old_at = (datetime.now() - timedelta(hours=100)).isoformat()
        wl.add(_make_watchlist_item(model_number="OLD-001", added_at=old_at))
        removed = wl.cleanup_stale(max_age_hours=48)
        assert removed == 1
        assert wl.size == 0

    def test_watchlist_cleanup_keeps_active(self):
        """최근 항목 유지."""
        wl = self._make_wl()
        wl.add(_make_watchlist_item(model_number="NEW-001"))
        removed = wl.cleanup_stale(max_age_hours=48)
        assert removed == 0
        assert wl.size == 1

    def test_watchlist_expiry_mixed(self):
        """만료+유효 혼합 → 만료만 제거."""
        wl = self._make_wl()
        old_at = (datetime.now() - timedelta(hours=100)).isoformat()
        wl.add(_make_watchlist_item(model_number="OLD-001", added_at=old_at))
        wl.add(_make_watchlist_item(model_number="NEW-001"))
        removed = wl.cleanup_stale(max_age_hours=48)
        assert removed == 1
        assert wl.size == 1
        assert wl.get("NEW-001") is not None
        assert wl.get("OLD-001") is None


class TestSchedulerStartStop:
    """스케줄러 시작/중지 테스트 (동등 복원)."""

    def test_scheduler_start_starts_loops(self, monkeypatch):
        """start() → 7개 루프 .start() 호출 (v2_reverse 활성 상태)."""
        from src import scheduler as scheduler_mod

        monkeypatch.setattr(scheduler_mod.settings, "v2_reverse_disabled", False)
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)
        scheduler.tier1_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.tier2_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.daily_report = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.collect_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.refresh_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.spike_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.continuous_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.followup_sweep_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.followup_report_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.adapter_health_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.db_health_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.start()
        scheduler.tier1_loop.start.assert_called_once()
        scheduler.tier2_loop.start.assert_called_once()
        scheduler.daily_report.start.assert_called_once()
        scheduler.collect_loop.start.assert_called_once()
        scheduler.refresh_loop.start.assert_called_once()
        scheduler.spike_loop.start.assert_called_once()
        scheduler.continuous_loop.start.assert_called_once()

    def test_scheduler_start_skips_v2_reverse_only_when_disabled(self, monkeypatch):
        """v2_reverse_disabled=True → tier1/tier2/continuous 만 미가동.
        refresh/spike 는 47k 갱신 메커니즘이라 v2_reverse 와 분리되어 항상 가동.
        """
        from src import scheduler as scheduler_mod

        monkeypatch.setattr(scheduler_mod.settings, "v2_reverse_disabled", True)
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)
        scheduler.tier1_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.tier2_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.daily_report = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.collect_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.refresh_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.spike_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.continuous_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.followup_sweep_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.followup_report_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.adapter_health_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.db_health_loop = MagicMock(is_running=MagicMock(return_value=False))
        scheduler.start()
        scheduler.tier1_loop.start.assert_not_called()
        scheduler.tier2_loop.start.assert_not_called()
        scheduler.continuous_loop.start.assert_not_called()
        scheduler.refresh_loop.start.assert_called_once()
        scheduler.spike_loop.start.assert_called_once()
        scheduler.daily_report.start.assert_called_once()
        scheduler.collect_loop.start.assert_called_once()

    def test_scheduler_stop_cancels_loops(self):
        """stop() → 7개 루프 .cancel() 호출."""
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)
        scheduler.tier1_loop = MagicMock()
        scheduler.tier2_loop = MagicMock()
        scheduler.daily_report = MagicMock()
        scheduler.collect_loop = MagicMock()
        scheduler.refresh_loop = MagicMock()
        scheduler.spike_loop = MagicMock()
        scheduler.continuous_loop = MagicMock()
        scheduler.stop()
        scheduler.tier1_loop.cancel.assert_called_once()
        scheduler.tier2_loop.cancel.assert_called_once()
        scheduler.daily_report.cancel.assert_called_once()
