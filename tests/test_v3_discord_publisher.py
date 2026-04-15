"""v3_discord_publisher 유닛 테스트 — 채널 send 경로 + 시그널 필터.

핵심 검증:
    - channel_send 주입 시 매수/강력매수만 embed dict 로 전달
    - 비추천/관망 은 channel_send / webhook 둘 다 호출 안 됨
    - channel_send 예외는 흡수 (다음 알림 차단 X)
    - channel_send 없고 webhook 만 있으면 httpx POST 경로 동작
"""

from __future__ import annotations

import pytest

from src.core.event_bus import ProfitFound
from src.core.v3_discord_publisher import V3DiscordPublisher, wrap_handler


def _make_event(signal: str, net_profit: int = 50_000, roi: float = 30.0) -> ProfitFound:
    return ProfitFound(
        source="musinsa",
        kream_product_id=1234,
        model_no="AR3565-004",
        size="270",
        retail_price=170_100,
        kream_sell_price=339_000,
        net_profit=net_profit,
        roi=roi,
        signal=signal,
        volume_7d=15,
        url="https://example.com/p/1",
    )


async def test_channel_send_receives_embed_for_strong_buy():
    captured: list[dict] = []

    async def _send(embed: dict) -> None:
        captured.append(embed)

    pub = V3DiscordPublisher(webhook_url=None, channel_send=_send)
    await pub.publish(_make_event("강력매수"))

    assert len(captured) == 1
    embed = captured[0]
    assert "[강력매수]" in embed["title"]
    assert embed["color"] == 0xFF0000  # 강력매수 red (v2 format 호환)
    assert "순수익" in embed["description"]
    assert any(f["name"] == "💰 가격" for f in embed["fields"])
    assert embed["url"] == "https://example.com/p/1"


async def test_channel_send_receives_embed_for_buy():
    captured: list[dict] = []

    async def _send(embed: dict) -> None:
        captured.append(embed)

    pub = V3DiscordPublisher(channel_send=_send)
    await pub.publish(_make_event("매수"))

    assert len(captured) == 1
    assert "[매수]" in captured[0]["title"]
    assert captured[0]["color"] == 0xFF8C00  # 매수 orange (v2 format 호환)


@pytest.mark.parametrize("signal", ["관망", "비추천"])
async def test_non_alert_signals_filtered(signal: str):
    captured: list[dict] = []

    async def _send(embed: dict) -> None:
        captured.append(embed)

    pub = V3DiscordPublisher(
        webhook_url="https://discord.example/wh",  # 있어도 호출되면 안 됨
        channel_send=_send,
    )
    await pub.publish(_make_event(signal))

    # channel_send 미호출 + webhook 도 POST 안 됨 (signal 필터가 최우선)
    assert captured == []


async def test_channel_send_exception_absorbed():
    """채널 send 실패해도 publish() 가 예외 전파하지 않아야 다음 알림 차단 안 됨."""

    async def _boom(embed: dict) -> None:
        raise RuntimeError("channel send failed")

    pub = V3DiscordPublisher(channel_send=_boom)
    # 예외 흡수 확인 — raise 되면 실패
    await pub.publish(_make_event("강력매수"))


async def test_channel_send_priority_over_webhook():
    """channel_send 주입 시 webhook 은 호출되지 않아야 한다."""
    captured: list[dict] = []
    webhook_calls: list[str] = []

    async def _send(embed: dict) -> None:
        captured.append(embed)

    class _FakeClient:
        is_closed = False

        async def post(self, url: str, json: dict):  # noqa: A002
            webhook_calls.append(url)
            raise AssertionError("webhook 이 호출되면 안 됨 — channel_send 가 우선")

    pub = V3DiscordPublisher(
        webhook_url="https://discord.example/wh",
        channel_send=_send,
        client=_FakeClient(),  # type: ignore[arg-type]
    )
    await pub.publish(_make_event("매수"))

    assert len(captured) == 1
    assert webhook_calls == []


async def test_no_op_when_neither_channel_nor_webhook():
    """channel_send 없고 webhook 도 없으면 조용히 skip (JSONL 만 남음)."""
    pub = V3DiscordPublisher()
    # 예외 없이 완료되어야 함
    await pub.publish(_make_event("강력매수"))


async def test_wrap_handler_publishes_on_new_alert():
    """wrap_handler: inner 가 AlertSent 반환 시에만 publish 호출."""
    captured: list[dict] = []

    async def _send(embed: dict) -> None:
        captured.append(embed)

    pub = V3DiscordPublisher(channel_send=_send)

    # inner 가 신규 알림으로 간주 (sentinel 객체 반환)
    sentinel = object()

    async def _inner_new(event):  # type: ignore[no-untyped-def]
        return sentinel

    wrapped = wrap_handler(_inner_new, pub)  # type: ignore[arg-type]
    await wrapped(_make_event("강력매수"))
    assert len(captured) == 1


async def test_wrap_handler_skips_publish_on_dedup():
    """inner 가 None 반환(= 중복/dedup) 이면 publish 안 함."""
    captured: list[dict] = []

    async def _send(embed: dict) -> None:
        captured.append(embed)

    pub = V3DiscordPublisher(channel_send=_send)

    async def _inner_dedup(event):  # type: ignore[no-untyped-def]
        return None

    wrapped = wrap_handler(_inner_dedup, pub)  # type: ignore[arg-type]
    await wrapped(_make_event("강력매수"))
    assert captured == []
