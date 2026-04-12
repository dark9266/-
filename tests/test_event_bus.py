"""이벤트 버스 단위 테스트 (Phase 2.2)."""

import asyncio

import pytest

from src.core.event_bus import (
    AlertFollowup,
    AlertSent,
    CandidateMatched,
    CatalogDumped,
    EventBus,
    ProfitFound,
)


class TestEventDataclasses:
    def test_catalog_dumped_fields(self):
        ev = CatalogDumped(source="musinsa", product_count=1080, dumped_at=1_700_000_000.0)
        assert ev.source == "musinsa"
        assert ev.product_count == 1080
        assert ev.dumped_at == 1_700_000_000.0

    def test_candidate_matched_fields(self):
        ev = CandidateMatched(
            source="musinsa",
            kream_product_id=12345,
            model_no="DZ5485-612",
            retail_price=159000,
            size="270",
            url="https://musinsa.com/p/1",
        )
        assert ev.model_no == "DZ5485-612"

    def test_profit_found_fields(self):
        ev = ProfitFound(
            source="musinsa",
            kream_product_id=12345,
            model_no="DZ5485-612",
            size="270",
            retail_price=159000,
            kream_sell_price=220000,
            net_profit=35000,
            roi=22.0,
            signal="BUY",
            volume_7d=8,
            url="https://musinsa.com/p/1",
        )
        assert ev.signal == "BUY"
        assert ev.net_profit == 35000

    def test_alert_sent_fields(self):
        ev = AlertSent(alert_id=1, kream_product_id=12345, signal="BUY", fired_at=1_700_000_000.0)
        assert ev.alert_id == 1

    def test_alert_followup_slot(self):
        ev = AlertFollowup(alert_id=1, checked_at=1_700_000_600.0)
        assert ev.alert_id == 1
        assert ev.checked_at == 1_700_000_600.0

    def test_events_are_frozen(self):
        ev = CatalogDumped(source="musinsa", product_count=1, dumped_at=0.0)
        with pytest.raises(Exception):
            ev.source = "nike"  # type: ignore


class TestEventBus:
    async def test_publish_subscribe_single(self):
        bus = EventBus()
        queue = bus.subscribe(CatalogDumped)
        ev = CatalogDumped(source="musinsa", product_count=1, dumped_at=0.0)
        await bus.publish(ev)
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received is ev

    async def test_fanout_multiple_subscribers(self):
        bus = EventBus()
        q1 = bus.subscribe(CatalogDumped)
        q2 = bus.subscribe(CatalogDumped)
        ev = CatalogDumped(source="nike", product_count=2, dumped_at=0.0)
        await bus.publish(ev)
        r1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        r2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert r1 is ev and r2 is ev

    async def test_topic_isolation(self):
        """다른 타입 구독자는 이벤트를 받지 않는다."""
        bus = EventBus()
        q_catalog = bus.subscribe(CatalogDumped)
        q_profit = bus.subscribe(ProfitFound)
        await bus.publish(CatalogDumped(source="nike", product_count=1, dumped_at=0.0))
        assert q_catalog.qsize() == 1
        assert q_profit.qsize() == 0

    async def test_publish_without_subscribers_is_noop(self):
        bus = EventBus()
        # 구독자 없는 타입 publish는 예외 없이 drop
        await bus.publish(CatalogDumped(source="nike", product_count=1, dumped_at=0.0))

    async def test_unsubscribe_removes_queue(self):
        bus = EventBus()
        queue = bus.subscribe(CatalogDumped)
        bus.unsubscribe(CatalogDumped, queue)
        await bus.publish(CatalogDumped(source="nike", product_count=1, dumped_at=0.0))
        assert queue.qsize() == 0

    async def test_publish_rejects_non_event(self):
        bus = EventBus()
        with pytest.raises(TypeError):
            await bus.publish("not an event")  # type: ignore

    async def test_stats_tracks_published_count(self):
        bus = EventBus()
        bus.subscribe(CatalogDumped)
        await bus.publish(CatalogDumped(source="a", product_count=1, dumped_at=0.0))
        await bus.publish(CatalogDumped(source="b", product_count=2, dumped_at=0.0))
        assert bus.published_count(CatalogDumped) == 2
