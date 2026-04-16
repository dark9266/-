"""29CM 푸시 어댑터 테스트 (Phase 3 배치 1).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
무신사 어댑터 테스트와 구조 동일 — 비교 용이성 유지.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.twentynine_cm_adapter import TwentynineCmAdapter
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
    def __init__(self, sizes: list[_FakeSize]):
        self.sizes = sizes


class _Fake29cmHttp:
    """search_products + get_product_detail mock.

    `pdp_sizes` 맵: product_id → 재고 사이즈 리스트. 없으면 () 반환 (drop).
    """

    def __init__(
        self,
        listings: dict[str, list[dict]],
        pdp_sizes: dict[str, list[str]] | None = None,
    ):
        self._listings = listings
        self._pdp = pdp_sizes if pdp_sizes is not None else {}
        self.calls: list[tuple[str, int]] = []
        # 디폴트: 모든 product_id 에 ("270",) 반환 (정상 케이스 회귀 호환)
        self._default_sizes = ("270",)

    async def search_products(
        self, keyword: str, limit: int = 30
    ) -> list[dict]:
        self.calls.append((keyword, limit))
        return list(self._listings.get(keyword, []))

    async def get_product_detail(self, product_id: str):
        sizes = self._pdp.get(product_id)
        if sizes is None:
            sizes = list(self._default_sizes)
        if not sizes:
            return None
        return _FakeProduct([_FakeSize(s, True) for s in sizes])


def _product(
    pid: str,
    name: str,
    *,
    brand: str = "Nike",
    price: int = 139000,
    sold: bool = False,
    model_number: str = "",
) -> dict:
    """29CM 크롤러 search_products 반환 스키마와 동일한 dict 생성."""
    return {
        "product_id": pid,
        "name": name,
        "brand": brand,
        "model_number": model_number,
        "price": price,
        "original_price": price,
        "url": f"https://www.29cm.co.kr/products/{pid}",
        "image_url": "",
        "is_sold_out": sold,
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
            # 존재 — 일반 Nike
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


# ─── (a) dump_catalog publish + 반환 ──────────────────────

async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _Fake29cmHttp(
        listings={
            "Nike": [
                _product(
                    "2001",
                    "에어포스 1 '07 - 화이트 / CW2288-111",
                    model_number="CW2288-111",
                ),
                _product(
                    "2002",
                    "덩크 로우 레트로 - 블랙 / DD1391-100",
                    model_number="DD1391-100",
                ),
            ],
        }
    )
    adapter = TwentynineCmAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords=("Nike",),
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

    assert event.source == "29cm"
    assert event.product_count == 2
    assert len(products) == 2
    assert fake_http.calls == [("Nike", 50)]
    assert received and received[0].product_count == 2


async def test_dump_catalog_dedups_across_brand_keywords(bus, kream_db):
    """동일 product_id 가 여러 브랜드 키워드에 교차 노출되면 1회만 수집."""
    shared = _product(
        "3001",
        "에어조던 1 로우 / 553558-161",
        brand="Jordan",
        model_number="553558-161",
    )
    fake_http = _Fake29cmHttp(
        listings={
            "Nike": [shared],
            "Jordan": [shared],
        }
    )
    adapter = TwentynineCmAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords=("Nike", "Jordan"),
    )
    _, products = await adapter.dump_catalog()
    assert len(products) == 1
    assert len(fake_http.calls) == 2


# ─── (b) match_to_kream — 매칭/큐/가드 ────────────────────

async def test_match_to_kream_classifies_items(bus, kream_db):
    fake_http = _Fake29cmHttp(listings={})
    adapter = TwentynineCmAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords=("Nike",),
    )

    products = [
        # (1) 정상 매칭 — Nike AF1 일반
        _product(
            "2001",
            "에어포스 1 '07 - 화이트 / CW2288-111",
            model_number="CW2288-111",
        ),
        # (2) 미등재 신상 → collect_queue
        _product(
            "2003",
            "덩크 로우 - 그린 / ZZ9999-100",
            model_number="ZZ9999-100",
        ),
        # (3) 콜라보 가드 차단 — 크림은 Travis Scott, 소싱은 일반명
        _product(
            "2004",
            "에어포스 1 화이트 / AQ4211-100",
            model_number="AQ4211-100",
        ),
        # (4) 품절 제외
        _product(
            "2006",
            "덩크 로우 - 핑크 / DD7777-100",
            model_number="DD7777-100",
            sold=True,
        ),
        # (5) 모델번호 없음 → no_model_number
        _product(
            "2007",
            "노멀 티셔츠 - 블랙",
            brand="MuztdStandard",
        ),
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
    assert cand.source == "29cm"
    assert cand.kream_product_id == 101
    assert cand.model_no == "CW2288-111"
    assert cand.retail_price == 139000
    assert "2001" in cand.url

    assert _count_queue(kream_db) == 1


async def test_match_to_kream_extracts_model_from_name_when_missing(bus, kream_db):
    """item 에 model_number 필드가 비어도 상품명에서 추출하면 매칭."""
    fake_http = _Fake29cmHttp(listings={})
    adapter = TwentynineCmAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )
    products = [
        _product(
            "2001",
            "에어포스 1 '07 - 화이트 / CW2288-111",
            model_number="",  # 크롤러 추출 실패 가정
        ),
    ]
    matches, stats = await adapter.match_to_kream(products)
    assert stats.matched == 1
    assert len(matches) == 1
    assert matches[0].model_no == "CW2288-111"


# ─── (c) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db):
    fake_http = _Fake29cmHttp(
        listings={
            "Nike": [
                _product(
                    "2001",
                    "에어포스 1 '07 - 화이트 / CW2288-111",
                    model_number="CW2288-111",
                ),
                _product(
                    "2006",
                    "덩크 로우 / DD7777-100",
                    model_number="DD7777-100",
                    sold=True,
                ),
            ]
        }
    )
    adapter = TwentynineCmAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords=("Nike",),
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (d) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _Fake29cmHttp(
        listings={
            "Nike": [
                _product(
                    "2001",
                    "에어포스 1 '07 - 화이트 / CW2288-111",
                    model_number="CW2288-111",
                ),
            ]
        }
    )
    adapter = TwentynineCmAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brand_keywords=("Nike",),
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
        # 어댑터가 직접 publish 하므로 catalog_handler 는 noop.
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
