"""노스페이스 푸시 어댑터 테스트 (Phase 3 배치 6).

실호출 금지: HTTP 레이어·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.thenorthface_adapter import TheNorthFaceAdapter
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
from src.crawlers.thenorthface import (
    _normalize_size_label,
    _parse_pdp_sizes,
    parse_listing_html,
)

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
    def __init__(self, sizes: list[_FakeSize]):
        self.sizes = sizes


class _FakeTnfHttp:
    """``fetch_tiles_category(category, page)`` 제공.

    카테고리별 페이지 리스트를 미리 저장해두고 호출에 따라 슬라이스 반환.
    """

    def __init__(self, pages_by_category: dict[str, list[list[dict]]]):
        self._pages = pages_by_category
        self._pdp: dict[str, list[str]] = {}
        self.calls: list[tuple[str, int]] = []

    async def fetch_tiles_category(self, category: str, page: int) -> list[dict]:
        self.calls.append((category, page))
        pages = self._pages.get(category, [])
        if page < 1 or page > len(pages):
            return []
        return list(pages[page - 1])

    async def get_product_detail(self, product_id: str):
        sizes = self._pdp.get(product_id, ["L"])
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
            # (1) 일반 — 눕시 자켓
            {
                "product_id": "901",
                "name": "The North Face 1996 Eco Nuptse Jacket Black",
                "model_number": "NJ1DR82J",
                "brand": "The North Face",
            },
            # (2) 콜라보 — Supreme x TNF 로 크림 등록
            {
                "product_id": "902",
                "name": "Supreme x The North Face Mountain Jacket",
                "model_number": "NP0ZK01S",
                "brand": "The North Face",
            },
            # (3) 일반 — 하이스트 퓸 6
            {
                "product_id": "903",
                "name": "The North Face HST Fume 6 Gray",
                "model_number": "NA5AS41B",
                "brand": "The North Face",
            },
        ],
    )
    return str(path)


def _tile(
    model: str,
    name: str,
    price: int = 299000,
    is_sold_out: bool = False,
    product_id: str = "",
) -> dict:
    return {
        "model_number": model,
        "product_id": product_id or f"pk_{model}",
        "name": name,
        "brand": "The North Face",
        "price": price,
        "original_price": price,
        "url": f"https://www.thenorthfacekorea.co.kr/product/{model}",
        "image_url": "",
        "is_sold_out": is_sold_out,
        "sizes": [],
    }


# ─── (a) parse_listing_html — 순수 파서 단위 ──────────────


def test_parse_listing_html_extracts_tiles():
    html = """
<html><body>
<ul>
  <li>
    <p class="name">
      <a class="text-link" data-name="HST Fume 6" data-id="NA5AS41B" href="/product/NA5AS41B">HST Fume 6</a>
    </p>
    <a class="swatch swatch-tile" data-product-id="4000059442" data-product-sold-out="false"
       data-product-image-urls="/cmsstatic/product/NA5AS41B_primary.jpg@/cmsstatic/product/NA5AS41B_00.jpg"
       data-product-url="/product/NA5AS41B" href="/product/NA5AS41B">
    </a>
    <div class="price-block">
      <span class="price">269,000 원</span>
    </div>
  </li>
  <li>
    <p class="name">
      <a class="text-link" data-name="1996 Eco Nuptse Jacket" data-id="NJ1DR82J" href="/product/NJ1DR82J">1996 Eco Nuptse Jacket</a>
    </p>
    <a class="swatch swatch-tile" data-product-id="4000059999" data-product-sold-out="true"
       data-product-url="/product/NJ1DR82J" href="/product/NJ1DR82J">
    </a>
    <div>
      <span class="price">459,000 원</span>
    </div>
  </li>
