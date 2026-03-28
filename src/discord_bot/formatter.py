"""디스코드 Embed 메시지 포매터.

수익 기회, 가격 변동, 일일 리포트 등 알림 메시지를 Discord Embed로 생성한다.
"""

from datetime import datetime

import discord

from src.models.product import (
    AutoScanOpportunity,
    AutoScanSizeProfit,
    ProfitOpportunity,
    Signal,
    SizeProfitResult,
)
from src.price_tracker import PriceChange
from src.profit_calculator import calculate_kream_fees

# 시그널별 색상
SIGNAL_COLORS = {
    Signal.STRONG_BUY: 0xFF0000,  # 빨강
    Signal.BUY: 0xFF8C00,  # 주황
    Signal.WATCH: 0xFFD700,  # 노랑
    Signal.NOT_RECOMMENDED: 0x808080,  # 회색
}

SIGNAL_EMOJI = {
    Signal.STRONG_BUY: "🔴",
    Signal.BUY: "🟠",
    Signal.WATCH: "🟡",
    Signal.NOT_RECOMMENDED: "⚪",
}


def format_profit_alert(opportunity: ProfitOpportunity) -> discord.Embed:
    """수익 기회 알림 Embed 생성."""
    kp = opportunity.kream_product
    signal = opportunity.signal
    emoji = SIGNAL_EMOJI[signal]

    embed = discord.Embed(
        title=f"{emoji} [{signal.value}] {kp.name}",
        url=kp.url,
        color=SIGNAL_COLORS[signal],
        timestamp=opportunity.detected_at,
    )

    if kp.image_url:
        embed.set_thumbnail(url=kp.image_url)

    # 기본 정보
    embed.add_field(
        name="상품 정보",
        value=(
            f"**모델번호:** `{kp.model_number}`\n"
            f"**브랜드:** {kp.brand}\n"
            f"**최고 순수익:** **{opportunity.best_profit:,}원** (ROI {opportunity.best_roi}%)"
        ),
        inline=False,
    )

    # 구매처 정보
    retail_lines = []
    for rp in opportunity.retail_products:
        prices = [s.price for s in rp.sizes if s.in_stock]
        if prices:
            min_price = min(prices)
            discount_info = ""
            if rp.sizes and rp.sizes[0].discount_type:
                discount_info = f" ({rp.sizes[0].discount_type}"
                if rp.sizes[0].discount_rate > 0:
                    discount_info += f" {rp.sizes[0].discount_rate:.0%}"
                discount_info += ")"
            retail_lines.append(
                f"**{rp.source}** — {min_price:,}원{discount_info}\n[구매 링크]({rp.url})"
            )
    if retail_lines:
        embed.add_field(name="구매처", value="\n".join(retail_lines), inline=False)

    # 사이즈별 수익 테이블
    size_lines = _format_size_table(opportunity.size_profits)
    # Discord Embed 필드 값은 1024자 제한
    if len(size_lines) > 1024:
        # 수익 높은 상위 10개만
        size_lines = _format_size_table(opportunity.size_profits[:10])
        size_lines += "\n*... 상위 10개만 표시*"
    embed.add_field(name="사이즈별 수익", value=size_lines, inline=False)

    # 거래 활성도
    trade_info = (
        f"**7일 거래량:** {kp.volume_7d}건\n"
        f"**30일 거래량:** {kp.volume_30d}건\n"
    )
    if kp.last_trade_date:
        trade_info += f"**마지막 거래:** {kp.last_trade_date.strftime('%m/%d')}\n"
    if kp.price_trend:
        trend_emoji = {"상승": "📈", "하락": "📉", "보합": "➡️"}.get(kp.price_trend, "")
        trade_info += f"**추세:** {trend_emoji} {kp.price_trend}"
    embed.add_field(name="거래 활성도", value=trade_info, inline=True)

    # 수수료 상세
    best_sp = opportunity.size_profits[0] if opportunity.size_profits else None
    if best_sp:
        fee_info = (
            f"판매수수료: {best_sp.sell_fee:,}원\n"
            f"검수비: {best_sp.inspection_fee:,}원\n"
            f"크림배송: {best_sp.kream_shipping_fee:,}원\n"
            f"택배비: {best_sp.seller_shipping_fee:,}원\n"
            f"**총비용: {best_sp.total_cost:,}원**"
        )
        embed.add_field(name="수수료 상세 (최고수익 기준)", value=fee_info, inline=True)

    embed.set_footer(text=f"감지 시각 • 크림 ID: {kp.product_id}")

    return embed


