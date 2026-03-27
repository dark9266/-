"""자동 스캔 스케줄러.

discord.ext.tasks 기반 주기적 자동 스캔, 수익 상품 집중 추적, 일일 리포트.
"""

import asyncio
from datetime import datetime, time, timedelta

from discord.ext import tasks

from src.config import settings
from src.models.product import AutoScanOpportunity, ProfitOpportunity, Signal
from src.utils.logging import setup_logger
from src.utils.resilience import chrome_health, error_aggregator

logger = setup_logger("scheduler")


class Scheduler:
    """자동 스캔 스케줄러.

    bot.py의 KreamBot에서 초기화하고 start()로 시작한다.
    """

    def __init__(self, bot):
        """
        Args:
            bot: KreamBot 인스턴스 (scanner, db, send_profit_alert 등 접근용)
        """
        self.bot = bot
        # 집중 추적 대상 (product_id -> ProfitOpportunity)
        self._tracking: dict[str, ProfitOpportunity] = {}
        self._tracking_expires: dict[str, datetime] = {}
        self._tracking_duration = timedelta(hours=2)  # 집중 추적 지속 시간

    def start(self) -> None:
        """모든 스케줄 태스크 시작."""
        if not self.periodic_scan.is_running():
            self.periodic_scan.start()
        if not self.fast_track_scan.is_running():
            self.fast_track_scan.start()
        if not self.daily_report.is_running():
            self.daily_report.start()
        if not self.health_check.is_running():
            self.health_check.start()
        if not self.tab_cleanup.is_running():
            self.tab_cleanup.start()
        logger.info("스케줄러 시작 완료")

    def stop(self) -> None:
        """모든 스케줄 태스크 중지."""
        self.periodic_scan.cancel()
        self.fast_track_scan.cancel()
        self.daily_report.cancel()
        self.health_check.cancel()
        self.tab_cleanup.cancel()
        self.stop_auto_scan()
        logger.info("스케줄러 중지")

    def start_auto_scan(self) -> None:
        """자동스캔 루프 시작."""
        if not self.auto_scan_loop.is_running():
            self.auto_scan_loop.start()
            logger.info("자동스캔 루프 시작 (%d분 간격)", settings.auto_scan_interval_minutes)

    def stop_auto_scan(self) -> None:
        """자동스캔 루프 중지."""
        if self.auto_scan_loop.is_running():
            self.auto_scan_loop.cancel()
            logger.info("자동스캔 루프 중지")

    def add_to_tracking(self, opportunity: ProfitOpportunity) -> None:
        """수익 상품을 집중 추적 목록에 추가."""
        pid = opportunity.kream_product.product_id
        self._tracking[pid] = opportunity
        self._tracking_expires[pid] = datetime.now() + self._tracking_duration
        logger.info(
            "집중 추적 추가: %s (만료: %s)",
            opportunity.kream_product.name,
            self._tracking_expires[pid].strftime("%H:%M"),
        )

    def _cleanup_expired_tracking(self) -> None:
        """만료된 집중 추적 항목 제거."""
        now = datetime.now()
        expired = [pid for pid, exp in self._tracking_expires.items() if now > exp]
        for pid in expired:
            name = self._tracking.get(pid, None)
            del self._tracking[pid]
            del self._tracking_expires[pid]
            logger.info("집중 추적 만료: %s", pid)

    @tasks.loop(minutes=settings.scan_interval_minutes)
    async def periodic_scan(self) -> None:
        """주기적 자동 스캔 (기본 30분)."""
        logger.info("=== 자동 스캔 시작 ===")

        try:
            # Chrome 상태 확인
            if not await chrome_health.check_and_recover():
                logger.error("Chrome 상태 불량, 스캔 건너뜀")
                await self.bot.log_to_channel("⚠️ Chrome 연결 불량으로 자동 스캔 건너뜀")
                return

            result = await self.bot.scanner.scan_all_keywords()

            # 통계 업데이트
            self.bot.daily_stats["scan_count"] += 1
            self.bot.daily_stats["product_count"] += result.scanned_products
            self.bot.daily_stats["opportunity_count"] += len(result.opportunities)
            self.bot.daily_stats["opportunities"].extend(result.opportunities)

            # 수익 알림 전송
            for op in result.opportunities:
                await self.bot.send_profit_alert(op)
                # 매수/강력매수 시그널이면 집중 추적에 추가
                if op.signal in (Signal.STRONG_BUY, Signal.BUY):
                    self.add_to_tracking(op)

            # 가격 변동 알림
            if result.price_changes:
                await self.bot.send_price_change_alert(result.price_changes)

            chrome_health.report_success()

            logger.info(
                "=== 자동 스캔 완료: 수익기회 %d건, 가격변동 %d건 ===",
                len(result.opportunities), len(result.price_changes),
            )

        except Exception as e:
            chrome_health.report_failure()
            error_aggregator.add("periodic_scan", e)
            logger.error("자동 스캔 실패: %s", e)
            await self.bot.log_to_channel(f"⚠️ 자동 스캔 실패: {e}")

    @periodic_scan.before_loop
    async def before_periodic_scan(self) -> None:
        """봇이 ready될 때까지 대기."""
        await self.bot.wait_until_ready()
        # 시작 후 첫 스캔은 1분 뒤에 (봇 초기화 시간 확보)
        await asyncio.sleep(60)

    @tasks.loop(minutes=settings.fast_scan_interval_minutes)
    async def fast_track_scan(self) -> None:
        """수익 상품 집중 추적 (기본 10분)."""
        self._cleanup_expired_tracking()

        if not self._tracking:
            return

        logger.info("집중 추적 스캔: %d개 상품", len(self._tracking))

        for pid, opportunity in list(self._tracking.items()):
            try:
                # 크림 가격만 재수집
                updated = await self.bot.scanner.scan_single_product(
                    pid, opportunity.retail_products
                )
                if updated:
                    # 가격 변동 확인
                    retail_price_map = {}
                    for rp in opportunity.retail_products:
                        for s in rp.sizes:
                            if s.in_stock:
                                retail_price_map[s.size] = s.price

                    changes = await self.bot.scanner.price_tracker.check_kream_price_changes(
                        updated.kream_product, retail_price_map
                    )
                    significant = self.bot.scanner.price_tracker.filter_significant_changes(changes)
                    if significant:
                        await self.bot.send_price_change_alert(significant)

                    # 시그널이 비추천으로 떨어지면 추적 해제
                    if updated.signal == Signal.NOT_RECOMMENDED:
                        del self._tracking[pid]
                        del self._tracking_expires[pid]
                        logger.info("집중 추적 해제 (시그널 하락): %s", pid)

            except Exception as e:
                error_aggregator.add("fast_track", e)
                logger.error("집중 추적 실패 (%s): %s", pid, e)

    @fast_track_scan.before_loop
    async def before_fast_track(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)

    @tasks.loop(time=time(hour=0, minute=0))  # 매일 자정
    async def daily_report(self) -> None:
        """매일 자정 일일 리포트 자동 생성."""
        logger.info("일일 리포트 생성")

        try:
            from src.discord_bot.formatter import format_daily_report

            stats = self.bot.daily_stats
            top = sorted(stats["opportunities"], key=lambda o: -o.best_profit)[:5]

            embed = format_daily_report(
                scan_count=stats["scan_count"],
                product_count=stats["product_count"],
                opportunity_count=stats["opportunity_count"],
                top_products=top,
            )

            if settings.channel_daily_report:
                channel = self.bot.get_channel(settings.channel_daily_report)
                if channel:
                    await channel.send(embed=embed)

            # 에러 요약도 로그 채널에 전송
            error_summary = error_aggregator.get_summary(minutes=1440)  # 24시간
            if "에러 없음" not in error_summary:
                await self.bot.log_to_channel(f"📊 일일 에러 요약\n```\n{error_summary}\n```")

            # 일일 통계 리셋
            self.bot.daily_stats = {
                "scan_count": 0,
                "product_count": 0,
                "opportunity_count": 0,
                "opportunities": [],
            }
            error_aggregator.clear()

            logger.info("일일 리포트 전송 완료, 통계 리셋")

        except Exception as e:
            error_aggregator.add("daily_report", e)
            logger.error("일일 리포트 실패: %s", e)

    @daily_report.before_loop
    async def before_daily_report(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def health_check(self) -> None:
        """5분마다 Chrome 상태 확인 및 자동 복구."""
        try:
            await chrome_health.check_and_recover()
            # check_and_recover 내부에서 직접 알림을 전송하므로 여기선 추가 알림 불필요
        except Exception as e:
            error_aggregator.add("health_check", e)
            await self.bot.log_to_channel(f"❌ 헬스체크 자체 오류: {e}")

    @health_check.before_loop
    async def before_health_check(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(180)

    @tasks.loop(minutes=settings.auto_scan_interval_minutes)
    async def auto_scan_loop(self) -> None:
        """크림 인기상품 기준 자동스캔 (기본 30분)."""
        logger.info("=== 자동스캔 루프 실행 ===")

        try:
            # Chrome 상태 확인
            if not await chrome_health.check_and_recover():
                logger.error("Chrome 상태 불량, 자동스캔 건너뜀")
                await self.bot.log_to_channel("⚠️ Chrome 연결 불량으로 자동스캔 건너뜀")
                return

            async def on_opportunity(opportunity: AutoScanOpportunity):
                """수익 기회 발견 즉시 디스코드 알림."""
                await self.bot.send_auto_scan_alert(opportunity)
                # 확정 수익이면 집중 추적 추가
                if opportunity.best_confirmed_roi >= settings.auto_scan_confirmed_roi:
                    # AutoScanOpportunity를 ProfitOpportunity로 래핑하여 추적
                    from src.models.product import ProfitOpportunity as PO
                    tracking_op = PO(
                        kream_product=opportunity.kream_product,
                        retail_products=[],
                        size_profits=[],
                        best_profit=opportunity.best_confirmed_profit,
                        best_roi=opportunity.best_confirmed_roi,
                        signal=Signal.STRONG_BUY if opportunity.best_confirmed_roi >= 10 else Signal.BUY,
                    )
                    self.add_to_tracking(tracking_op)

            async def on_progress(message: str):
                """진행 상황 로그."""
                logger.info("자동스캔 진행: %s", message)

            result = await self.bot.scanner.auto_scan(
                on_opportunity=on_opportunity,
                on_progress=on_progress,
            )

            # 통계 업데이트
            self.bot.daily_stats["scan_count"] += 1
            self.bot.daily_stats["product_count"] += result.kream_scanned

            chrome_health.report_success()

            # 완료 요약 로그 채널에 전송
            elapsed = 0.0
            if result.finished_at and result.started_at:
                elapsed = (result.finished_at - result.started_at).total_seconds()

            await self.bot.log_to_channel(
                f"자동스캔 완료 ({elapsed:.0f}초) | "
                f"크림 {result.kream_scanned} → 매칭 {result.matched} → "
                f"수익기회 {len(result.opportunities)} "
                f"(확정 {result.confirmed_count} / 예상 {result.estimated_count}) | "
                f"에러 {len(result.errors)}"
            )

            logger.info(
                "=== 자동스캔 루프 완료: 수익기회 %d건 ===",
                len(result.opportunities),
            )

        except Exception as e:
            chrome_health.report_failure()
            error_aggregator.add("auto_scan_loop", e)
            logger.error("자동스캔 루프 실패: %s", e)
            await self.bot.log_to_channel(f"⚠️ 자동스캔 실패: {e}")

    @auto_scan_loop.before_loop
    async def before_auto_scan_loop(self) -> None:
        """봇이 ready될 때까지 대기."""
        await self.bot.wait_until_ready()
        # 시작 후 첫 스캔은 2분 뒤에
        await asyncio.sleep(120)

    @tasks.loop(minutes=30)
    async def tab_cleanup(self) -> None:
        """30분마다 불필요한 탭 정리."""
        try:
            from src.crawlers.chrome_cdp import cdp_manager
            await cdp_manager.close_extra_tabs(keep=2)
        except Exception:
            pass

    @tab_cleanup.before_loop
    async def before_tab_cleanup(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(300)
