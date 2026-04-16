"""W컨셉 푸시 어댑터 테스트 (Phase 3 배치 3).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.wconcept_adapter import WconceptAdapter
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

class _FakeSize:
    def __init__(self, size: str, in_stock: bool = True):
        self.size = size
        self.in_stock = in_stock


class _FakeProduct:
    def __init__(self, sizes):
        self.sizes = sizes


class _FakeWconceptHttp:
    """W컨셉 크롤러의 `search_products` + `get_product_detail` mock."""

    def __init__(
        self,
        keyword_items: dict[str, list[dict]],
        pdp_sizes: dict[str, list[str]] | None = None,
    ):
        self._keyword_items = keyword_items
        self._pdp = pdp_sizes if pdp_sizes is not None else {}
        self.calls: list[dict] = []

    async def get_product_detail(self, product_id: str):
        sizes = self._pdp.get(product_id, ["270"])
        if not sizes:
            return None
        return _FakeProduct([_FakeSize(s, True) for s in sizes])

    async def search_products(
        self,
        keyword: str,
        limit: int = 40,
        page_no: int = 1,
    ) -> list[dict]:
        self.calls.append({
            "keyword": keyword,
            "limit": limit,
            "page_no": page_no,
        })
        all_items = list(self._keyword_items.get(keyword, []))
        start = (page_no - 1) * limit
        end = start + limit
        # 깊은 복사를 피하기 위해 dict 복제만 수행 (어댑터가 메타 필드 주입)
        return [dict(it) for it in all_items[start:end]]


def _w_item(
    item_cd: str,
    name: str,
    model_number: str,
    price: int = 129000,
    brand: str = "Nike",
    sold_out: bool = False,
) -> dict:
    return {
        "product_id": item_cd,
        "name": name,
        "brand": brand,
        "model_number": model_number,
        "price": price,
        "original_price": price,
        "url": f"https://www.wconcept.co.kr/Product/{item_cd}",
        "image_url": "",
        "is_sold_out": sold_out,
    }


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
            {
                "product_id": "101",
                "name": "Nike Air Force 1 Low White",
                "model_number": "CW2288-111",
                "brand": "Nike",
            },
            {
                "product_id": "202",
                "name": "Nike Air Force 1 Travis Scott Cactus Jack",
                "model_number": "AQ4211-100",
                "brand": "Nike",
            },
            {
                "product_id": "303",
                "name": "adidas Samba OG White Black",
                "model_number": "B75806",
                "brand": "adidas",
            },
        ],
    )
    return str(path)


# ─── (a) dump_catalog publish + 반환 ──────────────────────

async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeWconceptHttp(
        keyword_items={
            "Nike": [
                _w_item("WC5001", "Nike Air Force 1 Low White",
                        model_number="CW2288-111"),
                _w_item("WC5002", "Nike Dunk Low",
                        model_number="ZZ9999-100"),
            ],
        }
    )
    adapter = WconceptAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords={"Nike": "Nike"},
        max_pages=1,
        page_size=40,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "wconcept"
    assert event.product_count == 2
    assert len(products) == 2
    assert all(p.get("_brand_label") == "Nike" for p in products)
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["keyword"] == "Nike"
    assert received and received[0].product_count == 2


# ─── (b) 3케이스: 정상/미등재/콜라보 가드 ─────────────────

async def test_match_three_cases(bus, kream_db):
    fake_http = _FakeWconceptHttp(keyword_items={})
    adapter = WconceptAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords={"Nike": "Nike"},
    )

    products = [
        # (1) 정상 매칭 — Nike AF1
        _w_item("WC5001", "Nike Air Force 1 Low White [CW2288-111]",
                model_number="CW2288-111"),
        # (2) 미등재 신상 — 크림 DB 에 ZZ9999-100 없음 → collect_queue
        _w_item("WC5002", "Nike Dunk Low Panda [ZZ9999-100]",
                model_number="ZZ9999-100"),
        # (3) 콜라보 가드 차단 — 크림은 Travis Scott, 소싱은 일반명
        _w_item("WC5003", "Nike Air Force 1 Low White [AQ4211-100]",
                model_number="AQ4211-100"),
        # (4) 품절 제외
        _w_item("WC5004", "adidas Samba OG", model_number="B75806",
                sold_out=True),
        # (5) 모델번호 없음
        _w_item("WC5005", "이름만 있고 모델번호 없음", model_number=""),
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
    assert cand.source == "wconcept"
    assert cand.kream_product_id == 101
    assert cand.model_no == "CW2288-111"
    assert cand.retail_price == 129000
    assert "WC5001" in cand.url

    # 미등재 신상 1건만 큐에 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (c) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeWconceptHttp(
        keyword_items={
            "Nike": [
                _w_item("WC5001", "Nike Air Force 1 [CW2288-111]",
                        model_number="CW2288-111"),
                _w_item("WC5010", "Nike Dunk [DD7777-100]",
                        model_number="DD7777-100", sold_out=True),
            ],
        }
    )
    adapter = WconceptAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords={"Nike": "Nike"},
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (d) 페이지네이션 루프 확인 ───────────────────────────

async def test_pagination_iterates_multiple_pages(bus, kream_db):
    fake_http = _FakeWconceptHttp(
        keyword_items={
            "Nike": [
                _w_item("WC5001", "A CW2288-111", model_number="CW2288-111"),
                _w_item("WC5002", "B ZZ0001-100", model_number="ZZ0001-100"),
                _w_item("WC5003", "C ZZ0002-100", model_number="ZZ0002-100"),
            ],
        }
    )
    adapter = WconceptAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords={"Nike": "Nike"},
        max_pages=5,
        page_size=2,
    )
    _, products = await adapter.dump_catalog()
    assert len(products) == 3
    pages_called = [c["page_no"] for c in fake_http.calls]
    assert 1 in pages_called and 2 in pages_called


# ─── (e) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeWconceptHttp(
        keyword_items={
            "Nike": [
                _w_item("WC5001", "Nike Air Force 1 Low White [CW2288-111]",
                        model_number="CW2288-111"),
            ],
        }
    )
    adapter = WconceptAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords={"Nike": "Nike"},
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
            fired_at=111.0,
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
