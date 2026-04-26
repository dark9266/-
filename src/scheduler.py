"""자동 스캔 스케줄러.

discord.ext.tasks 기반 3티어 실시간 아키텍처:
- Tier 0: v2 연속 배치 스캔 (5분 주기, 50건/배치)
- Tier 1: 워치리스트 빌더 (30분 주기)
- Tier 2: 실시간 폴링 모니터 (60초 주기)
- 일일 리포트 (자정)
- 실시간 DB: 신규 수집 (6시간), 시세 갱신 (10분), 급등 감지 (60분)
"""

import asyncio
import os
from datetime import datetime, time
from pathlib import Path

from discord.ext import tasks

from src.config import settings
from src.core.db import sync_connect
from src.models.product import Signal
from src.utils.logging import setup_logger
from src.utils.resilience import error_aggregator

logger = setup_logger("scheduler")

# Phase 4 헬스모니터 임계값 — 2026-04-18 incident 기준.
# 평상시 WAL 은 수 MB 이내이고 fd 는 적으면 3, 많아도 7~8 수준.
_HEALTH_WAL_WARN_MB: float = 100.0
_HEALTH_FD_WARN: int = 10
_HEALTH_DECISION_STALL_SEC: int = 3600  # 1h 무이벤트


class Scheduler:
    """2티어 스케줄러.

    bot.py의 KreamBot에서 초기화하고 start()로 시작한다.
    """

    def __init__(self, bot):
        self.bot = bot
        self.last_tier1_run: datetime | None = None
        self.last_tier2_run: datetime | None = None
        self.last_continuous_run: datetime | None = None

    def start(self) -> None:
        """모든 스케줄 태스크 시작."""
        if settings.v2_reverse_disabled:
            logger.warning(
                "[v2] reverse_scanner 경로 비활성 (V2_REVERSE_DISABLED=true) — "
                "Tier1/Tier2/continuous 루프 미가동"
            )
        else:
            if not self.tier1_loop.is_running():
                self.tier1_loop.start()
            if not self.tier2_loop.is_running():
                self.tier2_loop.start()
            if not self.continuous_loop.is_running():
                self.continuous_loop.start()
        if not self.daily_report.is_running():
            self.daily_report.start()
        if not self.collect_loop.is_running():
            self.collect_loop.start()
        if not self.followup_sweep_loop.is_running():
            self.followup_sweep_loop.start()
        if not self.followup_report_loop.is_running():
            self.followup_report_loop.start()
        if not self.adapter_health_loop.is_running():
            self.adapter_health_loop.start()
        if not self.db_health_loop.is_running():
            self.db_health_loop.start()
        # 갱신 루프는 V2_REVERSE_DISABLED 와 무관하게 항상 가동.
        # reverse/continuous_scanner 폐기 후 47k 갱신은 price_refresher + spike_detector 가 단독 담당.
        # v2 reverse 와 묶인 비활성 상태에서는 last_volume_check NULL 98%+ 누적으로 prefilter_low_volume 폭주.
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
        self.continuous_loop.cancel()
        self.followup_sweep_loop.cancel()
        self.followup_report_loop.cancel()
        self.adapter_health_loop.cancel()
        self.db_health_loop.cancel()
        logger.info("스케줄러 중지")

    def start_auto_scan(self) -> None:
        """자동스캔 시작 (Tier1 + Tier2)."""
        if settings.v2_reverse_disabled:
            logger.warning("[v2] reverse_scanner 비활성 상태 — 자동스캔 무시")
            return
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
                """역방향 스캔 진행 상황 → 진행 채널."""
                logger.info("Tier1: %s", message)
                await self.bot.progress_to_channel(message)

            async def on_reverse_opportunity(opportunity):
                """수익 기회 발견 → 매수알림 채널."""
                if opportunity.signal not in (Signal.STRONG_BUY, Signal.BUY):
                    return
                await self.bot.send_auto_scan_alert(opportunity)

            async def on_error(message: str):
                """에러 → 로그 채널."""
                await self.bot.log_to_channel(f"⚠️ 에러: {message}")

            async def on_cat_opportunity(opportunity):
                if opportunity.signal not in (Signal.STRONG_BUY, Signal.BUY):
                    return
                await self.bot.send_auto_scan_alert(opportunity)

            async def on_cat_progress(message):
                logger.info("Tier1: %s", message)
                if "필터 완료" in message:
                    await self.bot.progress_to_channel(f"📋 {message}")

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
                    await self.bot.progress_to_channel(
                        f"🔍 Tier1 스캔 시작\n"
                        f"- 역방향: hot {hot_count}건 처리 예정\n"
                        f"- 카테고리: 60건 처리 예정"
                    )

                    t1_result = await self.bot.tier1_scanner.run(
                        on_progress=on_reverse_progress,
                        on_opportunity=on_reverse_opportunity,
                        on_error=on_error,
                    )
                    await self.bot.progress_to_channel(
                        f"✅ Tier1 완료 ({cat_elapsed:.0f}초) | "
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
                await self.bot.progress_to_channel(
                    f"✅ 카테고리스캔 완료 ({cat_elapsed:.0f}초) | "
                    f"리스팅 {cat_result.listing_fetched} → "
                    f"브랜드필터 {cat_result.brand_filtered} → "
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
                await self.bot.progress_to_channel(
                    f"📦 신규 상품 수집: +{result['total_new']}건 ({result['elapsed']:.0f}초)"
                )
            logger.info("신규 수집: %d건 (%.0f초)", result["total_new"], result["elapsed"])
        except Exception as e:
            error_aggregator.add("collect_loop", e)
            logger.error("신규 수집 실패: %s", e)

        # 미등재 큐(kream_collect_queue) pending 항목 처리
        try:
            pending = await self.bot._kream_collector.collect_pending(batch_size=20)
            if pending["found"] > 0:
                await self.bot.progress_to_channel(
                    f"🔍 큐 처리: {pending['found']}건 발견 "
                    f"(잔여 {pending['remaining']}건)"
                )
            logger.info(
                "큐 처리: 체크 %d, 발견 %d, 미발견 %d, 잔여 %d",
                pending["checked"], pending["found"],
                pending["not_found"], pending["remaining"],
            )
        except Exception as e:
            error_aggregator.add("collect_pending", e)
            logger.error("큐 처리 실패: %s", e)

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
                await self.bot.progress_to_channel(
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

    # ─── v2 연속 배치 스캔 (5분 주기) ────────────────────

    @tasks.loop(minutes=settings.continuous_scan_interval_minutes)
    async def continuous_loop(self) -> None:
        """v2 연속 배치 스캔 — hot/warm/cold 우선순위 큐."""
        if not hasattr(self.bot, 'continuous_scanner') or not self.bot.continuous_scanner:
            return

        try:
            async def on_opportunity(opportunity):
                if opportunity.signal not in (Signal.STRONG_BUY, Signal.BUY):
                    return
                await self.bot.send_auto_scan_alert(opportunity)

            async def on_progress(message: str):
                logger.info("연속스캔: %s", message)
                await self.bot.progress_to_channel(message)

            result = await self.bot.continuous_scanner.run_batch(
                on_progress=on_progress,
                on_opportunity=on_opportunity,
            )

            self.last_continuous_run = datetime.now()
            self.bot.daily_stats["scan_count"] += 1
            self.bot.daily_stats["product_count"] += result.processed

        except Exception as e:
            error_aggregator.add("continuous_loop", e)
            logger.error("연속 스캔 실패: %s", e)

    @continuous_loop.before_loop
    async def before_continuous(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(180)

    # ─── Phase 4: 알림 정답률 sweep (6시간 주기) ────────────

    @tasks.loop(hours=6)
    async def followup_sweep_loop(self) -> None:
        """알림 발사 후 24h 이상 지난 followup 의 실제 체결 여부 검증."""
        try:
            from src.core.alert_outcome import sweep_pending
            from src.core.alert_outcome_sweeper import make_check_fn

            check_fn = make_check_fn()  # 기본: 글로벌 kream_crawler 싱글톤
            stats = await sweep_pending(
                self.bot.db.db_path,
                check_fn,
                older_than_sec=86400,  # 24h 후 체결 가능성 안정
                limit=200,
            )
            logger.info(
                "[followup_sweep] scanned=%d sold=%d not_sold=%d errors=%d",
                stats["scanned"], stats["sold"],
                stats["not_sold"], stats["errors"],
            )
        except Exception as e:
            error_aggregator.add("followup_sweep_loop", e)
            logger.error("followup sweep 실패: %s", e)

    @followup_sweep_loop.before_loop
    async def before_followup_sweep(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(3600)  # 기동 1h 후 첫 실행 (다른 루프와 겹침 방지)

    # ─── Phase 4: 알림 정답률 일일 리포트 (00:30) ──────────

    @tasks.loop(time=time(hour=0, minute=30))
    async def followup_report_loop(self) -> None:
        """7일 윈도우 적중률 + 24h decision 분포 → 로그 채널에 발송."""
        try:
            from src.core.alert_outcome import hit_rate_summary
            from src.core.decision_report import decision_summary
            from src.discord_bot.formatter import (
                format_decision_report,
                format_followup_report,
            )

            summary = await hit_rate_summary(self.bot.db.db_path, hours=168)
            embed = format_followup_report(summary)

            dec = await decision_summary(self.bot.db.db_path, hours=24.0)
            dec_embed = format_decision_report(dec)

            channel_id = settings.channel_log or settings.channel_daily_report
            if channel_id:
                channel = self.bot.get_channel(channel_id)
                if channel:
                    await channel.send(embed=embed)
                    await channel.send(embed=dec_embed)
            logger.info(
                "[followup_report] total=%d checked=%d sold=%d hit=%s decision_total=%d",
                summary["total"], summary["checked"],
                summary["sold"], summary["hit_rate_pct"],
                dec.total,
            )
        except Exception as e:
            error_aggregator.add("followup_report_loop", e)
            logger.error("followup 리포트 실패: %s", e)

    @followup_report_loop.before_loop
    async def before_followup_report(self) -> None:
        await self.bot.wait_until_ready()

    # ─── 어댑터 silent failure 감지 (주간, 월요일 00:45) ──────

    @tasks.loop(time=time(hour=0, minute=45))
    async def adapter_health_loop(self) -> None:
        """주 1회 어댑터별 candidate emit 감사 → silent 어댑터 경보."""
        # 월요일만 실행 (daily loop 기반 주간 트리거)
        if datetime.now().weekday() != 0:
            return
        try:
            from src.core.adapter_heartbeat import silent_adapters
            from src.core.runtime import active_adapter_sources
            from src.discord_bot.formatter import format_adapter_health

            expected = active_adapter_sources()
            silent = await silent_adapters(
                self.bot.db.db_path, expected, threshold_hours=168.0
            )
            embed = format_adapter_health(
                silent, expected_count=len(expected), threshold_hours=168.0
            )
            channel_id = settings.channel_log or settings.channel_daily_report
            if channel_id:
                channel = self.bot.get_channel(channel_id)
                if channel:
                    await channel.send(embed=embed)
            logger.info(
                "[adapter_health] expected=%d silent=%d",
                len(expected), len(silent),
            )
        except Exception as e:
            error_aggregator.add("adapter_health_loop", e)
            logger.error("adapter_health 리포트 실패: %s", e)

    @adapter_health_loop.before_loop
    async def before_adapter_health(self) -> None:
        await self.bot.wait_until_ready()

    # ─── DB 헬스모니터 (5분 주기) ─────────────────────────
    # 2026-04-18 WAL incident 이후 자가 관측 레이어.
    # session-only 크론/lsof 의존 제거 — 봇 안에서 WAL 부풀음·fd 누수·
    # orchestrator 정체를 감지해 조기 경보한다.

    @tasks.loop(minutes=5)
    async def db_health_loop(self) -> None:
        """WAL 크기 · DB fd 수 · decision_log 정체 3종 헬스핀 + 강제 checkpoint.

        2026-04-18 관측: wal_autocheckpoint=1000 만으로는 장시간 read 트랜잭션이
        섞인 워크로드에서 WAL 이 무한 성장 (35분 240MB → 2h 680MB 투영).
        매 5분 `PRAGMA wal_checkpoint(TRUNCATE)` 로 강제 회수.
        """
        try:
            db_path = settings.db_path

            # 1) WAL 파일 크기 (체크포인트 전)
            wal_path = Path(f"{db_path}-wal")
            try:
                wal_mb_before = wal_path.stat().st_size / 1024 / 1024
            except FileNotFoundError:
                wal_mb_before = 0.0

            # 2) 강제 체크포인트 — TRUNCATE 모드로 WAL 파일 0바이트까지 회수.
            #    별도 sync 연결로 실행해 bot.db/checkpoint_store 의 장시간
            #    트랜잭션과 독립적으로 동작 (busy_timeout 30s 내 자체 종결).
            ckpt_result = await asyncio.to_thread(_force_wal_checkpoint, db_path)

            # 3) 체크포인트 후 WAL 크기
            try:
                wal_mb = wal_path.stat().st_size / 1024 / 1024
            except FileNotFoundError:
                wal_mb = 0.0
            if wal_mb > _HEALTH_WAL_WARN_MB:
                logger.warning("[health] WAL 비대화: %.1fMB", wal_mb)

            # 4) DB fd 수 (Linux /proc 기반 — WSL/리눅스만)
            fd_count = _count_db_fds(db_path)
            if fd_count is not None and fd_count > _HEALTH_FD_WARN:
                logger.warning("[health] DB fd 누수 의심: %d", fd_count)

            # 5) decision_log 1h 무이벤트 — orchestrator 정체 의심
            stall = _decision_log_stall(db_path, _HEALTH_DECISION_STALL_SEC)
            if stall:
                logger.warning(
                    "[health] decision_log 1h 무이벤트 — orchestrator 정체 의심"
                )

            logger.info(
                "[health] WAL=%.1f→%.1fMB ckpt=%s fd=%s decision_stall=%s",
                wal_mb_before, wal_mb, ckpt_result,
                fd_count if fd_count is not None else "n/a",
                stall,
            )
        except Exception as e:
            error_aggregator.add("db_health_loop", e)
            logger.exception("db_health_loop 실패: %s", e)

    @db_health_loop.before_loop
    async def before_db_health(self) -> None:
        await self.bot.wait_until_ready()


def _count_db_fds(db_path: str) -> int | None:
    """현재 프로세스가 열고 있는 DB 관련 fd 수. Linux only.

    /proc 접근 불가 환경에서는 None 반환 (사일런스).
    """
    proc_fd = Path("/proc/self/fd")
    if not proc_fd.exists():
        return None
    db_name = Path(db_path).name
    count = 0
    try:
        for fd in proc_fd.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if db_name in target:
                count += 1
    except OSError:
        return None
    return count


def _force_wal_checkpoint(db_path: str) -> str:
    """별도 연결로 `PRAGMA wal_checkpoint(TRUNCATE)` 실행.

    장시간 read 트랜잭션이 있으면 busy 로 돌아오지만, TRUNCATE 자체는
    best-effort 이라 실패해도 다음 5분 주기에 재시도. 반환값은 진단용 문자열.
    """
    try:
        with sync_connect(db_path, timeout=30.0) as conn:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # row = (busy, log_pages, checkpointed_pages)
        if row is None:
            return "no_result"
        return f"busy={row[0]} log={row[1]} done={row[2]}"
    except Exception as e:
        return f"err:{type(e).__name__}"


def _decision_log_stall(db_path: str, seconds: int) -> bool:
    """최근 `seconds` 초 동안 decision_log 쓰기가 0건인가."""
    cutoff = datetime.now().timestamp() - seconds
    try:
        with sync_connect(db_path, read_only=True, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM decision_log WHERE ts >= ?",
                (cutoff,),
            ).fetchone()
    except Exception:
        return False  # 테이블 미생성 등 — 경보 억제
    return int(row[0] or 0) == 0
