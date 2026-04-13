"""뉴발란스 푸시 어댑터 테스트 (Phase 3 배치 2).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.nbkorea_adapter import NbkoreaAdapter
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

class _FakeNbkoreaHttp:
    """fetch_category_catalog 만 mock — 고정 응답."""

    def __init__(self, catalog: list[dict]):
        self._catalog = catalog
        self.calls: list[tuple[dict[str, str], int]] = []

    async def fetch_category_catalog(
        self,
        categories: dict[str, str],
        max_items_per_category: int = 200,
    ) -> list[dict]:
        self.calls.append((dict(categories), max_items_per_category))
        return [dict(x) for x in self._catalog]


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
            # 일반 NB 모델 (U20024VT)
            {
                "product_id": "301",
                "name": "New Balance 2002R Protection Pack Rain Cloud",
                "model_number": "U20024VT",
                "brand": "New Balance",
            },
            # 콜라보 — Stussy (가드 테스트용, COLLAB_KEYWORDS 수록)
            {
                "product_id": "302",
                "name": "New Balance 2002R Stussy Water",
                "model_number": "M2002RST",
                "brand": "New Balance",
            },
        ],
    )
    return str(path)


# ─── (a) dump_catalog publish + 반환 ──────────────────────

async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeNbkoreaHttp(
        catalog=[
            {
                "display_name": "U20024VT",
                "style_code": "NBPDAA001F",
                "col_code": "01",
                "name": "U20024VT",
                "price": 189000,
                "original_price": 189000,
                "is_sold_out": False,
                "url": (
                    "https://www.nbkorea.com/product/productDetail.action"
                    "?styleCode=NBPDAA001F&colCode=01"
                ),
            },
            {
                "display_name": "M2002RXD",
                "style_code": "NBPDBB002F",
                "col_code": "02",
                "name": "M2002RXD",
                "price": 179000,
                "original_price": 179000,
                "is_sold_out": False,
                "url": (
                    "https://www.nbkorea.com/product/productDetail.action"
                    "?styleCode=NBPDBB002F&colCode=02"
                ),
            },
        ]
    )
    adapter = NbkoreaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"250110": "남성 신발"},
        max_items_per_category=50,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "nbkorea"
    assert event.product_count == 2
    assert len(products) == 2
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0][0] == {"250110": "남성 신발"}
    assert fake_http.calls[0][1] == 50
    assert received and received[0].product_count == 2


# ─── (b) match_to_kream — 매칭/큐/가드/품절 ───────────────

async def test_match_to_kream_classifies_items(bus, kream_db):
    fake_http = _FakeNbkoreaHttp(catalog=[])
    adapter = NbkoreaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )

    products = [
        # (1) 정상 매칭 — U20024VT
        {
            "display_name": "U20024VT",
            "style_code": "NBPDAA001F",
            "col_code": "01",
            "name": "U20024VT",
            "price": 189000,
            "is_sold_out": False,
            "url": "https://www.nbkorea.com/p/u20024vt",
        },
        # (2) 미등재 신상 → collect_queue
        {
            "display_name": "MT410XX9",
            "style_code": "NBPDFF003Z",
            "col_code": "05",
            "name": "MT410XX9",
            "price": 129000,
            "is_sold_out": False,
            "url": "https://www.nbkorea.com/p/mt410xx9",
        },
        # (3) 콜라보 가드 차단 — 크림 이름에는 Stussy, 소싱은 일반명
        {
            "display_name": "M2002RST",
            "style_code": "NBPDCC010Z",
            "col_code": "11",
            "name": "뉴발란스 2002R",
            "price": 219000,
            "is_sold_out": False,
            "url": "https://www.nbkorea.com/p/m2002rst",
        },
        # (4) 품절 제외
        {
            "display_name": "M2002RXD",
            "style_code": "NBPDBB002F",
            "col_code": "02",
            "name": "M2002RXD",
            "price": 179000,
            "is_sold_out": True,
            "url": "https://www.nbkorea.com/p/m2002rxd",
        },
        # (5) 모델번호 없음
        {
            "display_name": "",
            "style_code": "NBPDZZ999",
            "col_code": "99",
            "name": "알 수 없음",
            "price": 0,
            "is_sold_out": False,
            "url": "",
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
    assert stats.soldout_dropped == 1
    assert stats.no_model_number == 1
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert isinstance(cand, CandidateMatched)
    assert cand.source == "nbkorea"
    assert cand.kream_product_id == 301
    assert cand.model_no == "U20024VT"
    assert cand.retail_price == 189000
    assert "u20024vt" in cand.url.lower()

    # collect_queue 에 MT410XX9 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (c) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeNbkoreaHttp(
        catalog=[
            {
                "display_name": "U20024VT",
                "style_code": "NBPDAA001F",
                "col_code": "01",
                "name": "U20024VT",
                "price": 189000,
                "is_sold_out": False,
                "url": "https://www.nbkorea.com/p/u20024vt",
            },
            {
                "display_name": "M2002RXD",
                "style_code": "NBPDBB002F",
                "col_code": "02",
                "name": "M2002RXD",
                "price": 179000,
                "is_sold_out": True,
                "url": "https://www.nbkorea.com/p/m2002rxd",
            },
        ]
    )
    adapter = NbkoreaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (d) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeNbkoreaHttp(
        catalog=[
            {
                "display_name": "U20024VT",
                "style_code": "NBPDAA001F",
                "col_code": "01",
                "name": "U20024VT",
                "price": 189000,
                "is_sold_out": False,
                "url": "https://www.nbkorea.com/p/u20024vt",
            },
        ]
    )
    adapter = NbkoreaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
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
        # 어댑터가 직접 publish — catalog_handler 는 noop.
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
            kream_sell_price=250000,
            net_profit=45000,
            roi=0.23,
            signal="STRONG_BUY",
            volume_7d=8,
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
        assert sent.kream_product_id == 301
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
