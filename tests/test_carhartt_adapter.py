"""Carhartt WIP 글로벌 EU 어댑터 테스트.

실호출 금지 — sitemap.xml + detail HTML 모두 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.adapters.carhartt_adapter import (
    BASE_URL,
    LOCALE_PATH,
    CarharttAdapter,
    CarharttMatchStats,
    _extract_currency,
    _extract_name,
    _extract_price,
    _extract_variant_codes,
    _is_soldout,
    _normalize_variant,
)
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus

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


class _FakeCarharttHttp:
    def __init__(self, sitemap_xml: str, detail_map: dict[str, str]):
        self._sitemap = sitemap_xml
        self._detail_map = detail_map
        self.sitemap_calls = 0
        self.detail_calls: list[str] = []

    async def fetch_sitemap(self) -> str:
        self.sitemap_calls += 1
        return self._sitemap

    async def fetch_detail(self, url: str) -> str:
        self.detail_calls.append(url)
        return self._detail_map.get(url, "")


def _mk_sitemap(slugs: list[str]) -> str:
    body = "".join(f"<url><loc>{BASE_URL}{LOCALE_PATH}/p/{slug}</loc></url>" for slug in slugs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>"
    )


def _mk_detail(
    *,
    name: str = "OG Single Knee Pant",
    primary_sku: str = "I034871_01_01",
    variants: tuple[str, ...] = ("I034871_01_01", "I034871_01_UR", "I034871_01_4Q"),
    price: int = 165,
    currency: str = "GBP",
    availability: str = "InStock",
) -> str:
    variant_dump = " ".join(variants)
    return f"""<html><head>
