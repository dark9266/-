"""v3 ProfitFound → Discord 웹훅 발송 브릿지.

배경:
    `v3_alert_logger.py` 는 설계상 JSONL 파일 기록 전용이었음 (병행 운영
    기간에 v2 가 Discord 발송 담당이라는 가정). 그러나 푸시 트랙이 메인이
    되면서 v2 reverse 경로는 사실상 ProfitFound 를 발생시키지 않게 되어,
    v3 푸시 어댑터의 알림 (강력매수 포함) 이 파일에만 누워 있고 사용자
    채널로 전달되지 않는 누수가 발견됨 (2026-04-15).

설계:
    - `V3AlertLogger.log()` 는 그대로 두고 (forensic JSONL 유지)
    - 이 모듈은 alert_logger 의 handler 를 **wrap** 해서 AlertSent 가
      반환되면(= 신규 알림이면) 추가로 webhook POST 를 보낸다
    - dedup·중복 방지는 alert_logger / orchestrator 의 기존 경로가 담당
    - 외부 의존성: httpx (이미 프로젝트 표준). discord.py import 안 함

크림 호출 0건. 발송 실패는 흡수 (다음 알림 차단하지 않음).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from src.core.db import sync_connect
from src.core.event_bus import AlertSent, ProfitFound

logger = logging.getLogger(__name__)


ProfitHandler = Callable[[ProfitFound], Awaitable[AlertSent | None]]


# 시그널별 색상/이모지
_SIGNAL_COLOR: dict[str, int] = {
    "강력매수": 0xFF0000,  # 빨강 (v2 format_profit_alert 동일)
    "매수": 0xFF8C00,      # 주황
    "관망": 0xFFD700,      # 노랑
    "비추천": 0x808080,    # 회색
}

_SIGNAL_EMOJI: dict[str, str] = {
    "강력매수": "🔴",
    "매수": "🟠",
    "관망": "🟡",
    "비추천": "⚪",
}


_ALERT_SIGNALS: frozenset[str] = frozenset({"강력매수", "매수"})


def _lookup_kream_product(db_path: str, kream_product_id: int) -> dict | None:
    """크림 상품 메타 조회 (읽기 전용, 동기 SQLite — 수 ms)."""
    try:
        with sync_connect(db_path, read_only=True, timeout=2.0) as conn:
            row = conn.execute(
                "SELECT name, brand, category, image_url, url, volume_7d, volume_30d "
                "FROM kream_products WHERE product_id=?",
                (str(kream_product_id),),
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("[v3_discord] kream_products 조회 실패: %s", e)
        return None


def _build_embed(event: ProfitFound, product: dict | None = None) -> dict:
    color = _SIGNAL_COLOR.get(event.signal, 0x5865F2)
    emoji = _SIGNAL_EMOJI.get(event.signal, "")

    name = (product or {}).get("name") or event.model_no
    brand = (product or {}).get("brand") or ""
    image_url = (product or {}).get("image_url") or ""
    kream_url = (product or {}).get("url") or f"https://kream.co.kr/products/{event.kream_product_id}"
    volume_30d = (product or {}).get("volume_30d")

    title = f"{emoji} [{event.signal}] {name}"[:256]

    # 설명: 한눈에 들어오는 수익 요약 (굵게, 개행 포함)
    description_lines = [
        f"**모델번호:** `{event.model_no}`",
    ]
    if brand:
        description_lines.append(f"**브랜드:** {brand}")
    color_name = getattr(event, "color_name", "") or ""
    if color_name:
        description_lines.append(f"**🎨 색상:** {color_name}")
    description_lines.append(
        f"**순수익:** **{event.net_profit:,}원** (ROI {event.roi:.1f}%)"
    )
    description = "\n".join(description_lines)

    catch_applied = getattr(event, "catch_applied", False)
    original_retail = getattr(event, "original_retail", None)
    if catch_applied and original_retail:
        price_value = (
            f"💡 실측 결제가 **{event.retail_price:,}원**\n"
            f"   _(정가 {original_retail:,}원, 확장 catch)_\n"
            f"크림 즉시판매 **{event.kream_sell_price:,}원**\n"
            f"차액 **{event.kream_sell_price - event.retail_price:,}원**"
        )
    else:
        price_value = (
            f"소싱가 **{event.retail_price:,}원**\n"
            f"크림 즉시판매 **{event.kream_sell_price:,}원**\n"
            f"차액 **{event.kream_sell_price - event.retail_price:,}원**"
        )
    fields: list[dict] = [
        {
            "name": "💰 가격",
            "value": price_value,
            "inline": True,
        },
        {
            "name": "📊 거래량",
            "value": (
                f"7일 **{event.volume_7d}건**"
                + (f"\n30일 **{volume_30d}건**" if volume_30d is not None else "")
            ),
            "inline": True,
        },
    ]
    if event.size:
        fields.append({"name": "📏 대표 사이즈", "value": event.size, "inline": True})

    # 사이즈별 수익 테이블 — ProfitFound.size_profits (tuple of dict)
    size_profits = getattr(event, "size_profits", None) or ()
    if size_profits:
        lines: list[str] = []
        for row in size_profits[:12]:  # 최대 12 사이즈 (embed 1024자 제한 대비)
            try:
                sz = str(row.get("size") or "").strip() or "-"
                sell = int(row.get("kream_sell_price") or 0)
                profit = int(row.get("net_profit") or 0)
                roi_v = float(row.get("roi") or 0.0)
            except (TypeError, ValueError, AttributeError):
                continue
            marker = "✅" if profit >= 10000 else "⚠️"
            lines.append(
                f"{marker} `{sz:>6}` 즉시판매 {sell:,}원 → **{profit:,}원** (ROI {roi_v:.1f}%)"
            )
        if lines:
            value = "\n".join(lines)
            if len(value) > 1020:
                value = value[:1017] + "..."
            fields.append(
                {"name": "📐 사이즈별 수익", "value": value, "inline": False}
            )

    fields.append(
        {
            "name": "🔗 링크",
            "value": f"[크림]({kream_url}) | [{event.source}]({event.url})",
            "inline": False,
        }
    )

    embed: dict = {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {
            "text": f"{event.source} • 크림 ID: {event.kream_product_id}"
        },
    }
    if event.url:
        embed["url"] = event.url
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    return embed


ChannelSendFn = Callable[[dict], Awaitable[None]]
"""async 콜백 — embed dict 하나를 받아 discord.py 채널로 전송.

