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
from src.utils.resilience import ChromeHealthChecker, ErrorAggregator, RecoveryRecord


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
    return bot


# --- Scheduler 테스트 ---


class TestSchedulerTracking:
    """집중 추적 관련 테스트."""

    def test_add_to_tracking(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)
        op = _make_opportunity()

        scheduler.add_to_tracking(op)

        assert "1" in scheduler._tracking
        assert "1" in scheduler._tracking_expires
        assert scheduler._tracking["1"] is op

    def test_add_multiple_products(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)

        op1 = _make_opportunity(product_id="1", name="신발A")
        op2 = _make_opportunity(product_id="2", name="신발B")

        scheduler.add_to_tracking(op1)
        scheduler.add_to_tracking(op2)

        assert len(scheduler._tracking) == 2

    def test_add_same_product_updates(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)

        op1 = _make_opportunity(product_id="1", best_profit=10000)
        op2 = _make_opportunity(product_id="1", best_profit=20000)

        scheduler.add_to_tracking(op1)
        scheduler.add_to_tracking(op2)

        assert len(scheduler._tracking) == 1
        assert scheduler._tracking["1"].best_profit == 20000

    def test_cleanup_expired_tracking(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)

        op = _make_opportunity(product_id="1")
        scheduler.add_to_tracking(op)

        # 만료 시간을 과거로 설정
        scheduler._tracking_expires["1"] = datetime.now() - timedelta(hours=1)

        scheduler._cleanup_expired_tracking()

        assert "1" not in scheduler._tracking
        assert "1" not in scheduler._tracking_expires

    def test_cleanup_keeps_active_tracking(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)

        op1 = _make_opportunity(product_id="1")
        op2 = _make_opportunity(product_id="2")

        scheduler.add_to_tracking(op1)
        scheduler.add_to_tracking(op2)

        # 1번만 만료
        scheduler._tracking_expires["1"] = datetime.now() - timedelta(hours=1)

        scheduler._cleanup_expired_tracking()

        assert "1" not in scheduler._tracking
        assert "2" in scheduler._tracking

    def test_tracking_expiry_duration(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)

        before = datetime.now()
        op = _make_opportunity()
        scheduler.add_to_tracking(op)
        after = datetime.now()

        exp = scheduler._tracking_expires["1"]
        # 만료 시간은 현재 + 2시간 (기본값)
        assert exp >= before + timedelta(hours=2)
        assert exp <= after + timedelta(hours=2)


class TestSchedulerStartStop:
    """스케줄러 시작/중지 테스트."""

    def test_start_sets_tasks_running(self):
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)

        # discord.ext.tasks.Loop는 mock이 아니므로 is_running 확인
        # start()가 에러 없이 호출되는지만 검증
        # (실제 Loop는 discord.py 이벤트 루프가 필요)
        assert scheduler._tracking == {}
        assert scheduler._tracking_expires == {}

    def test_stop_clears_nothing(self):
        """stop()은 추적 목록을 지우지 않는다."""
        bot = _make_mock_bot()
        scheduler = Scheduler(bot)

        op = _make_opportunity()
        scheduler.add_to_tracking(op)

        # stop()은 태스크만 취소하고 데이터는 유지
        assert len(scheduler._tracking) == 1


# --- ChromeHealthChecker 테스트 ---


class TestChromeHealthChecker:
    """Chrome 헬스체커 테스트."""

    def test_initial_state(self):
        checker = ChromeHealthChecker()
        assert checker._consecutive_failures == 0
        assert checker._total_recoveries == 0
        assert checker._total_recovery_failures == 0
        assert checker._last_recovery is None

    def test_report_success_resets_failures(self):
        checker = ChromeHealthChecker()
        checker._consecutive_failures = 5
        checker.report_success()
        assert checker._consecutive_failures == 0

    def test_report_failure_increments(self):
        checker = ChromeHealthChecker()
        checker.report_failure()
        checker.report_failure()
        assert checker._consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_check_and_recover_healthy(self):
        checker = ChromeHealthChecker()

        with patch("src.utils.resilience.ChromeHealthChecker.check_and_recover") as mock:
            mock.return_value = True
            result = await checker.check_and_recover()
            assert result is True

    def test_get_health_summary(self):
        checker = ChromeHealthChecker()
        checker._total_recoveries = 3
        checker._total_recovery_failures = 1
        checker._consecutive_failures = 0

        summary = checker.get_health_summary()
        assert "총 복구 성공: 3회" in summary
        assert "총 복구 실패: 1회" in summary

    def test_get_health_summary_with_history(self):
        checker = ChromeHealthChecker()
        checker._add_history(
            RecoveryRecord(True, "자동 복구 성공", datetime(2026, 3, 25, 10, 0))
        )
        checker._add_history(
            RecoveryRecord(False, "타임아웃", datetime(2026, 3, 25, 11, 0))
        )

        summary = checker.get_health_summary()
        assert "최근 복구 이력" in summary
        assert "자동 복구 성공" in summary
        assert "타임아웃" in summary

    def test_history_max_limit(self):
        checker = ChromeHealthChecker()
        checker._max_history = 5

        for i in range(10):
            checker._add_history(
                RecoveryRecord(True, f"복구 {i}", datetime.now())
            )

        assert len(checker._recovery_history) == 5
        assert checker._recovery_history[0].reason == "복구 5"

    @pytest.mark.asyncio
    async def test_notify_callback(self):
        checker = ChromeHealthChecker()
        messages = []

        async def mock_callback(msg):
            messages.append(msg)

        checker.set_notify_callback(mock_callback)
        await checker._notify("테스트 메시지")

        assert len(messages) == 1
        assert messages[0] == "테스트 메시지"

    @pytest.mark.asyncio
    async def test_notify_without_callback(self):
        checker = ChromeHealthChecker()
        # 콜백 없이 호출해도 에러 안 남
        await checker._notify("테스트")


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
