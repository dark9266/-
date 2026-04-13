"""더한섬닷컴 푸시 어댑터 테스트 (Phase 3 배치 5).

실호출 금지: 더한섬 크롤러·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.adapters.thehandsome_adapter import (
    DEFAULT_BRAND_WHITELIST,
    ThehandsomeAdapter,
)
from src.core.event_bus import (
    CandidateMatched,
    CatalogDumped,
    EventBus,
)

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


# ─── mock thehandsome 크롤러 ──────────────────────────────

class _FakeThehandsomeCrawler:
    """`list_brands()` + `dump_brand_goods(brand_no, ...)` 만 mock."""

    def __init__(
        self,
        brands: list[dict],
        brand_items: dict[str, list[dict]],
    ):
        self._brands = brands
        self._brand_items = brand_items
        self.brand_calls: int = 0
        self.dump_calls: list[dict] = []

    async def list_brands(self) -> list[dict]:
        self.brand_calls += 1
        return [dict(b) for b in self._brands]

    async def dump_brand_goods(
        self,
        brand_no: str,
        *,
        page_size: int = 40,
        max_pages: int = 5,
    ) -> list[dict]:
        self.dump_calls.append({
            "brand_no": brand_no,
            "page_size": page_size,
            "max_pages": max_pages,
        })
        return [dict(it) for it in self._brand_items.get(brand_no, [])]


def _listing_item(
    goods_no: str,
    name: str,
    brand: str,
    price: int = 890000,
    sold_out: bool = False,
) -> dict:
    return {
        "product_id": goods_no,
        "name": name,
        "brand": brand,
        "brand_no": "",
        "lowr_brand_nm": brand,
        "model_number": goods_no,
        "price": price,
        "original_price": price,
        "dc_rate": 0.0,
        "url": f"https://www.thehandsome.com/ko/PM/productDetail/{goods_no}",
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
            # 존재 — BALLY 스니커 가정. 한섬 ERP 코드와 일치한다고 가정.
            {
                "product_id": "5001",
                "name": "Bally Maxim Low Leather Sneaker",
                "model_number": "BL2G3ABG001BAL",
                "brand": "Bally",
            },
            # 존재 — LANVIN Curb 가정.
            {
                "product_id": "5002",
                "name": "Lanvin Curb Sneaker White",
                "model_number": "LN2G3ABG001LAN",
                "brand": "Lanvin",
            },
            # 존재 — 콜라보 가드 테스트용 Fear of God 피스.
            {
                "product_id": "5003",
                "name": "Fear of God Essentials Travis Scott Cactus Jack Hoodie",
                "model_number": "FG2G3ABG001FOG",
                "brand": "Fear of God",
            },
        ],
    )
    return str(path)


@pytest.fixture
def fake_brands() -> list[dict]:
    return [
        {"brandNo": "BR01", "brandNm": "BALLY"},
        {"brandNo": "BR02", "brandNm": "LANVIN"},
        {"brandNo": "BR03", "brandNm": "FEAR OF GOD"},
        {"brandNo": "BR99", "brandNm": "TIME"},  # 화이트리스트 아님 → 제외
    ]


# ─── (a) dump_catalog: 브랜드 whitelist 필터 + 이벤트 publish ─

async def test_dump_catalog_publishes_event(bus, kream_db, fake_brands):
    fake = _FakeThehandsomeCrawler(
        brands=fake_brands,
        brand_items={
            "BR01": [
                _listing_item("BL2G3ABG001BAL",
                              "BALLY MAXIM LOW SNEAKER",
                              "BALLY"),
                _listing_item("BL2G3ABG999BAL",
                              "BALLY NEW ARRIVAL",
                              "BALLY"),
            ],
            "BR02": [
                _listing_item("LN2G3ABG001LAN",
                              "LANVIN CURB SNEAKER",
                              "LANVIN"),
            ],
            "BR99": [
                # TIME 은 whitelist 미포함 — 절대 덤프되면 안 됨
                _listing_item("TM2G3ABG111TIM", "TIME SHIRT", "TIME"),
            ],
        },
    )
    adapter = ThehandsomeAdapter(
        bus=bus,
        db_path=kream_db,
        crawler=fake,
        brand_whitelist=("BALLY", "LANVIN"),
        max_pages=2,
        page_size=20,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "thehandsome"
    assert event.product_count == 3  # BR01 2건 + BR02 1건
    assert len(products) == 3
    labels = {p["_brand_label"] for p in products}
    assert labels == {"BALLY", "LANVIN"}
    assert fake.brand_calls == 1
    assert len(fake.dump_calls) == 2  # BR01 + BR02, BR99/BR03 제외
    dumped_brand_nos = {c["brand_no"] for c in fake.dump_calls}
    assert dumped_brand_nos == {"BR01", "BR02"}
    assert received and received[0].product_count == 3


# ─── (b) match_to_kream: 다양한 케이스 분류 ───────────────

async def test_match_to_kream_classifies_items(bus, kream_db):
    fake = _FakeThehandsomeCrawler(brands=[], brand_items={})
    adapter = ThehandsomeAdapter(
        bus=bus,
        db_path=kream_db,
        crawler=fake,
    )

    products = [
        # (1) 정상 매칭 — BALLY Maxim
        _listing_item("BL2G3ABG001BAL",
                      "BALLY MAXIM LOW SNEAKER",
                      "BALLY"),
        # (2) 미등재 신상 → collect_queue
        _listing_item("BL2G3ABG777NEW",
                      "BALLY NEW DROP SNEAKER",
                      "BALLY"),
        # (3) 콜라보 가드 차단 — 크림은 Travis Scott, 소싱은 일반명
        _listing_item("FG2G3ABG001FOG",
                      "FEAR OF GOD ESSENTIALS HOODIE BLACK",
                      "FEAR OF GOD"),
        # (4) 품절 제외
        _listing_item("LN2G3ABG001LAN",
                      "LANVIN CURB SNEAKER",
                      "LANVIN",
                      sold_out=True),
        # (5) 모델번호 없음
        {
            "product_id": "",
            "name": "모델번호 없음",
            "brand": "BALLY",
            "brand_no": "",
            "lowr_brand_nm": "",
            "model_number": "",
            "price": 0,
            "original_price": 0,
            "dc_rate": 0.0,
            "url": "",
            "image_url": "",
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
    assert cand.source == "thehandsome"
    assert cand.kream_product_id == 5001
    assert cand.model_no == "BL2G3ABG001BAL"
    assert cand.retail_price == 890000
    assert "BL2G3ABG001BAL" in cand.url

    assert _count_queue(kream_db) == 1


# ─── (c) run_once 통계 정확성 ─────────────────────────────

async def test_run_once_stats(bus, kream_db, fake_brands):
    fake = _FakeThehandsomeCrawler(
        brands=fake_brands,
        brand_items={
            "BR01": [
                _listing_item("BL2G3ABG001BAL",
                              "BALLY MAXIM LOW SNEAKER",
                              "BALLY"),
                _listing_item("BL2G3ABG555OUT",
                              "BALLY OUTLET",
                              "BALLY",
                              sold_out=True),
            ],
        },
    )
    adapter = ThehandsomeAdapter(
        bus=bus,
        db_path=kream_db,
        crawler=fake,
        brand_whitelist=("BALLY",),
        max_pages=1,
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1


# ─── (d) 빈 브랜드 리스트 — 예외 안전 ────────────────────

async def test_empty_brand_list_is_safe(bus, kream_db):
    fake = _FakeThehandsomeCrawler(brands=[], brand_items={})
    adapter = ThehandsomeAdapter(
        bus=bus,
        db_path=kream_db,
        crawler=fake,
        brand_whitelist=("BALLY",),
    )
    event, products = await adapter.dump_catalog()
    assert event.product_count == 0
    assert products == []
    assert fake.brand_calls == 1
    assert fake.dump_calls == []


# ─── (e) whitelist 비교는 대소문자 무관 ───────────────────

async def test_whitelist_case_insensitive(bus, kream_db):
    fake = _FakeThehandsomeCrawler(
        brands=[{"brandNo": "BR01", "brandNm": "Bally"}],  # 소문자 혼합
        brand_items={
            "BR01": [
                _listing_item("BL2G3ABG001BAL",
                              "BALLY MAXIM LOW",
                              "Bally"),
            ],
        },
    )
    adapter = ThehandsomeAdapter(
        bus=bus,
        db_path=kream_db,
        crawler=fake,
        brand_whitelist=("BALLY",),  # 대문자
    )
    _, products = await adapter.dump_catalog()
    assert len(products) == 1
    assert products[0]["_brand_label"] == "Bally"


# ─── (f) dump 예외 격리 — 한 브랜드 실패해도 나머지 진행 ─

async def test_brand_dump_exception_isolated(bus, kream_db):
    class _FlakyCrawler(_FakeThehandsomeCrawler):
        async def dump_brand_goods(self, brand_no, *, page_size=40, max_pages=5):
            if brand_no == "BR01":
                raise RuntimeError("boom")
            return await super().dump_brand_goods(
                brand_no, page_size=page_size, max_pages=max_pages,
            )

    fake = _FlakyCrawler(
        brands=[
            {"brandNo": "BR01", "brandNm": "BALLY"},
            {"brandNo": "BR02", "brandNm": "LANVIN"},
        ],
        brand_items={
            "BR02": [
                _listing_item("LN2G3ABG001LAN", "LANVIN CURB", "LANVIN"),
            ],
        },
    )
    adapter = ThehandsomeAdapter(
        bus=bus,
        db_path=kream_db,
        crawler=fake,
        brand_whitelist=("BALLY", "LANVIN"),
    )
    _, products = await adapter.dump_catalog()
    # BR01 예외 — BR02 만 성공
    assert len(products) == 1
    assert products[0]["_brand_label"] == "LANVIN"


# ─── (g) DEFAULT_BRAND_WHITELIST 내용 건전성 ──────────────

def test_default_brand_whitelist_nonempty():
    assert len(DEFAULT_BRAND_WHITELIST) > 0
    # 모두 대문자여야 — 비교는 upper() 기준
    for nm in DEFAULT_BRAND_WHITELIST:
        assert nm == nm.upper()