def _format_size_table(size_profits: list[SizeProfitResult]) -> str:
    """사이즈별 수익 테이블 문자열."""
    lines = ["```"]
    lines.append(f"{'사이즈':>5} {'구매가':>8} {'판매가':>8} {'순수익':>8} {'ROI':>6} {'재고':>2}")
    lines.append("-" * 46)

    for sp in size_profits:
        stock = "O" if sp.in_stock else "X"
        signal = SIGNAL_EMOJI.get(sp.signal, "")
        lines.append(
            f"{sp.size:>5} {sp.retail_price:>7,} {sp.kream_sell_price:>7,} "
            f"{sp.net_profit:>+7,} {sp.roi:>5.1f}% {stock:>2}"
        )

    lines.append("```")
    return "\n".join(lines)


def format_price_change_alert(changes: list[PriceChange]) -> discord.Embed:
    """가격 변동 알림 Embed."""
    if not changes:
        return discord.Embed(title="가격 변동 없음", color=0x808080)

    first = changes[0]
    color = 0x00FF00 if first.profit_diff > 0 else 0xFF4444

    embed = discord.Embed(
        title=f"💱 가격 변동 — {first.product_name}",
        color=color,
        timestamp=first.detected_at,
    )

    embed.add_field(
        name="상품 정보",
        value=f"**모델번호:** `{first.model_number}`\n**변동 출처:** {first.change_source}",
        inline=False,
    )

    change_lines = []
    for c in changes[:15]:  # 최대 15개 사이즈
        direction = "📈" if c.price_diff > 0 else "📉"
        signal_change = ""
        if c.old_signal != c.new_signal:
            signal_change = (
                f" | {SIGNAL_EMOJI.get(c.old_signal, '')} → {SIGNAL_EMOJI.get(c.new_signal, '')}"
            )

        change_lines.append(
            f"**[{c.size}]** {direction} {c.old_price:,} → {c.new_price:,}원 "
            f"({c.price_diff:+,}원)"
        )
        if c.old_profit or c.new_profit:
            change_lines.append(
                f"  순수익: {c.old_profit:,} → {c.new_profit:,}원 "
                f"({c.profit_diff:+,}원){signal_change}"
            )

    embed.add_field(name="변동 내역", value="\n".join(change_lines), inline=False)

    return embed


def format_daily_report(
    scan_count: int,
    product_count: int,
    opportunity_count: int,
    top_products: list[ProfitOpportunity],
    date: datetime | None = None,
) -> discord.Embed:
    """일일 리포트 Embed."""
    if date is None:
        date = datetime.now()

    embed = discord.Embed(
        title=f"📊 일일 리포트 — {date.strftime('%Y-%m-%d')}",
        color=0x5865F2,
        timestamp=date,
    )

    embed.add_field(
        name="오늘의 요약",
        value=(
            f"**스캔 횟수:** {scan_count}회\n"
            f"**비교 상품:** {product_count}개\n"
            f"**수익 기회:** {opportunity_count}건"
        ),
        inline=False,
    )

    if top_products:
        top_lines = []
        for i, op in enumerate(top_products[:5], 1):
            emoji = SIGNAL_EMOJI.get(op.signal, "")
            top_lines.append(
                f"**{i}.** {emoji} {op.kream_product.name}\n"
                f"   순수익 {op.best_profit:,}원 (ROI {op.best_roi}%)"
            )
        embed.add_field(
            name="TOP 5 수익 기회",
            value="\n".join(top_lines),
            inline=False,
        )
    else:
        embed.add_field(name="TOP 5 수익 기회", value="오늘 발견된 수익 기회가 없습니다.", inline=False)

    return embed


def format_product_detail(
    opportunity: ProfitOpportunity,
) -> discord.Embed:
    """상품 상세 조회 결과 Embed (!크림 명령어용)."""
    kp = opportunity.kream_product

    embed = discord.Embed(
        title=kp.name,
        url=kp.url,
        color=SIGNAL_COLORS.get(opportunity.signal, 0x5865F2),
    )

    if kp.image_url:
        embed.set_thumbnail(url=kp.image_url)

    embed.add_field(
        name="기본 정보",
        value=(
            f"**모델번호:** `{kp.model_number}`\n"
            f"**브랜드:** {kp.brand}\n"
            f"**카테고리:** {kp.category}"
        ),
        inline=True,
    )

    embed.add_field(
        name="거래 정보",
        value=(
            f"**7일:** {kp.volume_7d}건\n"
            f"**30일:** {kp.volume_30d}건\n"
            f"**추세:** {kp.price_trend or '정보없음'}"
        ),
        inline=True,
    )

    if opportunity.size_profits:
        size_lines = _format_size_table(opportunity.size_profits)
        embed.add_field(name="사이즈별 분석", value=size_lines, inline=False)

    if opportunity.retail_products:
        retail_lines = []
        for rp in opportunity.retail_products:
            retail_lines.append(f"**{rp.source}** — [링크]({rp.url})")
        embed.add_field(name="구매처", value="\n".join(retail_lines), inline=False)

    return embed


