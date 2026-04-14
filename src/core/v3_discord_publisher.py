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

import httpx

from src.core.event_bus import AlertSent, ProfitFound

logger = logging.getLogger(__name__)


ProfitHandler = Callable[[ProfitFound], Awaitable[AlertSent | None]]


# 시그널별 색상 (Discord embed) — Discord int color
_SIGNAL_COLOR: dict[str, int] = {
    "강력매수": 0x2ECC71,  # green
    "매수": 0x3498DB,      # blue
    "관망": 0xF1C40F,      # yellow
    "비추천": 0x95A5A6,    # gray
}


def _build_embed(event: ProfitFound) -> dict:
    color = _SIGNAL_COLOR.get(event.signal, 0x5865F2)
    title = f"[{event.signal}] {event.source} · {event.model_no}"
    desc_lines = [
        f"**순수익**: {event.net_profit:,}원 (ROI {event.roi:.1f}%)",
        f"**크림 즉시판매**: {event.kream_sell_price:,}원",
        f"**소싱가**: {event.retail_price:,}원",
        f"**거래량 7d**: {event.volume_7d}",
    ]
    if event.size:
        desc_lines.append(f"**사이즈**: {event.size}")
    return {
        "title": title[:256],
        "description": "\n".join(desc_lines),
        "url": event.url or None,
        "color": color,
    }


class V3DiscordPublisher:
    """ProfitHandler wrapper — alert_logger 다음에 webhook POST."""

    def __init__(
        self,
        webhook_url: str | None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._webhook_url = webhook_url
        self._client = client
        self._timeout = timeout
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def publish(self, event: ProfitFound) -> None:
        """webhook POST. 실패 흡수 — 다음 알림 차단 X."""
        if not self._webhook_url:
            return
        embed = _build_embed(event)
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