</ul>
</body></html>
"""
    tiles = parse_listing_html(html)
    assert len(tiles) == 2
    by_model = {t.model_number: t for t in tiles}

    assert "NA5AS41B" in by_model
    tile = by_model["NA5AS41B"]
    assert tile.name == "HST Fume 6"
    assert tile.price == 269000
    assert tile.is_sold_out is False
    assert tile.url.endswith("/product/NA5AS41B")
    assert "cmsstatic" in tile.image_url

    assert "NJ1DR82J" in by_model
    nuptse = by_model["NJ1DR82J"]
    assert nuptse.is_sold_out is True
    assert nuptse.price == 459000


def test_parse_listing_html_empty_returns_empty():
    assert parse_listing_html("") == []
    assert parse_listing_html("<html>no tiles</html>") == []


# ─── (b) dump_catalog publish ─────────────────────────────


async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeTnfHttp(
        pages_by_category={
            "men": [
                [
                    _tile("NA5AS41B", "HST Fume 6"),
                    _tile("NJ1DR82J", "1996 Eco Nuptse Jacket Black", price=459000),
                ],
            ],
        }
    )
    adapter = TheNorthFaceAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men": "남성"},
        max_pages=3,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "thenorthface"
    assert event.product_count == 2
    assert len(products) == 2
    assert all(p.get("_category") == "men" for p in products)
    # page 1 호출 + 새 상품 0인 page 2 는 끊김 (page 2 빈 결과)
    assert fake_http.calls[0] == ("men", 1)
    assert received and received[0].product_count == 2


# ─── (c) 매칭 5케이스 ────────────────────────────────────


async def test_match_five_cases(bus, kream_db):
    fake_http = _FakeTnfHttp(pages_by_category={})
    adapter = TheNorthFaceAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )

    products = [
        # (1) 정상 매칭 — NA5AS41B 존재
        _tile("NA5AS41B", "HST Fume 6 Gray"),
        # (2) 미등재 → collect_queue
        _tile("NM2DP51A", "Whitelabel Duffel Pack"),
        # (3) 콜라보 가드 차단 — 크림=Supreme 콜라보, 소싱=일반명
        _tile("NP0ZK01S", "Mountain Jacket"),
        # (4) 품절 → soldout_dropped
        _tile(
            "NJ1DR82J",
            "1996 Eco Nuptse Jacket",
            price=459000,
            is_sold_out=True,
        ),
        # (5) 잘못된 모델번호 포맷 → invalid_model
        _tile("bad-?!", "Bad Model"),
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
    assert stats.invalid_model == 1
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert isinstance(cand, CandidateMatched)
    assert cand.source == "thenorthface"
    assert cand.kream_product_id == 903
    assert cand.model_no == "NA5AS41B"
    assert cand.retail_price == 299000
    assert "NA5AS41B" in cand.url

    assert _count_queue(kream_db) == 1


# ─── (d) 카테고리 페이지네이션 ───────────────────────────


async def test_pagination_iterates_pages_until_empty(bus, kream_db):
    fake_http = _FakeTnfHttp(
        pages_by_category={
            "men": [
                [_tile("NA5AS41B", "HST Fume 6")],
                [_tile("NJ1DR82J", "Nuptse")],
                [],  # page 3 — 중단
            ],
        }
    )
    adapter = TheNorthFaceAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men": "남성"},
        max_pages=10,
    )

    _, products = await adapter.dump_catalog()
    assert len(products) == 2
    pages = [c[1] for c in fake_http.calls if c[0] == "men"]
    assert 1 in pages and 2 in pages and 3 in pages
    # 4 페이지 이상은 호출하지 않음
    assert max(pages) == 3


async def test_pagination_stops_on_no_new_rows(bus, kream_db):
    """같은 상품만 반복되면 중단."""
    same = [_tile("NA5AS41B", "HST Fume 6")]
    fake_http = _FakeTnfHttp(
        pages_by_category={"men": [same, list(same), list(same)]}
    )
    adapter = TheNorthFaceAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men": "남성"},
        max_pages=10,
    )
    _, products = await adapter.dump_catalog()
    assert len(products) == 1
    # 2 페이지는 호출되지만 3 페이지는 new_rows=0 로 중단
    pages = [c[1] for c in fake_http.calls]
    assert 1 in pages and 2 in pages
    assert 3 not in pages


# ─── (e) run_once 통계 ────────────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeTnfHttp(
        pages_by_category={
            "men": [
                [
                    _tile("NA5AS41B", "HST Fume 6"),
                    _tile("NJ1DR82J", "Nuptse", is_sold_out=True),
                ],
            ],
            "women": [
                [_tile("NWOMEN01A", "Womens Item")],
            ],
        }
    )
    adapter = TheNorthFaceAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men": "남성", "women": "여성"},
        max_pages=3,
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 3
    assert stats["matched"] == 1  # NA5AS41B
    assert stats["soldout_dropped"] == 1  # Nuptse
    assert stats["collected_to_queue"] == 1  # NWOMEN01A 미등재


# ─── (f) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────


async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    fake_http = _FakeTnfHttp(
        pages_by_category={
            "men": [[_tile("NA5AS41B", "HST Fume 6 Gray")]],
        }
    )
    adapter = TheNorthFaceAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories={"men": "남성"},
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
            kream_sell_price=520000,
            net_profit=180000,
            roi=0.40,
            signal="STRONG_BUY",
            volume_7d=12,
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
        assert sent.kream_product_id == 903
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()


# ─── 사이즈 정규화 (PDP `090S`/`095M` → 크림 `S`/`M`) ─────────


def test_normalize_size_label_strips_apparel_height_prefix():
    """의류 PDP 사이즈는 신장 prefix(3자리) + 사이즈 라벨로 옴."""
    assert _normalize_size_label("090S") == "S"
    assert _normalize_size_label("095M") == "M"
    assert _normalize_size_label("100L") == "L"
    assert _normalize_size_label("085XS") == "XS"
    assert _normalize_size_label("110XXL") == "XXL"


def test_normalize_size_label_preserves_shoe_sizes():
    """신발은 prefix 없는 순수 3자리(`230`/`270`) — 통째로 보존."""
    assert _normalize_size_label("230") == "230"
    assert _normalize_size_label("270") == "270"
    assert _normalize_size_label("285") == "285"


def test_normalize_size_label_passthrough_for_plain_labels():
    """이미 깨끗한 라벨은 그대로 (공백 trim 만)."""
    assert _normalize_size_label("M") == "M"
    assert _normalize_size_label("XXL") == "XXL"
    assert _normalize_size_label("FREE") == "FREE"
    assert _normalize_size_label("  L  ") == "L"


def test_parse_pdp_sizes_normalizes_apparel_labels():
    """PDP HTML 통합 — apparel 사이즈가 정규화돼서 크림 교집합과 매칭 가능."""
    html = (
        '<div data-product-options="['
        '{&quot;type&quot;:&quot;SIZE&quot;,'
        '&quot;values&quot;:{&quot;1&quot;:&quot;090S&quot;,'
        '&quot;2&quot;:&quot;095M&quot;,&quot;3&quot;:&quot;100L&quot;}}'
        ']"></div>'
        '<div data-sku-data="['
        '{&quot;isSoldOut&quot;:false,&quot;quantity&quot;:5,&quot;selectedOptions&quot;:[1]},'
        '{&quot;isSoldOut&quot;:false,&quot;quantity&quot;:3,&quot;selectedOptions&quot;:[2]},'
        '{&quot;isSoldOut&quot;:true,&quot;quantity&quot;:0,&quot;selectedOptions&quot;:[3]}'
        ']"></div>'
    )
    sizes = _parse_pdp_sizes(html)
    label_to_stock = {s.size: s.in_stock for s in sizes}
    assert label_to_stock == {"S": True, "M": True, "L": False}