def format_auto_scan_alert(opportunity: AutoScanOpportunity) -> discord.Embed:
    """자동스캔 수익 기회 알림 Embed (2단계 분석).

    확정 수익 (즉시판매) + 예상 수익 (시세 기반) 동시 표시.
    """
    kp = opportunity.kream_product
    confirmed_roi = opportunity.best_confirmed_roi
    estimated_roi = opportunity.best_estimated_roi

    # 알림 등급 결정
    if confirmed_roi >= 5:
        color = 0xFF0000  # 빨강 - 확정 수익
        title_prefix = "🔴 [즉시판매 가능]"
        urgency = "긴급"
    elif estimated_roi >= 10:
        color = 0xFF8C00  # 주황 - 예상 수익
        title_prefix = "🟠 [시세 기반 수익]"
        urgency = "참고"
    elif estimated_roi >= 5:
        color = 0xFFD700  # 노랑 - 관망
        title_prefix = "🟡 [수익 가능성]"
        urgency = "참고"
    else:
        color = 0x808080
        title_prefix = "⚪ [수익 낮음]"
        urgency = "정보"

    embed = discord.Embed(
        title=f"{title_prefix} {kp.name}",
        url=kp.url,
        color=color,
        timestamp=opportunity.detected_at,
    )

    if kp.image_url:
        embed.set_thumbnail(url=kp.image_url)

    # 상품 정보
    embed.add_field(
        name="상품 정보",
        value=(
            f"**모델번호:** `{kp.model_number}`\n"
            f"**브랜드:** {kp.brand}\n"
            f"**사이즈:** {len(opportunity.size_profits)}개 매칭"
        ),
        inline=False,
    )

    # 수익 분석 (2단계)
    profit_lines = []

    if opportunity.best_confirmed_profit != 0:
        sign = "+" if opportunity.best_confirmed_profit > 0 else ""
        profit_lines.append(
            f"**[확정] 즉시판매 수익:** {sign}{opportunity.best_confirmed_profit:,}원 "
            f"(ROI {opportunity.best_confirmed_roi}%)"
        )

    if opportunity.best_estimated_profit != 0:
        sign = "+" if opportunity.best_estimated_profit > 0 else ""
        profit_lines.append(
            f"**[참고] 시세기반 예상:** {sign}{opportunity.best_estimated_profit:,}원 "
            f"(ROI {opportunity.best_estimated_roi}%)"
        )

    if profit_lines:
        embed.add_field(name="수익 분석", value="\n".join(profit_lines), inline=False)

    # 매입처 정보
    if opportunity.musinsa_url:
        embed.add_field(
            name="무신사 매입",
            value=(
                f"**상품명:** {opportunity.musinsa_name}\n"
                f"[무신사 구매 링크]({opportunity.musinsa_url})"
            ),
            inline=True,
        )

    # 리스크 지표
    trend_emoji = {"상승": "📈", "하락": "📉", "보합": "➡️"}.get(
        opportunity.price_trend, "❓"
    )
    risk_lines = [
        f"**7일 거래량:** {opportunity.volume_7d}건",
        f"**가격 추세:** {trend_emoji} {opportunity.price_trend or '정보없음'}",
    ]
    if opportunity.bid_ask_spread > 0:
        stability = "안정" if opportunity.bid_ask_spread < 10000 else "불안정"
        risk_lines.append(
            f"**시세 안정성:** {opportunity.bid_ask_spread:,}원 차이 ({stability})"
        )
    embed.add_field(name="리스크 지표", value="\n".join(risk_lines), inline=True)

    # 사이즈별 수익 테이블
    size_table = _format_auto_scan_size_table(opportunity.size_profits)
    if len(size_table) > 1024:
        size_table = _format_auto_scan_size_table(opportunity.size_profits[:8])
        size_table += "\n*... 상위 8개만 표시*"
    embed.add_field(name="사이즈별 수익", value=size_table, inline=False)

    # 링크
    embed.add_field(
        name="링크",
        value=(
            f"[크림 상품 페이지]({kp.url})"
            + (f" | [무신사 구매]({opportunity.musinsa_url})" if opportunity.musinsa_url else "")
        ),
        inline=False,
    )

    embed.set_footer(text=f"{urgency} • 크림 ID: {kp.product_id}")

    return embed


