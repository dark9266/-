"""디스코드 봇 코어 및 명령어 핸들러."""

from datetime import datetime, timedelta

import discord
from discord.ext import commands

from src.config import settings
from src.crawlers.chrome_cdp import cdp_manager
from src.crawlers.kream import kream_crawler
from src.crawlers.musinsa import musinsa_crawler
from src.discord_bot.formatter import (
    format_auto_scan_alert,
    format_auto_scan_summary,
    format_daily_report,
    format_help,
    format_price_change_alert,
    format_product_detail,
    format_profit_alert,
    format_status,
)
from src.models.database import Database
from src.profit_calculator import analyze_opportunity
from src.scanner import Scanner
from src.scheduler import Scheduler
from src.utils.logging import setup_logger
from src.utils.resilience import error_aggregator

logger = setup_logger("discord_bot")


class KreamBot(commands.Bot):
    """크림 리셀 수익 모니터링 봇."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.db = Database()
        self.scanner: Scanner | None = None
        self.scheduler: Scheduler | None = None
        self.start_time = datetime.now()
        # 스캔 통계 (일일 리포트용)
        self.daily_stats = {
            "scan_count": 0,
            "product_count": 0,
            "opportunity_count": 0,
            "opportunities": [],  # 오늘 발견된 수익 기회
        }

    async def setup_hook(self) -> None:
        """봇 시작 시 초기화."""
        await self.db.connect()
        self.scanner = Scanner(self.db)
        self.scheduler = Scheduler(self)
        logger.info("봇 초기화 완료")

    async def on_ready(self) -> None:
        logger.info("봇 로그인: %s (ID: %s)", self.user.name, self.user.id)
        # Chrome 헬스체커에 알림 콜백 연결
        from src.utils.resilience import chrome_health
        chrome_health.set_notify_callback(self.log_to_channel)
        # 스케줄러 시작
        if self.scheduler:
            self.scheduler.start()
        await self.log_to_channel("봇이 시작되었습니다. 자동 스캔이 활성화됩니다.")

    async def log_to_channel(self, message: str) -> None:
        """로그 채널에 메시지 전송."""
        if settings.channel_log:
            channel = self.get_channel(settings.channel_log)
            if channel:
                await channel.send(f"📝 {message}")

    async def send_profit_alert(self, opportunity) -> None:
        """수익 알림 채널에 알림 전송 (중복 방지 강화)."""
        if not settings.channel_profit_alert:
            return

        # 강화된 중복 알림 체크
        should_send = await self.db.should_send_alert(
            kream_product_id=opportunity.kream_product.product_id,
            alert_type="profit",
            signal=opportunity.signal.value,
            best_profit=opportunity.best_profit,
            cooldown_hours=1,
        )
        if not should_send:
            return

        channel = self.get_channel(settings.channel_profit_alert)
        if not channel:
            return

        embed = format_profit_alert(opportunity)
        msg = await channel.send(embed=embed)

        await self.db.save_alert(
            kream_product_id=opportunity.kream_product.product_id,
            alert_type="profit",
            best_profit=opportunity.best_profit,
            signal=opportunity.signal.value,
            message_id=str(msg.id),
        )

    async def send_price_change_alert(self, changes) -> None:
        """가격 변동 알림 전송 (중복 방지 적용)."""
        if not settings.channel_price_change or not changes:
            return

        # 상품별로 그룹핑하여 중복 체크
        by_product: dict[str, list] = {}
        for c in changes:
            pid = c.kream_product_id or c.model_number
            if pid not in by_product:
                by_product[pid] = []
            by_product[pid].append(c)

        filtered_changes = []
        for pid, product_changes in by_product.items():
            # 최대 수익 변동폭으로 중복 체크
            max_profit_diff = max(abs(c.profit_diff) for c in product_changes)
            should_send = await self.db.should_send_alert(
                kream_product_id=pid,
                alert_type="price_change",
                signal=product_changes[0].new_signal.value if hasattr(product_changes[0].new_signal, 'value') else str(product_changes[0].new_signal),
                best_profit=max_profit_diff,
                cooldown_hours=1,
            )
            if should_send:
                filtered_changes.extend(product_changes)

        if not filtered_changes:
            return

        channel = self.get_channel(settings.channel_price_change)
        if not channel:
            return

        embed = format_price_change_alert(filtered_changes)
        msg = await channel.send(embed=embed)

        # 알림 기록 저장
        for pid in by_product:
            product_changes = by_product[pid]
            if any(c in filtered_changes for c in product_changes):
                max_diff = max(abs(c.profit_diff) for c in product_changes)
                await self.db.save_alert(
                    kream_product_id=pid,
                    alert_type="price_change",
                    best_profit=max_diff,
                    signal=product_changes[0].new_signal.value if hasattr(product_changes[0].new_signal, 'value') else str(product_changes[0].new_signal),
                    message_id=str(msg.id),
                )


    async def send_auto_scan_alert(self, opportunity) -> None:
        """자동스캔 수익 알림 전송 (중복 방지 적용)."""
        if not settings.channel_profit_alert:
            return

        kream_product = opportunity.kream_product

        # 중복 알림 체크
        best_profit = max(
            opportunity.best_confirmed_profit,
            opportunity.best_estimated_profit,
        )
        should_send = await self.db.should_send_alert(
            kream_product_id=kream_product.product_id,
            alert_type="auto_scan",
            signal="확정" if opportunity.best_confirmed_roi >= 5 else "예상",
            best_profit=best_profit,
            cooldown_hours=1,
        )
        if not should_send:
            return

        channel = self.get_channel(settings.channel_profit_alert)
        if not channel:
            return

        embed = format_auto_scan_alert(opportunity)
        msg = await channel.send(embed=embed)

        await self.db.save_alert(
            kream_product_id=kream_product.product_id,
            alert_type="auto_scan",
            best_profit=best_profit,
            signal="확정" if opportunity.best_confirmed_roi >= 5 else "예상",
            message_id=str(msg.id),
        )


# --- 봇 인스턴스 및 명령어 등록 ---

bot = KreamBot()


@bot.command(name="스캔")
async def cmd_scan(ctx: commands.Context):
    """모니터링 키워드 기반 즉시 스캔."""
    await ctx.send("🔍 스캔을 시작합니다...")

    try:
        result = await bot.scanner.scan_all_keywords()

        bot.daily_stats["scan_count"] += 1
        bot.daily_stats["product_count"] += result.scanned_products
        bot.daily_stats["opportunity_count"] += len(result.opportunities)
        bot.daily_stats["opportunities"].extend(result.opportunities)

        # 결과 요약
        summary = (
            f"스캔 완료!\n"
            f"검색: {result.scanned_products}개 → 매칭: {result.matched_products}개 → "
            f"수익기회: {len(result.opportunities)}건"
        )
        if result.errors:
            summary += f"\n⚠️ 오류: {len(result.errors)}건"

        await ctx.send(summary)

        # 수익 알림 전송
        for op in result.opportunities:
            await bot.send_profit_alert(op)

        # 가격 변동 알림
        if result.price_changes:
            await bot.send_price_change_alert(result.price_changes)

    except Exception as e:
        logger.error("스캔 실패: %s", e)
        await ctx.send(f"❌ 스캔 중 오류 발생: {e}")


@bot.command(name="자동스캔")
async def cmd_auto_scan(ctx: commands.Context):
    """크림 인기상품 기준 1회 전체 자동 스캔."""
    progress_msg = await ctx.send("🔄 **자동스캔 시작** — 크림 인기상품 수집 중...")

    async def on_opportunity(opportunity):
        """수익 기회 발견 즉시 알림."""
        await bot.send_auto_scan_alert(opportunity)

    async def on_progress(message):
        """진행 상황 업데이트."""
        try:
            await progress_msg.edit(content=f"🔄 {message}")
        except Exception:
            pass

    try:
        result = await bot.scanner.auto_scan(
            on_opportunity=on_opportunity,
            on_progress=on_progress,
        )

        # 통계 업데이트
        bot.daily_stats["scan_count"] += 1
        bot.daily_stats["product_count"] += result.kream_scanned

        # 완료 요약 전송
        elapsed = 0.0
        if result.finished_at and result.started_at:
            elapsed = (result.finished_at - result.started_at).total_seconds()

        summary_embed = format_auto_scan_summary(
            kream_scanned=result.kream_scanned,
            musinsa_searched=result.musinsa_searched,
            matched=result.matched,
            confirmed_count=result.confirmed_count,
            estimated_count=result.estimated_count,
            total_opportunities=len(result.opportunities),
            elapsed_seconds=elapsed,
            errors=len(result.errors),
        )
        await ctx.send(embed=summary_embed)

        # 진행 메시지 업데이트
        await progress_msg.edit(
            content=(
                f"✅ **자동스캔 완료** — "
                f"크림 {result.kream_scanned}개 → 매칭 {result.matched}개 → "
                f"수익기회 {len(result.opportunities)}건 "
                f"(확정 {result.confirmed_count} / 예상 {result.estimated_count})"
            )
        )

    except Exception as e:
        logger.error("자동스캔 실패: %s", e)
        await ctx.send(f"❌ 자동스캔 중 오류 발생: {e}")


@bot.command(name="자동스캔시작")
async def cmd_auto_scan_start(ctx: commands.Context):
    """30분 간격 자동스캔 반복 시작."""
    if not bot.scheduler:
        await ctx.send("❌ 스케줄러가 초기화되지 않았습니다.")
        return

    if bot.scheduler.auto_scan_loop.is_running():
        await ctx.send("⚠️ 자동스캔이 이미 실행 중입니다.")
        return

    bot.scheduler.start_auto_scan()
    interval = settings.auto_scan_interval_minutes
    await ctx.send(
        f"✅ **자동스캔 시작**\n"
        f"• 스캔 주기: {interval}분\n"
        f"• 크림 인기상품 → 무신사 매입가 비교\n"
        f"• 확정 수익 ROI ≥ {settings.auto_scan_confirmed_roi}% → 긴급 알림\n"
        f"• 예상 수익 ROI ≥ {settings.auto_scan_estimated_roi}% → 참고 알림"
    )


@bot.command(name="자동스캔중지")
async def cmd_auto_scan_stop(ctx: commands.Context):
    """자동스캔 반복 중지."""
    if not bot.scheduler:
        await ctx.send("❌ 스케줄러가 초기화되지 않았습니다.")
        return

    if not bot.scheduler.auto_scan_loop.is_running():
        await ctx.send("⚠️ 자동스캔이 이미 중지 상태입니다.")
        return

    bot.scheduler.stop_auto_scan()
    await ctx.send("⏹️ **자동스캔 중지** 완료.")


@bot.command(name="크림")
async def cmd_kream_detail(ctx: commands.Context, product_id: str = ""):
    """크림 상품 상세 조회."""
    if not product_id:
        await ctx.send("사용법: `!크림 <상품ID>`")
        return

    await ctx.send(f"🔍 크림 상품 조회 중... (ID: {product_id})")

    try:
        kream_product = await kream_crawler.get_full_product_info(product_id)
        if not kream_product:
            await ctx.send("❌ 상품을 찾을 수 없습니다.")
            return

        # DB에 저장
        await bot.db.upsert_kream_product(
            product_id=kream_product.product_id,
            name=kream_product.name,
            model_number=kream_product.model_number,
            brand=kream_product.brand,
            image_url=kream_product.image_url,
            url=kream_product.url,
        )

        from src.models.product import ProfitOpportunity, Signal

        opportunity = ProfitOpportunity(
            kream_product=kream_product,
            retail_products=[],
            size_profits=[],
            signal=Signal.NOT_RECOMMENDED,
        )
        embed = format_product_detail(opportunity)
        await ctx.send(embed=embed)

    except Exception as e:
        logger.error("크림 조회 실패: %s", e)
        await ctx.send(f"❌ 조회 실패: {e}")


@bot.command(name="비교")
async def cmd_compare(ctx: commands.Context, *, model_number: str = ""):
    """모델번호로 전 사이트 가격 비교."""
    if not model_number:
        await ctx.send("사용법: `!비교 <모델번호>` (예: `!비교 DQ8423-100`)")
        return

    await ctx.send(f"🔍 모델번호 비교 중... (`{model_number}`)")

    try:
        opportunity = await bot.scanner.compare_by_model(model_number)
        if not opportunity:
            await ctx.send("❌ 매칭 상품을 찾을 수 없습니다.")
            return

        if opportunity.size_profits:
            embed = format_profit_alert(opportunity)
        else:
            embed = format_product_detail(opportunity)
        await ctx.send(embed=embed)

    except Exception as e:
        logger.error("비교 실패: %s", e)
        await ctx.send(f"❌ 비교 실패: {e}")


@bot.command(name="무신사")
async def cmd_musinsa_search(ctx: commands.Context, *, keyword: str = ""):
    """무신사 상품 검색."""
    if not keyword:
        await ctx.send("사용법: `!무신사 <키워드>`")
        return

    await ctx.send(f"🔍 무신사 검색 중... (`{keyword}`)")

    try:
        results = await musinsa_crawler.search_products(keyword)
        if not results:
            await ctx.send("검색 결과가 없습니다.")
            return

        lines = [f"**무신사 검색 결과** ({len(results)}건)\n"]
        for i, r in enumerate(results[:10], 1):
            price_str = f"{r['price']:,}원" if r.get("price") else "가격 미확인"
            lines.append(f"**{i}.** {r['brand']} — {r['name']}\n   {price_str} | [링크]({r['url']})")

        if len(results) > 10:
            lines.append(f"\n*... 외 {len(results) - 10}건*")

        await ctx.send("\n".join(lines))

    except Exception as e:
        logger.error("무신사 검색 실패: %s", e)
        await ctx.send(f"❌ 검색 실패: {e}")


@bot.command(name="키워드")
async def cmd_keywords(ctx: commands.Context):
    """모니터링 키워드 목록."""
    keywords = await bot.db.get_active_keywords()
    if not keywords:
        await ctx.send("등록된 키워드가 없습니다. `!키워드추가 <키워드>`로 추가하세요.")
        return

    lines = ["**모니터링 키워드 목록**\n"]
    for i, kw in enumerate(keywords, 1):
        lines.append(f"{i}. `{kw}`")
    await ctx.send("\n".join(lines))


@bot.command(name="키워드추가")
async def cmd_add_keyword(ctx: commands.Context, *, keyword: str = ""):
    """모니터링 키워드 추가."""
    if not keyword:
        await ctx.send("사용법: `!키워드추가 <키워드>`")
        return

    success = await bot.db.add_keyword(keyword.strip())
    if success:
        await ctx.send(f"✅ 키워드 추가: `{keyword.strip()}`")
    else:
        await ctx.send(f"⚠️ 이미 등록된 키워드입니다: `{keyword.strip()}`")


@bot.command(name="키워드삭제")
async def cmd_remove_keyword(ctx: commands.Context, *, keyword: str = ""):
    """모니터링 키워드 삭제."""
    if not keyword:
        await ctx.send("사용법: `!키워드삭제 <키워드>`")
        return

    success = await bot.db.remove_keyword(keyword.strip())
    if success:
        await ctx.send(f"✅ 키워드 삭제: `{keyword.strip()}`")
    else:
        await ctx.send(f"⚠️ 등록되지 않은 키워드입니다: `{keyword.strip()}`")


@bot.command(name="설정")
async def cmd_settings(ctx: commands.Context):
    """현재 설정 조회."""
    fees = settings.fees
    signals = settings.signals

    embed = discord.Embed(title="⚙️ 현재 설정", color=0x5865F2)

    embed.add_field(
        name="수수료",
        value=(
            f"판매수수료: {fees.sell_fee_rate:.0%}\n"
            f"부가세: {fees.sell_fee_vat_rate:.0%}\n"
            f"검수비: {fees.inspection_fee:,}원\n"
            f"크림배송: {fees.kream_shipping_fee:,}원\n"
            f"사업자택배: {settings.shipping_cost_to_kream:,}원"
        ),
        inline=True,
    )

    embed.add_field(
        name="시그널 기준",
        value=(
            f"🔴 강력매수: ≥{signals.strong_buy_profit:,}원 & {signals.strong_buy_volume_7d}건\n"
            f"🟠 매수: ≥{signals.buy_profit:,}원 & {signals.buy_volume_7d}건\n"
            f"🟡 관망: ≥{signals.watch_profit:,}원\n"
            f"⚪ 비추천: 그 외 또는 거래량 < {signals.min_volume_7d}건"
        ),
        inline=True,
    )

    embed.add_field(
        name="크롤링",
        value=(
            f"스캔 주기: {settings.scan_interval_minutes}분\n"
            f"집중추적: {settings.fast_scan_interval_minutes}분\n"
            f"딜레이: {settings.request_delay_min}~{settings.request_delay_max}초"
        ),
        inline=False,
    )

    await ctx.send(embed=embed)


@bot.command(name="리포트")
async def cmd_report(ctx: commands.Context):
    """수동 일일 리포트 생성."""
    stats = bot.daily_stats
    top = sorted(stats["opportunities"], key=lambda o: -o.best_profit)[:5]

    embed = format_daily_report(
        scan_count=stats["scan_count"],
        product_count=stats["product_count"],
        opportunity_count=stats["opportunity_count"],
        top_products=top,
    )
    await ctx.send(embed=embed)


@bot.command(name="상태")
async def cmd_status(ctx: commands.Context):
    """봇 상태 확인."""
    await ctx.send("🔍 상태 확인 중...")

    is_chrome = cdp_manager.is_connected
    is_kream = kream_crawler.is_active
    is_musinsa = False

    try:
        is_musinsa = await musinsa_crawler.check_login_status()
    except Exception:
        pass

    keywords = await bot.db.get_active_keywords()
    cursor = await bot.db.db.execute("SELECT COUNT(*) as cnt FROM kream_products")
    row = await cursor.fetchone()
    product_count = row["cnt"] if row else 0

    uptime = str(datetime.now() - bot.start_time).split(".")[0]

    embed = format_status(
        is_chrome_connected=is_chrome,
        is_kream_logged_in=is_kream,
        is_musinsa_logged_in=is_musinsa,
        keyword_count=len(keywords),
        db_product_count=product_count,
        uptime=uptime,
    )

    # 스케줄러 정보 추가
    if bot.scheduler:
        tracking_count = len(bot.scheduler._tracking)
        scan_running = bot.scheduler.periodic_scan.is_running()
        auto_scan_running = bot.scheduler.auto_scan_loop.is_running()
        embed.add_field(
            name="스케줄러",
            value=(
                f"**키워드 스캔:** {'🟢 실행 중' if scan_running else '🔴 중지'} ({settings.scan_interval_minutes}분)\n"
                f"**자동스캔:** {'🟢 실행 중' if auto_scan_running else '🔴 중지'} ({settings.auto_scan_interval_minutes}분)\n"
                f"**집중 추적:** {tracking_count}개 상품\n"
                f"**오늘 스캔:** {bot.daily_stats['scan_count']}회"
            ),
            inline=False,
        )

    # 최근 에러 요약
    recent_errors = error_aggregator.get_recent(minutes=60)
    if recent_errors:
        embed.add_field(
            name="최근 1시간 에러",
            value=f"{len(recent_errors)}건",
            inline=True,
        )

    await ctx.send(embed=embed)


@bot.command(name="스케줄러시작")
async def cmd_scheduler_start(ctx: commands.Context):
    """자동 스캔 스케줄러 시작."""
    if not bot.scheduler:
        await ctx.send("❌ 스케줄러가 초기화되지 않았습니다.")
        return

    if bot.scheduler.periodic_scan.is_running():
        await ctx.send("⚠️ 스케줄러가 이미 실행 중입니다.")
        return

    bot.scheduler.start()
    await ctx.send(
        f"✅ 스케줄러 시작\n"
        f"• 자동 스캔: {settings.scan_interval_minutes}분 주기\n"
        f"• 집중 추적: {settings.fast_scan_interval_minutes}분 주기\n"
        f"• 헬스체크: 5분 주기"
    )


@bot.command(name="스케줄러중지")
async def cmd_scheduler_stop(ctx: commands.Context):
    """자동 스캔 스케줄러 중지."""
    if not bot.scheduler:
        await ctx.send("❌ 스케줄러가 초기화되지 않았습니다.")
        return

    if not bot.scheduler.periodic_scan.is_running():
        await ctx.send("⚠️ 스케줄러가 이미 중지 상태입니다.")
        return

    bot.scheduler.stop()
    await ctx.send("⏹️ 스케줄러 중지 완료. 자동 스캔이 비활성화됩니다.")


@bot.command(name="추적목록")
async def cmd_tracking_list(ctx: commands.Context):
    """집중 추적 중인 상품 목록."""
    if not bot.scheduler:
        await ctx.send("❌ 스케줄러가 초기화되지 않았습니다.")
        return

    tracking = bot.scheduler._tracking
    expires = bot.scheduler._tracking_expires

    if not tracking:
        await ctx.send("📋 집중 추적 중인 상품이 없습니다.")
        return

    embed = discord.Embed(
        title=f"🔍 집중 추적 목록 ({len(tracking)}건)",
        color=0xFF8C00,
    )

    for pid, op in tracking.items():
        exp = expires.get(pid)
        exp_str = exp.strftime("%H:%M") if exp else "?"
        remaining = ""
        if exp:
            delta = exp - datetime.now()
            if delta.total_seconds() > 0:
                mins = int(delta.total_seconds() // 60)
                remaining = f" (잔여 {mins}분)"
            else:
                remaining = " (만료 대기)"

        signal_emoji = {"강력매수": "🔴", "매수": "🟠", "관망": "🟡"}.get(
            op.signal.value, "⚪"
        )
        embed.add_field(
            name=f"{signal_emoji} {op.kream_product.name}",
            value=(
                f"**모델번호:** `{op.kream_product.model_number}`\n"
                f"**최고 수익:** {op.best_profit:,}원\n"
                f"**만료:** {exp_str}{remaining}"
            ),
            inline=False,
        )

    await ctx.send(embed=embed)


@bot.command(name="크롬상태")
async def cmd_chrome_health(ctx: commands.Context):
    """Chrome 상태 및 복구 이력 조회."""
    from src.utils.resilience import chrome_health

    is_connected = cdp_manager.is_connected
    status_icon = "🟢" if is_connected else "🔴"
    summary = chrome_health.get_health_summary()

    embed = discord.Embed(
        title=f"{status_icon} Chrome 상태",
        color=0x00FF00 if is_connected else 0xFF0000,
    )
    embed.add_field(
        name="CDP 연결",
        value="연결됨" if is_connected else "연결 끊김",
        inline=True,
    )
    embed.add_field(
        name="상세 정보",
        value=f"```\n{summary}\n```",
        inline=False,
    )

    await ctx.send(embed=embed)


@bot.command(name="도움")
async def cmd_help(ctx: commands.Context):
    """도움말 표시."""
    embed = format_help()
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(ctx: commands.Context, error):
    """명령어 에러 처리."""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❌ 알 수 없는 명령어입니다. `!도움`을 입력하세요.")
    else:
        logger.error("명령어 오류 (%s): %s", ctx.command, error)
        await ctx.send(f"❌ 오류 발생: {error}")
