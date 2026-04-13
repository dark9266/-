"""나이키 푸시 어댑터 테스트 (Phase 3 배치 1).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
무신사 어댑터 테스트 패턴을 그대로 복제.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.nike_adapter import NikeAdapter
from src.core.call_throttle import CallThrottle
from src.core.checkpoint_store import CheckpointStore
from src.core.event_bus import (
    AlertSent,
    CandidateMatched,
    CatalogDumped,
    EventBus,
    ProfitFound,
)
from src.core.orchestrator import Orchestrator

# ─── DB 헬퍼 ──────────────────────────────────────────────

_KREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS kream_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_number TEXT NOT NULL,
    brand TEXT DEFAULT '',
    category TEXT DEFAULT 'sneakers',
    image_url TEXT DEFAULT '',
    url TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kream_collect_queue (
    model_number TEXT PRIMARY KEY,
    brand_hint TEXT DEFAULT '',
    name_hint TEXT DEFAULT '',
    source TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0
);
"""


def _init_kream_db(path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_KREAM_SCHEMA)
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO kream_products "
                "(product_id, name, model_number, brand) VALUES (?, ?, ?, ?)",
                (r["product_id"], r["name"], r["model_number"], r.get("brand", "")),
            )
        conn.commit()
    finally:
        conn.close()


