"""자동 스캔 스케줄러.

discord.ext.tasks 기반 2티어 실시간 아키텍처:
- Tier 1: 워치리스트 빌더 (30분 주기)
- Tier 2: 실시간 폴링 모니터 (60초 주기)
- 일일 리포트 (자정)
- 실시간 DB: 신규 수집 (6시간), 시세 갱신 (10분), 급등 감지 (60분)
"""

import asyncio
from datetime import datetime, time

from discord.ext import tasks

from src.config import settings
from src.models.product import Signal
from src.utils.logging import setup_logger
from src.utils.resilience import error_aggregator

logger = setup_logger("scheduler")


class Scheduler:
    """2티어 스케줄러.

    bot.py의 KreamBot에서 초기화하고 start()로 시작한다.
    """

    def __init__(self, bot):
        self.bot = bot
        self.last_tier1_run: datetime | None = None
        self.last_tier2_run: datetime | None = None

    def start(self) -> None:
        """모든 스케줄 태스크 시작."""
        if not self.tier1_loop.is_running():
            self.tier1_loop.start()
        if not self.tier2_loop.is_running():
            self.tier2_loop.start()
        if not self.daily_report.is_running():
            self.daily_report.start()
        if not self.collect_loop.is_running():
            self.collect_loop.start()
        if not self.refresh_loop.is_running():
            self.refresh_loop.start()
        if not self.spike_loop.is_running():
            self.spike_loop.start()

        # 스캔 캐시 정리
        if hasattr(self.bot, 'scanner') and hasattr(self.bot.scanner, 'scan_cache'):
            self.bot.scanner.scan_cache.cleanup_expired()

        logger.info("스케줄러 시작 완료 (Tier1=%d분, Tier2=%d초)",
                     settings.tier1_interval_minutes, settings.tier2_interval_seconds)

    def stop(self) -> None:
        """모든 스케줄 태스크 중지."""
        self.tier1_loop.cancel()
        self.tier2_loop.cancel()
        self.daily_report.cancel()
        self.collect_loop.cancel()
        self.refresh_loop.cancel()
        self.spike_loop.cancel()
        logger.info("스케줄러 중지")

    def start_auto_scan(self) -> None:
        """자동스캔 시작 (Tier1 + Tier2)."""
        if not self.tier1_loop.is_running():
            self.tier1_loop.start()
        if not self.tier2_loop.is_running():
            self.tier2_loop.start()
        logger.info("자동스캔 시작 (Tier1 + Tier2)")

    def stop_auto_scan(self) -> None:
        """자동스캔 중지."""
        if self.tier1_loop.is_running():
            self.tier1_loop.cancel()
        if self.tier2_loop.is_running():
            self.tier2_loop.cancel()
        logger.info("자동스캔 중지")

    # ─── Tier 1: 워치리스트 빌더 (30분 주기) ─────────────

    @tasks.loop(minutes=settings.tier1_interval_minutes)
    async def tier1_loop(self) -> None:
        """카테고리스캔 → 크림 매칭 → 워치리스트 빌더."""
        logger.info("=== Tier1 워치리스트 빌더 시작 ===")

        try:
            # ── 콜백 정의 ──

            async def on_reverse_progress(message: str):
                """역방향 스캔 진행 상황 → Discord."""
                logger.info("Tier1: %s", message)
                await self.bot.log_to_channel(message)

            async def on_reverse_opportunity(opportunity):
                """수익 기회 발견 → Discord 즉시 알림."""
                if opportunity.signal not in (Signal.STRONG_BUY, Signal.BUY):
                    return
                # 상세 알림
                kp = opportunity.kream_product
                top_sp = opportunity.size_profits[0] if opportunity.size_profits else None
                if top_sp:
                    await self.bot.log_to_channel(
                        f"💰 수익 기회 발견!\n"
                        f"- 상품: {kp.name[:40]}\n"
                        f"- 모델번호: {kp.model_number}\n"
                        f"- 소싱처: {top_sp.source} {top_sp.musinsa_price:,}원\n"
                        f"- 크림 시세: {top_sp.kream_bid_price:,}원\n"
                        f"- 실수익: {top_sp.confirmed_profit:,}원"
                        if top_sp.kream_bid_price else
                        f"💰 수익 기회 발견!\n"
                        f"- 상품: {kp.name[:40]}\n"
                        f"- 모델번호: {kp.model_number}\n"
                        f"- 소싱처: {top_sp.source} {top_sp.musinsa_price:,}원\n"
                        f"- 실수익: {top_sp.confirmed_profit:,}원"
                    )
                await self.bot.send_auto_scan_alert(opportunity)

            async def on_error(message: str):
                """에러 → Discord."""
                await self.bot.log_to_channel(f"⚠️ 에러: {message}")

            async def on_cat_opportunity(opportunity):
                if opportunity.signal not in (Signal.STRONG_BUY, Signal.BUY):
                    return
                await self.bot.send_auto_scan_alert(opportunity)

            async def on_cat_progress(message):
                logger.info("Tier1: %s", message)
                if "필터 완료" in message:
                    await self.bot.log_to_channel(f"📋 {message}")

            # ── 카테고리 스캔 ──

            cat_result = await self.bot.scanner.run_category_scan(
                categories=["103"],
                max_pages=1,
                on_opportunity=on_cat_opportunity,
                on_progress=on_cat_progress,
                resume=True,
            )

            cat_elapsed = 0.0
            if cat_result.finished_at and cat_result.started_at:
                cat_elapsed = (cat_result.finished_at - cat_result.started_at).total_seconds()

            self.bot.daily_stats["scan_count"] += 1
            self.bot.daily_stats["product_count"] += cat_result.detail_fetched
            self.last_tier1_run = datetime.now()

            # ── Tier1 워치리스트 빌더 (역방향 + 카테고리 gap) ──

            if hasattr(self.bot, 'tier1_scanner') and self.bot.tier1_scanner:
                try:
                    # hot 상품 수 미리 조회하여 시작 알림
                    hot_count = await self.bot.db.get_hot_product_count()
                    await self.bot.log_to_channel(
                        f"🔍 Tier1 스캔 시작\n"
                        f"- 역방향: hot {hot_count}건 처리 예정\n"
                        f"- 카테고리: 60건 처리 예정"
                    )

                    t1_result = await self.bot.tier1_scanner.run(
                        on_progress=on_reverse_progress,
                        on_opportunity=on_reverse_opportunity,
                        on_error=on_error,
                    )
                    await self.bot.log_to_channel(
                        f"Tier1 완료 ({cat_elapsed:.0f}초) | "
                        f"역방향: hot {t1_result.reverse_hot}/소싱 {t1_result.reverse_sourced}"
                        f"/수익 {t1_result.reverse_profitable} | "
                        f"카테고리: 스캔 {t1_result.scanned}/매칭 {t1_result.matched}"
                        f"/추가 +{t1_result.added}"
                    )
                except Exception as e:
                    error_aggregator.add("tier1_watchlist", e)
                    logger.error("Tier1 워치리스트 빌더 실패: %s", e)
                    await self.bot.log_to_channel(f"⚠️ 에러: Tier1 워치리스트 빌더 실패 — {e}")
            else:
                await self.bot.log_to_channel(
                    f"카테고리스캔 완료 ({cat_elapsed:.0f}초) | "
                    f"리스팅 {cat_result.listing_fetched} → "
                    f"상세 {cat_result.detail_fetched} → "
                    f"수익기회 {len(cat_result.opportunities)}"
                )

        except Exception as e:
            error_aggregator.add("tier1_loop", e)
            logger.error("Tier1 실패: %s", e)
            await self.bot.log_to_channel(f"⚠️ 에러: Tier1 실패 — {e}")

    @tier1_loop.before_loop
    async def before_tier1(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

    # ─── Tier 2: 실시간 폴링 모니터 (60초 주기) ──────────

    @tasks.loop(seconds=settings.tier2_interval_seconds)
    async def tier2_loop(self) -> None:
        """워치리스트 대상 크림 실시간 폴링."""
        if not hasattr(self.bot, 'tier2_monitor') or not self.bot.tier2_monitor:
            return

        try:
            result = await self.bot.tier2_monitor.run()
            self.last_tier2_run = datetime.now()
            if result and result.alerts_sent > 0:
                logger.info("Tier2: %d건 알림 발송", result.alerts_sent)
        except Exception as e:
            error_aggregator.add("tier2_loop", e)
            logger.error("Tier2 실패: %s", e)

    @tier2_loop.before_loop
    async def before_tier2(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)

    # ─── 일일 리포트 (자정) ──────────────────────────────

    @tasks.loop(time=time(hour=0, minute=0))
    async def daily_report(self) -> None:
        """매일 자정 일일 리포트."""
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

            error_summary = error_aggregator.get_summary(minutes=1440)
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

            # 카테고리스캔 이력 초기화
            await self.bot.scanner.db.clear_category_scan_history()

            # 워치리스트 만료 항목 정리
            if hasattr(self.bot, 'watchlist') and self.bot.watchlist:
                self.bot.watchlist.cleanup_stale()

            logger.info("일일 리포트 전송 완료, 통계 리셋")

        except Exception as e:
            error_aggregator.add("daily_report", e)
            logger.error("일일 리포트 실패: %s", e)

    @daily_report.before_loop
    async def before_daily_report(self) -> None:
        await self.bot.wait_until_ready()

    # ─── 신규 상품 자동 수집 (6시간 주기) ─────────────

    @tasks.loop(hours=settings.realtime_collect_interval_hours)
    async def collect_loop(self) -> None:
        """크림 신규 상품 자동 수집."""
        if not hasattr(self.bot, '_kream_collector') or not self.bot._kream_collector:
            return

        try:
            result = await self.bot._kream_collector.run()
            if result["total_new"] > 0:
                await self.bot.log_to_channel(
                    f"📦 신규 상품 수집: +{result['total_new']}건 ({result['elapsed']:.0f}초)"
                )
            logger.info("신규 수집: %d건 (%.0f초)", result["total_new"], result["elapsed"])
        except Exception as e:
            error_aggregator.add("collect_loop", e)
            logger.error("신규 수집 실패: %s", e)

    @collect_loop.before_loop
    async def before_collect(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(300)

    # ─── 우선순위 시세 갱신 (10분 주기) ────────────────

    @tasks.loop(minutes=10)
    async def refresh_loop(self) -> None:
        """거래량 기반 우선순위 시세 갱신."""
        if not hasattr(self.bot, '_kream_refresher') or not self.bot._kream_refresher:
            return

        try:
            result = await self.bot._kream_refresher.run()
            if result["refreshed"] > 0:
                logger.info(
                    "시세 갱신: %d/%d (%.0f초)",
                    result["refreshed"], result["queue_size"], result["elapsed"],
                )
        except Exception as e:
            error_aggregator.add("refresh_loop", e)
            logger.error("시세 갱신 실패: %s", e)

    @refresh_loop.before_loop
    async def before_refresh(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(180)

    # ─── 거래량 급등 감지 (60분 주기) ──────────────────

    @tasks.loop(minutes=settings.realtime_volume_check_minutes)
    async def spike_loop(self) -> None:
        """거래량 급등 감지 + hot 승격."""
        if not hasattr(self.bot, '_kream_spike_detector') or not self.bot._kream_spike_detector:
            return

        try:
            result = await self.bot._kream_spike_detector.run()
            if result["spikes"] > 0:
                await self.bot.log_to_channel(
                    f"🔥 거래량 급등: {result['spikes']}건 감지 "
                    f"(승격: {', '.join(result['promoted'][:5])})"
                )
            logger.info(
                "급등 감지: %d건 체크, %d건 급등 (%.0f초)",
                result["checked"], result["spikes"], result["elapsed"],
            )
        except Exception as e:
            error_aggregator.add("spike_loop", e)
            logger.error("급등 감지 실패: %s", e)

    @spike_loop.before_loop
    async def before_spike(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(600)