main.py 에서 `lambda embed: bot.get_channel(id).send(embed=Embed.from_dict(embed))`
형태로 주입한다. 여기서는 duck-typing — discord 라이브러리 import 금지.
"""


class V3DiscordPublisher:
    """ProfitHandler wrapper — alert_logger 다음에 embed 발송.

    경로 우선순위 (생성 시 하나 선택):
        1. channel_send 콜백 주입 → discord.py bot 채널로 직접 send
           (기존 CHANNEL_PROFIT_ALERT 채널 재사용, 추가 URL 불필요)
        2. webhook_url 문자열 → httpx POST
        3. 둘 다 없음 → no-op (JSONL 만 기록)
    """

    def __init__(
        self,
        webhook_url: str | None = None,
        *,
        channel_send: ChannelSendFn | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
        db_path: str | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._channel_send = channel_send
        self._client = client
        self._timeout = timeout
        self._owns_client = client is None
        self._db_path = db_path

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def publish(self, event: ProfitFound) -> None:
        """수익 알림 발송. 실패 흡수 — 다음 알림 차단 X.

        매수/강력매수 시그널만 발송. 관망/비추천은 forensic JSONL 에만 남기고
        사용자 채널 노이즈 차단.
        """
        if event.signal not in _ALERT_SIGNALS:
            return
        product = None
        if self._db_path:
            product = _lookup_kream_product(self._db_path, event.kream_product_id)
        embed = _build_embed(event, product)

        # 1순위: discord.py bot 채널 직접 send (기존 profit 채널 재사용)
        if self._channel_send is not None:
            try:
                await self._channel_send(embed)
                return
            except Exception as e:
                logger.warning("[v3_discord] 채널 send 예외: %s", e)
                return

        # 2순위: webhook POST
        if not self._webhook_url:
            return
        payload = {"embeds": [embed]}
        try:
            client = await self._get_client()
            resp = await client.post(self._webhook_url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "[v3_discord] webhook POST 실패 status=%d body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
        except httpx.HTTPError as e:
            logger.warning("[v3_discord] webhook POST 예외: %s", e)

    async def close(self) -> None:
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def wrap_handler(
    inner: ProfitHandler,
    publisher: V3DiscordPublisher,
) -> ProfitHandler:
    """기존 ProfitHandler 를 감싸 AlertSent 반환 시 webhook POST 추가.

    inner handler 가 None 반환(= 중복/dedup) 이면 publish 안 함.
    """

    async def _wrapped(event: ProfitFound) -> AlertSent | None:
        result = await inner(event)
        if result is not None:
            await publisher.publish(event)
        return result

    return _wrapped


__all__ = ["V3DiscordPublisher", "wrap_handler"]
