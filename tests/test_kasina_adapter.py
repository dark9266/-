"""카시나 푸시 어댑터 테스트 (Phase 3 배치 1).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.kasina_adapter import (
    BRAND_ADIDAS,
    BRAND_NEWBALANCE,
    BRAND_NIKE,
    KasinaAdapter,
)
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


class _FakeKasinaHttp:
    """`_search_raw(brand_no, page_size, page_number)` + `get_product_detail` mock."""

    def __init__(
        self,
        brand_items: dict[int, list[dict]],
        pdp_sizes: dict[str, list[str]] | None = None,
    ):
        self._brand_items = brand_items
        self._pdp = pdp_sizes if pdp_sizes is not None else {}
        self.calls: list[dict] = []

    async def get_product_detail(self, product_id: str):
        sizes = self._pdp.get(product_id, ["270"])
        if not sizes:
            return None
        return _FakeProduct([_FakeSize(s, True) for s in sizes])

    async def _search_raw(
        self,
        *,
        management_cd: str | None = None,
        brand_no: int | None = None,
        page_size: int = 20,
        page_number: int = 1,
    ) -> dict:
        self.calls.append(
            {
                "brand_no": brand_no,
                "page_size": page_size,
                "page_number": page_number,
                "management_cd": management_cd,
            }
        )
        items_all = list(self._brand_items.get(brand_no or -1, []))
        start = (page_number - 1) * page_size
        end = start + page_size
        return {"items": items_all[start:end], "totalCount": len(items_all)}


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
            # 존재 — 일반 Nike AF1
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
            # 존재 — adidas Samba
            {
                "product_id": "303",
                "name": "adidas Samba OG White Black",
                "model_number": "B75806",
                "brand": "adidas",
            },
        ],
    )
    return str(path)


def _nike_item(product_no: int, name: str, mgmt: str, price: int = 139000,
               sold_out: bool = False) -> dict:
    return {
        "productNo": product_no,
        "productName": name,
        "productNameEn": name,
        "productManagementCd": mgmt,
        "salePrice": price,
        "brandNameKo": "나이키",
        "brandNameEn": "Nike",
        "isSoldOut": sold_out,
    }


# ─── (a) dump_catalog publish + 반환 ──────────────────────

async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeKasinaHttp(
        brand_items={
            BRAND_NIKE: [
                _nike_item(5001, "나이키 에어포스 1 로우 화이트", "CW2288-111"),
                _nike_item(5002, "나이키 덩크 로우", "ZZ9999-100"),
            ],
        }
    )
    adapter = KasinaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brands={BRAND_NIKE: "Nike"},
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

    assert event.source == "kasina"
    assert event.product_count == 2
    assert len(products) == 2
    # brand_no 메타 주입 확인
    assert all(p.get("_brand_no") == BRAND_NIKE for p in products)
    # 한 번 호출됨 (전량이 1페이지에 들어감)
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["brand_no"] == BRAND_NIKE
    assert received and received[0].product_count == 2


# ─── (b) productManagementCd 직접 매칭 확인 ──────────────

async def test_match_uses_product_management_cd_directly(bus, kream_db):
    fake_http = _FakeKasinaHttp(brand_items={})
    adapter = KasinaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brands={BRAND_NIKE: "Nike"},
    )

    # 상품명은 한글/영문 혼재 — 모델번호 추출을 상품명 파싱에 의존하지 않음을 증명
    products = [
        {
            "productNo": 9001,
            "productName": "나이키 에어포스 1 로우 화이트 (상품명에 코드 없음)",
            "productNameEn": "Nike AF1 Low White",
            "productManagementCd": "CW2288-111",
            "salePrice": 139000,
            "brandNameKo": "나이키",
            "isSoldOut": False,
            "_brand_no": BRAND_NIKE,
            "_brand_name": "Nike",
        },
    ]

    matches, stats = await adapter.match_to_kream(products)
    assert stats.matched == 1
    assert stats.no_model_number == 0
    assert len(matches) == 1
    assert matches[0].kream_product_id == 101
    assert matches[0].model_no == "CW2288-111"
    assert matches[0].retail_price == 139000
    assert "9001" in matches[0].url


# ─── (c) 3케이스: 존재/미등재/콜라보 fail ─────────────────

async def test_match_three_cases(bus, kream_db):
    fake_http = _FakeKasinaHttp(brand_items={})
    adapter = KasinaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brands={BRAND_NIKE: "Nike"},
    )

    products = [
        # (1) 정상 매칭 — Nike AF1
        {
            "productNo": 5001,
            "productName": "나이키 에어포스 1 로우 화이트",
            "productManagementCd": "CW2288-111",
            "salePrice": 139000,
            "brandNameKo": "나이키",
            "isSoldOut": False,
            "_brand_no": BRAND_NIKE,
            "_brand_name": "Nike",
        },
        # (2) 미등재 신상 → collect_queue (크림 DB 에 NBPDFF003Z 없음)
        {
            "productNo": 5002,
            "productName": "뉴발란스 2002R",
            "productManagementCd": "NBPDFF003Z",
            "salePrice": 159000,
            "brandNameKo": "뉴발란스",
            "isSoldOut": False,
            "_brand_no": BRAND_NEWBALANCE,
            "_brand_name": "New Balance",
        },
        # (3) 콜라보 가드 차단 — 크림은 Travis Scott 이지만 소싱 이름은 일반명
        {
            "productNo": 5003,
            "productName": "나이키 에어포스 1 로우 화이트",
            "productManagementCd": "AQ4211-100",
            "salePrice": 149000,
            "brandNameKo": "나이키",
            "isSoldOut": False,
            "_brand_no": BRAND_NIKE,
            "_brand_name": "Nike",
        },
        # (4) 품절 제외
        {
            "productNo": 5004,
            "productName": "아디다스 삼바 OG",
            "productManagementCd": "B75806",
            "salePrice": 129000,
            "brandNameKo": "아디다스",
            "isSoldOut": True,
            "_brand_no": BRAND_ADIDAS,
            "_brand_name": "adidas",
        },
        # (5) 모델번호 없음 → no_model_number
        {
            "productNo": 5005,
            "productName": "이름만 있고 mgmt 없음",
            "productManagementCd": "",
            "salePrice": 10000,
            "brandNameKo": "나이키",
            "isSoldOut": False,
            "_brand_no": BRAND_NIKE,
            "_brand_name": "Nike",
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
    assert cand.source == "kasina"
    assert cand.kream_product_id == 101
    assert cand.model_no == "CW2288-111"
    assert cand.retail_price == 139000
    assert "5001" in cand.url

    # collect_queue 에 NBPDFF003Z 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (d) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeKasinaHttp(
        brand_items={
            BRAND_NIKE: [
                _nike_item(5001, "나이키 에어포스 1", "CW2288-111"),
                _nike_item(5010, "나이키 덩크", "DD7777-100", sold_out=True),
            ],
        }
    )
    adapter = KasinaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brands={BRAND_NIKE: "Nike"},
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (e) 페이지네이션 루프 확인 ───────────────────────────

async def test_pagination_iterates_multiple_pages(bus, kream_db):
    # 3건 아이템, page_size=2 → 2페이지로 덤프
    fake_http = _FakeKasinaHttp(
        brand_items={
            BRAND_NIKE: [
                _nike_item(5001, "A", "CW2288-111"),
                _nike_item(5002, "B", "ZZ0001-100"),
                _nike_item(5003, "C", "ZZ0002-100"),
            ],
        }
    )
    adapter = KasinaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brands={BRAND_NIKE: "Nike"},
        max_pages=5,
        page_size=2,
    )
    _, products = await adapter.dump_catalog()
    assert len(products) == 3
    # page 1, page 2 호출 (page 3 은 empty 로 중단 또는 totalCount 도달)
    pages_called = [c["page_number"] for c in fake_http.calls]
    assert 1 in pages_called and 2 in pages_called


# ─── (f) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeKasinaHttp(
        brand_items={
            BRAND_NIKE: [
                _nike_item(5001, "나이키 에어포스 1 로우 화이트", "CW2288-111"),
            ],
        }
    )
    adapter = KasinaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        brands={BRAND_NIKE: "Nike"},
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