def _count_queue(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM kream_collect_queue")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


# ─── mock HTTP 레이어 ─────────────────────────────────────

class _FakeNikeHttp:
    """카테고리 slug → 상품 dict 리스트 고정 응답."""

    def __init__(self, listings: dict[str, list[dict]]):
        self._listings = listings
        self.calls: list[tuple[str, int]] = []

    async def fetch_category_listing(
        self, category: str, max_pages: int = 1
    ) -> list[dict]:
        self.calls.append((category, max_pages))
        # 깊은 복사 방어 (호출자가 키를 덧붙이므로)
        return [dict(x) for x in self._listings.get(category, [])]


# ─── fixtures ─────────────────────────────────────────────

@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def kream_db(tmp_path):
    path = tmp_path / "kream.db"
    _init_kream_db(
        str(path),
        rows=[
            # 존재 — Nike Air Force 1 Low White 일반
            {
                "product_id": "101",
                "name": "Nike Air Force 1 Low White",
                "model_number": "CW2288-111",
                "brand": "Nike",
            },
            # 존재 — 콜라보 Travis Scott (가드 테스트용)
            {
                "product_id": "202",
                "name": "Nike Air Force 1 Travis Scott Cactus Jack",
                "model_number": "AQ4211-100",
                "brand": "Nike",
            },
        ],
    )
    return str(path)


# ─── (a) dump_catalog publish + LAUNCH 스킵 ───────────────

async def test_dump_catalog_skips_launch(bus, kream_db):
    """리스팅에 LAUNCH(드로우) 상품이 섞여 있어도 일반 상품만 파싱된다."""
    fake_http = _FakeNikeHttp(
        listings={
            "men-shoes": [
                {
                    "productCode": "CW2288-111",
                    "name": "Nike Air Force 1 Low / CW2288-111",
                    "price": 139000,
                    "url": "https://www.nike.com/kr/t/_/CW2288-111",
                    "isSoldOut": False,
                    # 일반 상품
                    "productType": "FOOTWEAR",
                    "publishType": "LIVE",
                },
                {
                    "productCode": "DZ5485-612",
                    "name": "Air Jordan 1 Retro Lost & Found / DZ5485-612",
                    "price": 239000,
                    "url": "https://www.nike.com/kr/t/_/DZ5485-612",
                    "isSoldOut": False,
                    # LAUNCH — 드로우, 스킵 대상
                    "publishType": "LAUNCH",
                },
                {
                    "productCode": "FZ0000-100",
                    "name": "Nike Dunk Draw / FZ0000-100",
                    "price": 149000,
                    "url": "https://www.nike.com/kr/t/_/FZ0000-100",
                    "isSoldOut": False,
                    "consumerReleaseType": "LAUNCH",
                },
            ],
        }
    )
    adapter = NikeAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men-shoes": "남성 신발"},
        max_pages=1,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "nike"
    # LAUNCH 2건 제외 후 1건만 남아야 함
    assert event.product_count == 1
    assert len(products) == 1
    assert products[0]["productCode"] == "CW2288-111"
    assert fake_http.calls == [("men-shoes", 1)]
    assert received and received[0].product_count == 1


# ─── (b) match_to_kream — 매칭/큐/가드 ────────────────────

async def test_match_to_kream_classifies_items(bus, kream_db):
    fake_http = _FakeNikeHttp(listings={})
    adapter = NikeAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men-shoes": "남성 신발"},
    )

    products = [
        # (1) 정상 매칭 — Nike AF1 일반
        {
            "productCode": "CW2288-111",
            "name": "Nike Air Force 1 Low / CW2288-111",
            "price": 139000,
            "url": "https://www.nike.com/kr/t/_/CW2288-111",
            "isSoldOut": False,
            "publishType": "LIVE",
        },
        # (2) 미등재 신상 → collect_queue
        {
            "productCode": "ZZ9999-100",
            "name": "Nike Dunk Low / ZZ9999-100",
            "price": 129000,
            "url": "https://www.nike.com/kr/t/_/ZZ9999-100",
            "isSoldOut": False,
            "publishType": "LIVE",
        },
        # (3) 콜라보 가드 차단 — 크림은 Travis Scott, 소싱은 일반명
        {
            "productCode": "AQ4211-100",
            "name": "Nike Air Force 1 White / AQ4211-100",
            "price": 149000,
            "url": "https://www.nike.com/kr/t/_/AQ4211-100",
            "isSoldOut": False,
            "publishType": "LIVE",
        },
        # (4) LAUNCH 제외
        {
            "productCode": "DZ5485-612",
            "name": "Air Jordan 1 Retro / DZ5485-612",
            "price": 239000,
            "url": "https://www.nike.com/kr/t/_/DZ5485-612",
            "isSoldOut": False,
            "publishType": "LAUNCH",
        },
        # (5) 품절 제외
        {
            "productCode": "DD7777-100",
            "name": "Nike Dunk / DD7777-100",
            "price": 99000,
            "url": "https://www.nike.com/kr/t/_/DD7777-100",
            "isSoldOut": True,
            "publishType": "LIVE",
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(products)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.dumped == 5
    assert stats.matched == 1
    assert stats.collected_to_queue == 1
    assert stats.skipped_guard == 1
    assert stats.launch_dropped == 1
    assert stats.soldout_dropped == 1
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert isinstance(cand, CandidateMatched)
    assert cand.source == "nike"
    assert cand.kream_product_id == 101
    assert cand.model_no == "CW2288-111"
    assert cand.retail_price == 139000
    assert "CW2288-111" in cand.url

    # collect_queue 에 ZZ9999-100 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (c) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeNikeHttp(
        listings={
            "men-shoes": [
                {
                    "productCode": "CW2288-111",
                    "name": "Nike Air Force 1 Low / CW2288-111",
                    "price": 139000,
                    "url": "https://www.nike.com/kr/t/_/CW2288-111",
                    "isSoldOut": False,
                    "publishType": "LIVE",
                },
                {
                    "productCode": "DD7777-100",
                    "name": "Nike Dunk / DD7777-100",
                    "price": 99000,
                    "url": "https://www.nike.com/kr/t/_/DD7777-100",
                    "isSoldOut": True,
                    "publishType": "LIVE",
                },
                {
                    "productCode": "DZ5485-612",
                    "name": "Air Jordan 1 / DZ5485-612",
                    "price": 239000,
                    "url": "https://www.nike.com/kr/t/_/DZ5485-612",
                    "isSoldOut": False,
                    "publishType": "LAUNCH",
                },
            ]
        }
    )
    adapter = NikeAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men-shoes": "남성 신발"},
    )
    stats = await adapter.run_once()
    # LAUNCH 는 dump 단계에서 제외 → match 에 진입하지 않음
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (d) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeNikeHttp(
        listings={
            "men-shoes": [
                {
                    "productCode": "CW2288-111",
                    "name": "Nike Air Force 1 Low / CW2288-111",
                    "price": 139000,
                    "url": "https://www.nike.com/kr/t/_/CW2288-111",
                    "isSoldOut": False,
                    "publishType": "LIVE",
                },
            ]
        }
    )
    adapter = NikeAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men-shoes": "남성 신발"},
    )

    ckpt_path = tmp_path / "ckpt.db"
    store = CheckpointStore(str(ckpt_path))
    await store.init()
    throttle = CallThrottle(rate_per_min=6000.0, burst=100)
    orch = Orchestrator(bus, store, throttle)

    alerts: list[AlertSent] = []

    async def catalog_handler(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        # 어댑터가 직접 publish 하므로 orchestrator 의 catalog_handler 는 noop.
        if False:
            yield  # pragma: no cover

    async def candidate_handler(
        event: CandidateMatched,
    ) -> ProfitFound | None:
        return ProfitFound(
            source=event.source,
            kream_product_id=event.kream_product_id,
            model_no=event.model_no,
            size=event.size or "270",
            retail_price=event.retail_price,
            kream_sell_price=200000,
            net_profit=40000,
            roi=0.28,
            signal="STRONG_BUY",
            volume_7d=10,
            url=event.url,
        )

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        sent = AlertSent(
            alert_id=len(alerts) + 1,
            kream_product_id=event.kream_product_id,
            signal=event.signal,
            fired_at=222.0,
        )
        alerts.append(sent)
        return sent

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        await adapter.run_once()

        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if alerts:
                break
            await asyncio.sleep(0.01)

        assert len(alerts) == 1
        sent = alerts[0]
        assert sent.kream_product_id == 101
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