def _format_auto_scan_size_table(size_profits: list[AutoScanSizeProfit]) -> str:
    """자동스캔 사이즈별 수익 테이블."""
    lines = ["```"]
    lines.append(f"{'사이즈':>5} {'무신사':>8} {'확정수익':>8} {'예상수익':>8} {'입찰':>3}")
    lines.append("-" * 42)

    for sp in size_profits:
        confirmed = f"{sp.confirmed_profit:>+7,}" if sp.confirmed_profit else "    N/A"
        estimated = f"{sp.estimated_profit:>+7,}" if sp.estimated_profit else "    N/A"
        lines.append(
            f"{sp.size:>5} {sp.musinsa_price:>7,} {confirmed} {estimated} {sp.bid_count:>3}"
        )

    lines.append("```")
    return "\n".join(lines)


def format_auto_scan_summary(
    kream_scanned: int,
    musinsa_searched: int,
    matched: int,
    confirmed_count: int,
    estimated_count: int,
    total_opportunities: int,
    elapsed_seconds: float,
    errors: int = 0,
) -> discord.Embed:
    """자동스캔 완료 요약 Embed."""
    embed = discord.Embed(
        title="🔄 자동스캔 완료",
        color=0x5865F2,
        timestamp=datetime.now(),
    )

    embed.add_field(
        name="스캔 결과",
        value=(
            f"**크림 조회:** {kream_scanned}개\n"
            f"**무신사 검색:** {musinsa_searched}개\n"
            f"**매칭 성공:** {matched}개\n"
            f"**소요 시간:** {elapsed_seconds:.0f}초"
        ),
        inline=True,
    )

    embed.add_field(
        name="수익 기회",
        value=(
            f"**전체:** {total_opportunities}건\n"
            f"🔴 **확정 수익:** {confirmed_count}건\n"
            f"🟠 **예상 수익:** {estimated_count}건"
        ),
        inline=True,
    )

    if errors > 0:
        embed.add_field(
            name="오류",
            value=f"⚠️ {errors}건",
            inline=True,
        )

    return embed


def format_status(
    is_chrome_connected: bool,
    is_kream_logged_in: bool,
    is_musinsa_logged_in: bool,
    keyword_count: int,
    db_product_count: int,
    uptime: str,
) -> discord.Embed:
    """봇 상태 Embed (!상태 명령어용)."""
    embed = discord.Embed(title="🤖 봇 상태", color=0x5865F2)

    status_icon = lambda ok: "🟢" if ok else "🔴"

    embed.add_field(
        name="연결 상태",
        value=(
            f"{status_icon(is_chrome_connected)} Chrome CDP\n"
            f"{status_icon(is_kream_logged_in)} 크림 로그인\n"
            f"{status_icon(is_musinsa_logged_in)} 무신사 로그인"
        ),
        inline=True,
    )

    embed.add_field(
        name="데이터",
        value=(
            f"**모니터링 키워드:** {keyword_count}개\n"
            f"**DB 상품 수:** {db_product_count}개\n"
            f"**가동 시간:** {uptime}"
        ),
        inline=True,
    )

    return embed


def format_help() -> discord.Embed:
    """도움말 Embed (!도움 명령어용)."""
    embed = discord.Embed(
        title="📖 크림봇 명령어 도움말",
        color=0x5865F2,
    )

    commands = {
        "!자동스캔": "크림 인기상품 기준 1회 전체 스캔 (크림→무신사)",
        "!자동스캔시작": "30분 간격 자동스캔 반복 시작",
        "!자동스캔중지": "자동스캔 반복 중지",
        "!스캔": "모니터링 키워드 기반 즉시 스캔 (무신사→크림)",
        "!크림 <상품ID>": "크림 상품 상세 조회",
        "!비교 <모델번호>": "모델번호로 전 사이트 가격 비교",
        "!무신사 <키워드>": "무신사 상품 검색",
        "!키워드": "모니터링 키워드 목록",
        "!키워드추가 <키워드>": "모니터링 키워드 추가",
        "!키워드삭제 <키워드>": "모니터링 키워드 삭제",
        "!설정": "현재 설정 조회",
        "!리포트": "수동 일일 리포트 생성",
        "!상태": "봇 상태 확인",
        "!스케줄러시작": "키워드 기반 스케줄러 시작",
        "!스케줄러중지": "키워드 기반 스케줄러 중지",
        "!추적목록": "집중 추적 중인 상품 목록",
        "!크롬상태": "Chrome 상태 및 복구 이력",
        "!DB구축": "크림 전체 상품 DB 구축 (!DB구축 전체)",
        "!도움": "이 도움말 표시",
    }

    cmd_text = "\n".join(f"`{cmd}` — {desc}" for cmd, desc in commands.items())
    embed.add_field(name="명령어 목록", value=cmd_text, inline=False)

    embed.set_footer(text="크림 리셀 수익 모니터링 봇")

    return embed