<script type="application/ld+json">
{{
  "@context": "https://schema.org/",
  "@type": "Product",
  "sku": "{primary_sku}",
  "name": "{name}",
  "offers": {{
    "@type": "Offer",
    "price": "{price}",
    "priceCurrency": "{currency}",
    "availability": "https://schema.org/{availability}"
  }}
}}
</script>
</head><body>
<div class="variants">{variant_dump}</div>
</body></html>"""


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def kream_db(tmp_path):
    path = tmp_path / "kream.db"
    _init_kream_db(
        str(path),
        rows=[
            # 단일 variant 매칭 — I034871-01-01 (크림에 동일 키 보유)
            {
                "product_id": "2001",
                "name": "칼하트 WIP OG 싱글 니 팬츠 블루 린스드",
                "model_number": "I034871-01-01",
                "brand": "Carhartt WIP",
            },
            # 같은 style 의 다른 컬러 — I034871-01-UR
            {
                "product_id": "2002",
                "name": "칼하트 WIP OG 싱글 니 팬츠 블루 버스트 워시드",
                "model_number": "I034871-01-UR",
                "brand": "Carhartt WIP",
            },
            # 디트로이트 자켓 — I033112-00E-02
            {
                "product_id": "2003",
                "name": "칼하트 WIP 디트로이트 자켓 블랙",
                "model_number": "I033112-00E-02",
                "brand": "Carhartt WIP",
            },
            # 슬래시 결합형 — I006288/I031468-89-XX
            {
                "product_id": "2004",
                "name": "칼하트 WIP 킥플립 백팩 블랙",
                "model_number": "I006288/I031468-89-XX",
                "brand": "Carhartt WIP",
            },
        ],
    )
    return str(path)


# ─── (a) 순수 추출 함수 ─────────────────────────────────


def test_extract_variant_codes_from_detail():
    html = _mk_detail(
        primary_sku="I034871_01_01",
        variants=("I034871_01_01", "I034871_01_UR", "I023083_HZ_01"),
    )
    codes = _extract_variant_codes(html)
    # 최소한 세 variant 모두 포함
    assert "I034871_01_01" in codes
    assert "I034871_01_UR" in codes
    assert "I023083_HZ_01" in codes


def test_extract_variant_codes_empty():
    assert _extract_variant_codes("") == []
    assert _extract_variant_codes("<html>no style here</html>") == []


def test_extract_name_from_json_ld():
    html = _mk_detail(name="Active Jacket Winter")
    assert _extract_name(html) == "Active Jacket Winter"


def test_extract_price_and_currency():
    html = _mk_detail(price=185, currency="GBP")
    assert _extract_price(html) == 185
    assert _extract_currency(html) == "GBP"


def test_is_soldout_availability():
    assert _is_soldout(_mk_detail(availability="OutOfStock")) is True
    assert _is_soldout(_mk_detail(availability="InStock")) is False
    assert _is_soldout("") is False


def test_normalize_variant_underscore_to_dash():
    assert _normalize_variant("I034871_01_01") == "I034871-01-01"
    assert _normalize_variant("I023083_HZ_01") == "I023083-HZ-01"


# ─── (b) dump_catalog publish ───────────────────────────


async def test_dump_catalog_publishes_event(bus, kream_db):
    sitemap = _mk_sitemap(["og-single-knee-pant-blue-rigid-1131", "detroit-jacket-black-123"])
    details = {
        f"{BASE_URL}{LOCALE_PATH}/p/og-single-knee-pant-blue-rigid-1131": _mk_detail(
            name="OG Single Knee Pant",
            primary_sku="I034871_01_01",
            variants=("I034871_01_01", "I034871_01_UR"),
            price=165,
        ),
        f"{BASE_URL}{LOCALE_PATH}/p/detroit-jacket-black-123": _mk_detail(
            name="Detroit Jacket",
            primary_sku="I033112_00E_02",
            variants=("I033112_00E_02",),
            price=215,
        ),
    }
    fake = _FakeCarharttHttp(sitemap, details)
    adapter = CarharttAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        request_interval_sec=0,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, items = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "carhartt"
    assert event.product_count == 2
    assert len(items) == 2
    assert fake.sitemap_calls == 1
    assert len(fake.detail_calls) == 2
    assert received and received[0].product_count == 2
    # variants 가 두 번째 상품까지 전부 파싱됐는지
    all_codes = [c for it in items for c in it["variants"]]
    assert "I034871_01_01" in all_codes
    assert "I033112_00E_02" in all_codes


# ─── (c) match_to_kream — 분류 ──────────────────────────


async def test_match_to_kream_classifies(bus, kream_db):
    adapter = CarharttAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakeCarharttHttp("", {}),
        request_interval_sec=0,
    )

    items = [
        # (1) 단일 product-family → 2 variant 매칭 (둘 다 크림에 있음)
        {
            "url": f"{BASE_URL}{LOCALE_PATH}/p/og-single-knee-1131",
            "variants": ["I034871_01_01", "I034871_01_UR"],
            "name": "OG Single Knee Pant",
            "price": 165,
            "currency": "GBP",
            "soldout": False,
        },
        # (2) 디트로이트 자켓 — exact match
        {
            "url": f"{BASE_URL}{LOCALE_PATH}/p/detroit-jacket-123",
            "variants": ["I033112_00E_02"],
            "name": "Detroit Jacket",
            "price": 215,
            "currency": "GBP",
            "soldout": False,
        },
        # (3) 미등재 신상 → collect_queue
        {
            "url": f"{BASE_URL}{LOCALE_PATH}/p/new-thing-999",
            "variants": ["I099999_00_01"],
            "name": "New Experimental",
            "price": 99,
            "currency": "GBP",
            "soldout": False,
        },
        # (4) variant 없음 → no_model_number
        {
            "url": f"{BASE_URL}{LOCALE_PATH}/p/broken-888",
            "variants": [],
            "name": "Broken",
            "price": 0,
            "currency": "",
            "soldout": False,
        },
        # (5) 품절 — 매칭은 가능하지만 후보 발행 보류
        {
            "url": f"{BASE_URL}{LOCALE_PATH}/p/detroit-jacket-sold-456",
            # dedup 피하기 위해 다른 variant 사용
            "variants": ["I033112_00E_02"],  # already matched above → dedup 발생
            "name": "Detroit Jacket soldout",
            "price": 215,
            "currency": "GBP",
            "soldout": True,
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(items)
    while not queue.empty():
        received.append(queue.get_nowait())

    assert isinstance(stats, CarharttMatchStats)
    assert stats.dumped == 5
    assert stats.no_model_number == 1  # item (4)
    # matched: I034871-01-01, I034871-01-UR, I033112-00E-02 = 3
    assert stats.matched == 3
    # collect_queue: I099999-00-01 1건
    assert _count_queue(kream_db) == 1

    by_model = {m.model_no: m for m in matches}
    assert "I034871-01-01" in by_model
    assert "I034871-01-UR" in by_model
    assert "I033112-00E-02" in by_model
    assert by_model["I034871-01-01"].kream_product_id == 2001
    assert by_model["I034871-01-UR"].kream_product_id == 2002
    assert by_model["I033112-00E-02"].kream_product_id == 2003
    assert by_model["I034871-01-01"].source == "carhartt"

    assert len(received) == 3


# ─── (d) run_once 통계 ──────────────────────────────────


async def test_run_once_stats(bus, kream_db):
    sitemap = _mk_sitemap(["og-single-knee-1", "unknown-999"])
    details = {
        f"{BASE_URL}{LOCALE_PATH}/p/og-single-knee-1": _mk_detail(
            name="OG Single Knee Pant",
            primary_sku="I034871_01_01",
            variants=("I034871_01_01",),
            price=165,
        ),
        f"{BASE_URL}{LOCALE_PATH}/p/unknown-999": _mk_detail(
            name="Unknown Thing",
            primary_sku="I099999_00_01",
            variants=("I099999_00_01",),
            price=50,
        ),
    }
    fake = _FakeCarharttHttp(sitemap, details)
    adapter = CarharttAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        request_interval_sec=0,
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["collected_to_queue"] == 1
    assert stats["no_model_number"] == 0


# ─── (e) 빈 sitemap ─────────────────────────────────────


async def test_dump_catalog_empty_sitemap(bus, kream_db):
    fake = _FakeCarharttHttp("", {})
    adapter = CarharttAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        request_interval_sec=0,
    )
    event, items = await adapter.dump_catalog()
    assert event.product_count == 0
    assert items == []
    assert fake.sitemap_calls == 1
    assert fake.detail_calls == []


# ─── (f) detail 실패 격리 ───────────────────────────────


async def test_dump_catalog_handles_detail_failure(bus, kream_db):
    sitemap = _mk_sitemap(["broken-1"])

    class _BrokenHttp(_FakeCarharttHttp):
        async def fetch_detail(self, url: str) -> str:
            raise RuntimeError("boom")

    fake = _BrokenHttp(sitemap, {})
    adapter = CarharttAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        request_interval_sec=0,
    )
    event, items = await adapter.dump_catalog()
    assert event.product_count == 0
    assert items == []
    assert adapter._last_fetch_errors == 1


# ─── (g) sitemap 파서 dedup ─────────────────────────────


def test_parse_sitemap_dedup():
    xml = _mk_sitemap(["a-1", "b-2", "a-1"])
    parsed = CarharttAdapter._parse_sitemap(xml)
    assert len(parsed) == 2
