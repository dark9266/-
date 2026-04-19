"""아디다스 푸시 어댑터 테스트 (Phase 3 배치 3).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.adidas_adapter import AdidasAdapter
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


class _FakeAdidasHttp:
    """`fetch_taxonomy_page` + `get_product_detail` mock."""

    def __init__(
        self,
        category_items: dict[str, list[dict]],
        pdp_sizes: dict[str, list[str]] | None = None,
    ):
        self._category_items = category_items
        self._pdp = pdp_sizes if pdp_sizes is not None else {}
        self.calls: list[dict] = []

    async def fetch_taxonomy_page(
        self,
        *,
        category: str,
        page_size: int = 48,
        page_number: int = 1,
    ) -> dict:
        self.calls.append(
            {
                "category": category,
                "page_size": page_size,
                "page_number": page_number,
            }
        )
        items_all = list(self._category_items.get(category, []))
        start = (page_number - 1) * page_size
        end = start + page_size
        return {"items": items_all[start:end], "totalCount": len(items_all)}

    async def get_product_detail(self, product_id: str):
        sizes = self._pdp.get(product_id, ["270"])
        if not sizes:
            return None
        return _FakeProduct([_FakeSize(s, True) for s in sizes])


class _FakeAdidasHttpNoPdp:
    """라이브 회귀: search API 가 SKU 키워드 검색을 지원하지 않아
    `get_product_detail` 이 항상 None 을 반환하는 상태 모사.

    덤프 데이터의 `sizes` (availableSizes) 가 이미 실재고이므로
    어댑터는 PDP 재호출 없이 매칭을 완주해야 한다.
    """

    def __init__(self, category_items: dict[str, list[dict]]):
        self._category_items = category_items
        self.pdp_calls: list[str] = []

    async def fetch_taxonomy_page(
        self,
        *,
        category: str,
        page_size: int = 48,
        page_number: int = 1,
    ) -> dict:
        items_all = list(self._category_items.get(category, []))
        start = (page_number - 1) * page_size
        end = start + page_size
        return {"items": items_all[start:end], "totalCount": len(items_all)}

    async def get_product_detail(self, product_id: str):
        # 라이브 동작: SKU 키워드 검색 실패 → 항상 None
        self.pdp_calls.append(product_id)
        return None


def _adidas_item(
    product_id: str,
    name: str,
    price: int = 159000,
    sold_out: bool = False,
) -> dict:
    """adidas 크롤러 `_parse_items` 결과와 동일한 정규화 dict 생성."""
    return {
        "product_id": product_id,
        "name": name,
        "brand": "adidas",
        "model_number": product_id,
        "price": price,
        "original_price": price,
        "url": f"https://www.adidas.co.kr/{product_id}.html",
        "link": f"/{product_id}.html",
        "image_url": "",
        "is_sold_out": sold_out,
        "sizes": ["240", "250", "260", "270"],
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
            # 존재 — adidas Samba OG
            {
                "product_id": "301",
                "name": "adidas Samba OG White Black",
                "model_number": "B75806",
                "brand": "adidas",
            },
            # 존재 — adidas Gazelle (Bold Pink, 콜라보 아님)
            {
                "product_id": "302",
                "name": "adidas Gazelle Bold Pink Glow",
                "model_number": "IE5900",
                "brand": "adidas",
            },
            # 존재 — adidas x sacai 콜라보 (가드 테스트)
            {
                "product_id": "303",
                "name": "adidas Samba sacai White",
                "model_number": "IG8181",
                "brand": "adidas",
            },
        ],
    )
    return str(path)


# ─── (a) dump_catalog publish + 반환 ──────────────────────

async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeAdidasHttp(
        category_items={
            "men_shoes": [
                _adidas_item("B75806", "아디다스 삼바 OG 화이트"),
                _adidas_item("IE5900", "아디다스 가젤 볼드 핑크"),
            ],
        }
    )
    adapter = AdidasAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men_shoes": "남성 신발"},
        max_pages=1,
        page_size=48,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "adidas"
    assert event.product_count == 2
    assert len(products) == 2
    # 카테고리 메타 주입 확인
    assert all(p.get("_category_slug") == "men_shoes" for p in products)
    assert all(p.get("_category_name") == "남성 신발" for p in products)
    # 한 번 호출됨 (전량이 1페이지 수용)
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["category"] == "men_shoes"
    assert received and received[0].product_count == 2


# ─── (b) product_id 직접 매칭 ─────────────────────────────

async def test_match_uses_product_id_directly(bus, kream_db):
    fake_http = _FakeAdidasHttp(category_items={})
    adapter = AdidasAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )

    # 한글 상품명 — 상품명 파싱에 의존하지 않음을 증명
    products = [
        _adidas_item("IE5900", "아디다스 가젤 볼드 핑크 글로우", price=159000),
    ]

    matches, stats = await adapter.match_to_kream(products)
    assert stats.matched == 1
    assert stats.no_model_number == 0
    assert len(matches) == 1
    assert matches[0].kream_product_id == 302
    assert matches[0].model_no == "IE5900"
    assert matches[0].retail_price == 159000
    assert "IE5900" in matches[0].url


# ─── (c) 3케이스+: 존재/미등재/콜라보 fail/품절/빈 모델 ────

async def test_match_three_cases(bus, kream_db):
    fake_http = _FakeAdidasHttp(category_items={})
    adapter = AdidasAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )

    products = [
        # (1) 정상 매칭 — adidas Samba OG
        _adidas_item("B75806", "아디다스 삼바 OG 화이트 블랙", price=129000),
        # (2) 미등재 신상 → collect_queue
        _adidas_item("JJ9999", "아디다스 신상 러닝화", price=179000),
        # (3) 콜라보 가드 차단 — 크림은 sacai 콜라보, 소싱은 일반명
        _adidas_item("IG8181", "아디다스 삼바 화이트 (일반)", price=149000),
        # (4) 품절 제외
        _adidas_item("IE5900", "아디다스 가젤 볼드 핑크", sold_out=True),
        # (5) 모델번호 없음 → no_model_number
        {
            "product_id": "",
            "name": "모델번호 없는 이상한 아이템",
            "brand": "adidas",
            "price": 10000,
            "is_sold_out": False,
            "sizes": [],
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
    assert cand.source == "adidas"
    assert cand.kream_product_id == 301
    assert cand.model_no == "B75806"
    assert cand.retail_price == 129000
    assert "B75806" in cand.url

    # collect_queue 에 JJ9999 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (d) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeAdidasHttp(
        category_items={
            "men_shoes": [
                _adidas_item("B75806", "아디다스 삼바 OG"),
                _adidas_item("DD7777", "아디다스 울트라부스트", sold_out=True),
            ],
        }
    )
    adapter = AdidasAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men_shoes": "남성 신발"},
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (e) 페이지네이션 루프 확인 ───────────────────────────

async def test_pagination_iterates_multiple_pages(bus, kream_db):
    # 3건 아이템, page_size=2 → 2페이지로 덤프
    fake_http = _FakeAdidasHttp(
        category_items={
            "men_shoes": [
                _adidas_item("B75806", "A"),
                _adidas_item("XX0001", "B"),
                _adidas_item("XX0002", "C"),
            ],
        }
    )
    adapter = AdidasAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men_shoes": "남성 신발"},
        max_pages=5,
        page_size=2,
    )
    _, products = await adapter.dump_catalog()
    assert len(products) == 3
    pages_called = [c["page_number"] for c in fake_http.calls]
    assert 1 in pages_called and 2 in pages_called


# ─── (e-2) SKU 키워드 검색 불가 회귀 — PDP 없이도 매칭 성공 ───

async def test_match_without_pdp_uses_dump_sizes(bus, kream_db):
    """아디다스 search API 는 SKU 로 상품 조회 불가 (get_product_detail → None).

    덤프 데이터의 availableSizes 를 그대로 실재고로 사용해 매칭 완주해야 한다.
    """
    fake_http = _FakeAdidasHttpNoPdp(category_items={})
    adapter = AdidasAdapter(bus=bus, db_path=kream_db, http_client=fake_http)

    products = [
        _adidas_item("B75806", "아디다스 삼바 OG 화이트 블랙", price=129000),
    ]
    # 덤프 사이즈 == 실재고
    assert products[0]["sizes"] == ["240", "250", "260", "270"]

    matches, stats = await adapter.match_to_kream(products)

    assert stats.matched == 1, "PDP 불가 상태에서도 덤프 사이즈로 매칭돼야 함"
    assert len(matches) == 1
    # 어댑터는 SKU 단위 PDP 재호출을 **하면 안 됨** (search SKU 쿼리 불가)
    assert fake_http.pdp_calls == []
    # 덤프 사이즈 그대로 available_sizes 에 주입
    assert matches[0].available_sizes == ("240", "250", "260", "270")


async def test_match_empty_sizes_drops(bus, kream_db):
    """덤프 sizes 가 비어 있으면 실재고 없음 → drop."""
    fake_http = _FakeAdidasHttpNoPdp(category_items={})
    adapter = AdidasAdapter(bus=bus, db_path=kream_db, http_client=fake_http)

    empty = _adidas_item("B75806", "아디다스 삼바 OG")
    empty["sizes"] = []  # 전 사이즈 품절

    _, stats = await adapter.match_to_kream([empty])
    assert stats.matched == 0
    assert stats.soldout_dropped == 1


# ─── (f) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeAdidasHttp(
        category_items={
            "men_shoes": [
                _adidas_item("B75806", "아디다스 삼바 OG 화이트"),
            ],
        }
    )
    adapter = AdidasAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men_shoes": "남성 신발"},
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
        assert sent.kream_product_id == 301
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
