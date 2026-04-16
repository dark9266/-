"""아크테릭스 푸시 어댑터 테스트 (Phase 3 배치 2).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.arcteryx_adapter import ArcteryxAdapter
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
    category TEXT DEFAULT 'apparel',
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


class _FakeArcteryxHttp:
    """`_list_raw` + `_options_raw` + `get_product_detail` mock."""

    def __init__(
        self,
        category_items: dict[str, list[dict]],
        options_by_id: dict[int | str, str] | None = None,
        pdp_sizes: dict[str, list[str]] | None = None,
    ):
        self._category_items = category_items
        self._options_by_id = options_by_id or {}
        self._pdp = pdp_sizes if pdp_sizes is not None else {}
        self.list_calls: list[dict] = []
        self.options_calls: list[int | str] = []

    async def get_product_detail(self, product_id: str):
        sizes = self._pdp.get(product_id, ["L"])
        if not sizes:
            return None
        return _FakeProduct([_FakeSize(s, True) for s in sizes])

    async def _list_raw(
        self,
        *,
        category: str,
        page_size: int = 60,
        page_number: int = 1,
    ) -> dict:
        self.list_calls.append(
            {
                "category": category,
                "page_size": page_size,
                "page_number": page_number,
            }
        )
        items_all = list(self._category_items.get(category, []))
        start = (page_number - 1) * page_size
        end = start + page_size
        return {"rows": items_all[start:end], "total": len(items_all)}

    async def _options_raw(self, *, product_id: int | str) -> dict:
        self.options_calls.append(product_id)
        code = self._options_by_id.get(product_id, "")
        # 아크테릭스 실제 응답 구조 모사
        return {
            "options": [
                {"level": 1, "code": code, "values": []},
                {"level": 2, "values": []},
            ]
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
            # 존재 — 아크테릭스 Beta Jacket 일반
            {
                "product_id": "701",
                "name": "Arcteryx Beta Jacket Black",
                "model_number": "X000007301",
                "brand": "Arc'teryx",
            },
            # 존재 — 콜라보 (Supreme x Arcteryx 가정 — COLLAB_KEYWORDS 매칭)
            {
                "product_id": "702",
                "name": "Arcteryx Beta Jacket Supreme Edition",
                "model_number": "X000009999",
                "brand": "Arc'teryx",
            },
            # 존재 — Alpha SV
            {
                "product_id": "703",
                "name": "Arcteryx Alpha SV Jacket Red",
                "model_number": "X000001111",
                "brand": "Arc'teryx",
            },
        ],
    )
    return str(path)


def _item(product_id: int, name: str, price: int = 890000,
          sale_state: str = "ON") -> dict:
    return {
        "product_id": product_id,
        "product_name": name,
        "sell_price": price,
        "retail_price": price,
        "sale_state": sale_state,
    }


# ─── (a) dump_catalog publish + 반환 ──────────────────────

async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeArcteryxHttp(
        category_items={
            "mens-jackets": [
                _item(5001, "Beta Jacket Black"),
                _item(5002, "Alpha SV Jacket Red"),
            ],
        }
    )
    adapter = ArcteryxAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"mens-jackets": "남성 자켓"},
        max_pages=1,
        page_size=100,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "arcteryx"
    assert event.product_count == 2
    assert len(products) == 2
    assert all(p.get("_category") == "mens-jackets" for p in products)
    assert len(fake_http.list_calls) == 1
    assert fake_http.list_calls[0]["category"] == "mens-jackets"
    assert received and received[0].product_count == 2


# ─── (b) 매칭 3케이스: 존재 / 미등재 / 콜라보 fail ────────

async def test_match_three_cases(bus, kream_db):
    fake_http = _FakeArcteryxHttp(
        category_items={},
        options_by_id={
            6001: "X000007301",   # 매칭 성공
            6002: "X000008888",   # 미등재
            6003: "X000009999",   # 콜라보 가드 (크림은 Palace Edition, 소싱은 일반명)
            6004: "X000001111",   # 품절
            6005: "",             # 모델번호 추출 실패
        },
    )
    adapter = ArcteryxAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"mens-jackets": "남성 자켓"},
    )

    products = [
        # (1) 정상 매칭
        {
            "product_id": 6001,
            "product_name": "Arcteryx Beta Jacket Black",
            "sell_price": 890000,
            "sale_state": "ON",
            "_category": "mens-jackets",
        },
        # (2) 미등재 → collect_queue
        {
            "product_id": 6002,
            "product_name": "Arcteryx Gamma Jacket",
            "sell_price": 550000,
            "sale_state": "ON",
            "_category": "mens-jackets",
        },
        # (3) 콜라보 가드 차단
        {
            "product_id": 6003,
            "product_name": "Arcteryx Beta Jacket Black",
            "sell_price": 890000,
            "sale_state": "ON",
            "_category": "mens-jackets",
        },
        # (4) 품절 (sale_state != ON)
        {
            "product_id": 6004,
            "product_name": "Arcteryx Alpha SV Jacket Red",
            "sell_price": 1290000,
            "sale_state": "OFF",
            "_category": "mens-jackets",
        },
        # (5) 모델번호 없음 → no_model_number
        {
            "product_id": 6005,
            "product_name": "Arcteryx Mystery Item",
            "sell_price": 10000,
            "sale_state": "ON",
            "_category": "mens-jackets",
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
    assert cand.source == "arcteryx"
    assert cand.kream_product_id == 701
    assert cand.model_no == "X000007301"
    assert cand.retail_price == 890000
    assert "6001" in cand.url

    # collect_queue 에 X000008888 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (c) 사전 field 로 옵션 호출 생략 ─────────────────────

async def test_prefilled_model_number_skips_options_call(bus, kream_db):
    fake_http = _FakeArcteryxHttp(category_items={}, options_by_id={})
    adapter = ArcteryxAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )

    products = [
        {
            "product_id": 7001,
            "product_name": "Arcteryx Beta Jacket Black",
            "sell_price": 890000,
            "sale_state": "ON",
            # 사전 주입
            "product_code": "X000007301",
        },
    ]

    matches, stats = await adapter.match_to_kream(products)
    assert stats.matched == 1
    assert len(matches) == 1
    # 옵션 API 호출이 한 번도 발생하지 않아야 함
    assert fake_http.options_calls == []


# ─── (d) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeArcteryxHttp(
        category_items={
            "mens-jackets": [
                _item(5001, "Beta Jacket Black"),
                _item(5010, "Gamma Sold Out", sale_state="OFF"),
            ],
        },
        options_by_id={
            5001: "X000007301",
            5010: "X000002222",
        },
    )
    adapter = ArcteryxAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"mens-jackets": "남성 자켓"},
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (e) 페이지네이션 루프 확인 ───────────────────────────

async def test_pagination_iterates_multiple_pages(bus, kream_db):
    fake_http = _FakeArcteryxHttp(
        category_items={
            "mens-jackets": [
                _item(5001, "A"),
                _item(5002, "B"),
                _item(5003, "C"),
            ],
        }
    )
    adapter = ArcteryxAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"mens-jackets": "남성 자켓"},
        max_pages=5,
        page_size=2,
    )
    _, products = await adapter.dump_catalog()
    assert len(products) == 3
    pages_called = [c["page_number"] for c in fake_http.list_calls]
    assert 1 in pages_called and 2 in pages_called


# ─── (f) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeArcteryxHttp(
        category_items={
            "mens-jackets": [
                _item(5001, "Beta Jacket Black"),
            ],
        },
        options_by_id={5001: "X000007301"},
    )
    adapter = ArcteryxAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"mens-jackets": "남성 자켓"},
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
            size=event.size or "M",
            retail_price=event.retail_price,
            kream_sell_price=1200000,
            net_profit=250000,
            roi=0.28,
            signal="STRONG_BUY",
            volume_7d=8,
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
        assert sent.kream_product_id == 701
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
