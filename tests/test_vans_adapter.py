"""반스 푸시 어댑터 테스트 (Phase 3 배치 2).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.vans_adapter import VansAdapter
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


class _FakeVansHttp:
    """`search_products` + `get_product_detail` mock."""

    def __init__(
        self,
        keyword_items: dict[str, list[dict]],
        pdp_sizes: dict[str, list[str]] | None = None,
    ):
        self._keyword_items = keyword_items
        self._pdp = pdp_sizes if pdp_sizes is not None else {}
        self.calls: list[dict] = []

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        self.calls.append({"keyword": keyword, "limit": limit})
        items = list(self._keyword_items.get(keyword, []))
        return items[:limit]

    async def get_product_detail(self, product_id: str):
        sizes = self._pdp.get(product_id, ["270"])
        if not sizes:
            return None
        return _FakeProduct([_FakeSize(s, True) for s in sizes])


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
            # 존재 — Vans Old Skool Black/White
            {
                "product_id": "501",
                "name": "Vans Old Skool Black White",
                "model_number": "VN000D3HY28",
                "brand": "Vans",
            },
            # 존재 — 콜라보 Vans x Supreme (가드 테스트용)
            {
                "product_id": "502",
                "name": "Vans x Supreme Old Skool Red",
                "model_number": "VN0A5FCBRED",
                "brand": "Vans",
            },
            # 존재 — Vans Authentic
            {
                "product_id": "503",
                "name": "Vans Authentic Classic White",
                "model_number": "VN000EE3WHT",
                "brand": "Vans",
            },
        ],
    )
    return str(path)


def _vans_item(model: str, name: str, price: int = 79000,
               sold_out: bool = False) -> dict:
    """반스 크롤러 search_products 정규화 형태와 동일."""
    return {
        "product_id": model,
        "name": name,
        "brand": "Vans",
        "model_number": model,
        "price": price,
        "original_price": price,
        "url": f"https://www.vans.co.kr/PRODUCT/{model}",
        "image_url": "",
        "is_sold_out": sold_out,
        "sizes": [],
    }


# ─── (a) dump_catalog publish + 반환 ──────────────────────

async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeVansHttp(
        keyword_items={
            "Old Skool": [
                _vans_item("VN000D3HY28", "Vans Old Skool Black/White"),
                _vans_item("VN0NEWXXXXX", "Vans Old Skool Checkerboard"),
            ],
        }
    )
    adapter = VansAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        keywords=("Old Skool",),
        limit_per_keyword=50,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "vans"
    assert event.product_count == 2
    assert len(products) == 2
    # _keyword 메타 주입 확인
    assert all(p.get("_keyword") == "Old Skool" for p in products)
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["keyword"] == "Old Skool"
    assert received and received[0].product_count == 2


# ─── (b) 다중 키워드 중복 제거 ──────────────────────────

async def test_dump_deduplicates_across_keywords(bus, kream_db):
    """같은 모델이 여러 키워드에서 잡혀도 단일 아이템으로 합쳐진다."""
    fake_http = _FakeVansHttp(
        keyword_items={
            "Old Skool": [_vans_item("VN000D3HY28", "Vans Old Skool")],
            "Authentic": [
                _vans_item("VN000D3HY28", "Vans Old Skool"),  # 중복
                _vans_item("VN000EE3WHT", "Vans Authentic White"),
            ],
        }
    )
    adapter = VansAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        keywords=("Old Skool", "Authentic"),
    )
    _, products = await adapter.dump_catalog()
    models = {p["model_number"] for p in products}
    assert models == {"VN000D3HY28", "VN000EE3WHT"}


# ─── (c) 매칭 3케이스: 존재 / 미등재 / 콜라보 fail + 품절/공란 ──

async def test_match_three_cases(bus, kream_db):
    fake_http = _FakeVansHttp(keyword_items={})
    adapter = VansAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        keywords=("Old Skool",),
    )

    products = [
        # (1) 정상 매칭 — Vans Old Skool
        _vans_item("VN000D3HY28", "Vans Old Skool Black/White"),
        # (2) 미등재 신상 → collect_queue (크림 DB 미존재)
        _vans_item("VN0NEW12345", "Vans Knu Skool Pink"),
        # (3) 콜라보 가드 차단 — 크림은 Vans x Supreme, 소싱 이름은 일반명
        _vans_item("VN0A5FCBRED", "Vans Old Skool Red"),
        # (4) 품절 제외
        _vans_item("VN000EE3WHT", "Vans Authentic White", sold_out=True),
        # (5) 모델번호 없음 → no_model_number
        {
            "product_id": "",
            "name": "이름만 있음",
            "brand": "Vans",
            "model_number": "",
            "price": 10000,
            "is_sold_out": False,
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
    assert cand.source == "vans"
    assert cand.kream_product_id == 501
    assert cand.model_no == "VN000D3HY28"
    assert cand.retail_price == 79000
    assert "VN000D3HY28" in cand.url

    # collect_queue 에 VN0NEW12345 만 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (d) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeVansHttp(
        keyword_items={
            "Old Skool": [
                _vans_item("VN000D3HY28", "Vans Old Skool"),
                _vans_item("VN0A5FCBRED", "Vans Old Skool Red", sold_out=True),
            ],
        }
    )
    adapter = VansAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        keywords=("Old Skool",),
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (e) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeVansHttp(
        keyword_items={
            "Old Skool": [
                _vans_item("VN000D3HY28", "Vans Old Skool Black/White", price=79000),
            ],
        }
    )
    adapter = VansAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        keywords=("Old Skool",),
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
            kream_sell_price=120000,
            net_profit=25000,
            roi=0.31,
            signal="BUY",
            volume_7d=6,
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
        assert sent.kream_product_id == 501
        assert sent.signal == "BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
