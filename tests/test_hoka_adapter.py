"""호카 푸시 어댑터 테스트 (Phase 3 배치 5).

실호출 금지: HTTP 레이어·크림 API 전부 mock. HOKA Coveo-Show 응답 파싱은
순수 함수 `_extract_tiles_from_html` 로 별도 검증.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.adapters.hoka_adapter import (
    DEFAULT_USD_TO_KRW,
    HOKA_SKU_RE,
    HokaAdapter,
)
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.crawlers.hoka import (
    HOKA_MODEL_RE,
    _extract_analytics_masters,
    _extract_tiles_from_html,
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


# ─── mock HTTP 레이어 ─────────────────────────────────────


class _FakeSize:
    def __init__(self, size: str, in_stock: bool = True):
        self.size = size
        self.in_stock = in_stock


class _FakeProduct:
    def __init__(self, sizes: list[_FakeSize]):
        self.sizes = sizes


class _FakeHokaHttp:
    """fetch_tiles_keyword(keyword) 만 mock."""

    def __init__(self, by_keyword: dict[str, list[dict]]):
        self._by_keyword = by_keyword
        self._pdp: dict[str, list[str]] = {}
        self.calls: list[str] = []

    async def fetch_tiles_keyword(self, keyword: str) -> list[dict]:
        self.calls.append(keyword)
        return list(self._by_keyword.get(keyword, []))

    async def get_product_detail(self, product_id: str):
        sizes = self._pdp.get(product_id, ["270"])
        if not sizes:
            return None
        return _FakeProduct([_FakeSize(s, True) for s in sizes])


def _mk_tile(
    sku: str,
    name: str,
    price_usd: float = 155.0,
    available: bool = True,
) -> dict:
    master, _, color = sku.partition("-")
    slug = name.lower().replace(" ", "-")
    return {
        "sku": sku,
        "master_id": master,
        "color_code": color,
        "name": name,
        "url": f"https://www.hoka.com/en/us/mens-road/{slug}/{master}.html"
        f"?dwvar_{master}_color={color}",
        "price_usd": price_usd,
        "available": available,
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
            # 정상 매칭 — Mach Remastered
            {
                "product_id": "901",
                "name": "HOKA Mach Remastered Frost Cream Green",
                "model_number": "1176251-FCG",
                "brand": "HOKA",
            },
            # 콜라보 가드 — 크림=Sacai, 소싱=일반 Mach 7
            {
                "product_id": "902",
                "name": "HOKA x Sacai Mach 7",
                "model_number": "1171904-ASRN",
                "brand": "HOKA",
            },
        ],
    )
    return str(path)


# ─── (a) SKU 정규식 — 경계 케이스 ─────────────────────────


def test_hoka_sku_regex():
    assert HOKA_SKU_RE.match("1176251-FCG")
    assert HOKA_SKU_RE.match("1168872-WBS")
    assert HOKA_SKU_RE.match("1122930-BBBLC")
    # 잘못된 패턴 reject
    assert not HOKA_SKU_RE.match("176251-FCG")       # master 6자리
    assert not HOKA_SKU_RE.match("11762510-FCG")     # master 8자리
    assert not HOKA_SKU_RE.match("1176251-fcg")      # 소문자 color
    assert not HOKA_SKU_RE.match("1176251-F")        # color 1자리
    assert not HOKA_SKU_RE.match("1176251FCG")       # hyphen 누락
    assert not HOKA_SKU_RE.match("")


def test_model_re_extracts_from_text():
    sample = "상품: 1171904-ASRN 와 1168871-NZS 등"
    hits = HOKA_MODEL_RE.findall(sample)
    assert ("1171904", "ASRN") in hits
    assert ("1168871", "NZS") in hits


# ─── (b) HTML 파서 — 순수 함수 ────────────────────────────


def test_extract_tiles_from_html_minimal():
    """Coveo-Show 응답의 tile 블록 3개를 파싱할 수 있는가."""
    html_doc = """
    <div class="tile-suggest" data-suggestion-pid="198605568138" data-status="2">
      <a href="/en/us/mens-road/mach-7/1171904.html?dwvar_1171904_color=ASRN">
        <div class="name">Mach 7</div>
        <span class="sales">$170.00</span>
      </a>
    </div>
    <div class="tile-suggest" data-suggestion-pid="198605843549" data-status="2">
      <a href="/en/us/womens-road/clifton-10/1162031.html?dwvar_1162031_color=BRGL">
        <div class="name"><span>Women's</span><br/> Clifton 10</div>
        <span class="sales">$155.00</span>
      </a>
    </div>
    <div class="tile-suggest" data-suggestion-pid="198605567926" data-status="2">
      <a href="/en/us/mens-road/mach-remastered/1176251.html?dwvar_1176251_color=FCG">
        <div class="name">Mach Remastered</div>
        <span class="sales">$170.00</span>
      </a>
    </div>
    """
    tiles = _extract_tiles_from_html(html_doc)
    skus = {t.sku for t in tiles}
    assert skus == {"1171904-ASRN", "1162031-BRGL", "1176251-FCG"}
    by_sku = {t.sku: t for t in tiles}
    assert by_sku["1171904-ASRN"].name == "Mach 7"
    assert by_sku["1171904-ASRN"].price_usd == 170.0
    assert by_sku["1162031-BRGL"].name.startswith("Women")
    assert "Clifton 10" in by_sku["1162031-BRGL"].name
    assert by_sku["1162031-BRGL"].price_usd == 155.0
    # 절대 URL
    assert by_sku["1176251-FCG"].url.startswith("https://www.hoka.com")


def test_extract_tiles_dedup_duplicate_sku():
    html_doc = """
    <div class="tile-suggest" data-suggestion-pid="100000000001">
      <a href="/en/us/mens-road/mach-7/1171904.html?dwvar_1171904_color=ASRN">
        <div class="name">Mach 7</div><span class="sales">$170.00</span>
      </a>
    </div>
    <div class="tile-suggest" data-suggestion-pid="100000000002">
      <a href="/en/us/mens-road/mach-7/1171904.html?dwvar_1171904_color=ASRN">
        <div class="name">Mach 7</div><span class="sales">$170.00</span>
      </a>
    </div>
    """
    tiles = _extract_tiles_from_html(html_doc)
    assert len(tiles) == 1


def test_extract_tiles_from_tile_row_main_grid():
    """메인 그리드 ``tile-row`` 블록 + ``data-tile-analytics`` JSON 파싱.

    2026-04-14: 프로덕션 Coveo-Show 응답 실 구조. customData.id 에서 color 추출,
    customData.name 과 가격·href 독립 파싱.
    """
    html_doc = (
        '<div class="tile-row col-6 col-md-4" '
        'data-tile-analytics="{&quot;customData&quot;:{&quot;contentIDValue&quot;:&quot;1147810&quot;,'
        '&quot;name&quot;:&quot;Mach 6&quot;,&quot;id&quot;:&quot;1147810-RSLT-10.5B&quot;}}">'
        '<a href="/en/us/womens-road/mach-6/1147810.html?dwvar_1147810_color=RSLT">link</a>'
        '<span class="sales">$140.00</span>'
        '</div>'
        '<div class="tile-row col-6 col-md-4" '
        'data-tile-analytics="{&quot;customData&quot;:{&quot;contentIDValue&quot;:&quot;1176251&quot;,'
        '&quot;name&quot;:&quot;Mach Remastered&quot;,&quot;id&quot;:&quot;1176251-FCG-09D&quot;}}">'
        '<a href="/en/us/all-gender/mach-remastered/1176251.html?dwvar_1176251_color=FCG">link</a>'
        '<span class="sales">$170.00</span>'
        '</div>'
    )
    tiles = _extract_tiles_from_html(html_doc)
    assert {t.sku for t in tiles} == {"1147810-RSLT", "1176251-FCG"}
    by_sku = {t.sku: t for t in tiles}
    assert by_sku["1147810-RSLT"].name == "Mach 6"
    assert by_sku["1147810-RSLT"].price_usd == 140.0
    assert by_sku["1147810-RSLT"].master_id == "1147810"
    assert by_sku["1147810-RSLT"].color_code == "RSLT"
    assert by_sku["1176251-FCG"].url.startswith("https://www.hoka.com/en/us/all-gender")


def test_extract_tiles_tile_row_detects_sold_out():
    html_doc = (
        '<div class="tile-row" '
        'data-tile-analytics="{&quot;customData&quot;:{&quot;contentIDValue&quot;:&quot;1111111&quot;,'
        '&quot;name&quot;:&quot;Test&quot;,&quot;id&quot;:&quot;1111111-AAA-09D&quot;}}">'
        '<a href="/en/us/x/y/1111111.html?dwvar_1111111_color=AAA">x</a>'
        '<button class="sold-out">Notify Me</button>'
        '<span class="sales">$100.00</span>'
        '</div>'
    )
    tiles = _extract_tiles_from_html(html_doc)
    assert len(tiles) == 1
    assert tiles[0].available is False


def test_extract_analytics_masters():
    payload = (
        '<div class="search-results" '
        'data-search-analytics="{&quot;masterIDs&quot;:[&quot;1171904&quot;,&quot;1176251&quot;],'
        '&quot;productSKUs&quot;:[&quot;1171904-ASRN-09D&quot;,&quot;1176251-FCG-07.5B&quot;],'
        '&quot;productNames&quot;:[&quot;Mach 7&quot;,&quot;Mach Remastered&quot;]}"></div>'
    )
    rows = _extract_analytics_masters(payload)
    assert len(rows) == 2
    assert rows[0]["master_id"] == "1171904"
    assert rows[0]["full_sku"] == "1171904-ASRN-09D"
    assert rows[1]["name"] == "Mach Remastered"


# ─── (c) dump_catalog — publish + 키워드 순회 + dedup ────


async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeHokaHttp(
        by_keyword={
            "Mach": [
                _mk_tile("1171904-ASRN", "Mach 7", 170.0),
                _mk_tile("1176251-FCG", "Mach Remastered", 170.0),
            ],
            # 다른 키워드에서 Mach 7 중복 — dedup 되어야 함
            "Hoka": [
                _mk_tile("1171904-ASRN", "Mach 7", 170.0),
                _mk_tile("1162031-BRGL", "Clifton 10", 155.0),
            ],
        }
    )
    adapter = HokaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        keywords=("Mach", "Hoka"),
        usd_to_krw=1400,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, variants = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "hoka"
    assert event.product_count == 3  # dedup 후 3개
    assert len(variants) == 3
    assert fake_http.calls == ["Mach", "Hoka"]
    # KRW 환산 확인 (170 * 1400 = 238000)
    by_sku = {v["sku"]: v for v in variants}
    assert by_sku["1171904-ASRN"]["price_krw"] == 238000
    assert by_sku["1162031-BRGL"]["price_krw"] == 217000  # 155 * 1400
    assert received and received[0].product_count == 3


# ─── (d) match_to_kream — 매칭/큐/가드 분류 ────────────


async def test_match_to_kream_classifies(bus, kream_db):
    adapter = HokaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakeHokaHttp(by_keyword={}),
        keywords=(),
    )

    variants = [
        # (1) 정상 매칭 — Mach Remastered (크림 존재)
        {
            "sku": "1176251-FCG",
            "master_id": "1176251",
            "color_code": "FCG",
            "name": "Mach Remastered",
            "url": "https://www.hoka.com/en/us/.../1176251.html?dwvar_1176251_color=FCG",
            "price_usd": 170.0,
            "price_krw": 238000,
            "available": True,
        },
        # (2) 동일 SKU 중복 — dedup 되어야 함
        {
            "sku": "1176251-FCG",
            "master_id": "1176251",
            "color_code": "FCG",
            "name": "Mach Remastered duplicate",
            "url": "https://x",
            "price_usd": 170.0,
            "price_krw": 238000,
            "available": True,
        },
        # (3) 미등재 신상 → collect_queue
        {
            "sku": "1199999-NEW",
            "master_id": "1199999",
            "color_code": "NEW",
            "name": "Mach 99 (Future)",
            "url": "https://www.hoka.com/en/us/.../1199999.html",
            "price_usd": 180.0,
            "price_krw": 252000,
            "available": True,
        },
        # (4) 콜라보 가드 — 크림=Sacai 콜라보, 소싱=일반 Mach 7 → 차단
        {
            "sku": "1171904-ASRN",
            "master_id": "1171904",
            "color_code": "ASRN",
            "name": "Mach 7",
            "url": "https://www.hoka.com/en/us/.../1171904.html",
            "price_usd": 170.0,
            "price_krw": 238000,
            "available": True,
        },
        # (5) 품절 → soldout_dropped
        {
            "sku": "1176251-FCG",
            "master_id": "1176251",
            "color_code": "FCG",
            "name": "Mach Remastered",
            "url": "https://x",
            "price_usd": 170.0,
            "price_krw": 238000,
            "available": False,
        },
        # (6) invalid SKU — 4자리 color
        {
            "sku": "1176251-FCGXX",
            "master_id": "1176251",
            "color_code": "FCGXX",
            "name": "Bad",
            "url": "https://x",
            "price_usd": 0.0,
            "price_krw": 0,
            "available": True,
        },
        # (7) invalid SKU — master 6자리
        {
            "sku": "117625-FCG",
            "master_id": "117625",
            "color_code": "FCG",
            "name": "Bad 2",
            "url": "https://x",
            "price_usd": 0.0,
            "price_krw": 0,
            "available": True,
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(variants)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.dumped == 7
    assert stats.soldout_dropped == 1
    # (6) FCGXX 는 5자리라 정규식 ``[A-Z]{2,6}`` 통과 — invalid 아님.
    # 대신 (7) 6자리 master 는 invalid.
    # 재확인: FCGXX 는 5글자 → 통과 → 크림 DB 없음 → collect_queue.
    assert stats.invalid_sku == 1
    assert stats.matched == 1
    assert stats.skipped_guard == 1
    # collect_queue: (3) 1199999-NEW + (6) 1176251-FCGXX = 2건
    assert stats.collected_to_queue == 2

    assert len(matches) == 1
    cand = matches[0]
    assert cand.source == "hoka"
    assert cand.kream_product_id == 901
    assert cand.model_no == "1176251-FCG"
    assert cand.retail_price == 238000
    assert cand.size == ""
    assert "1176251" in cand.url
    assert len(received) == 1

    # collect_queue 적재 건수 확인
    assert _count_queue(kream_db) == 2


# ─── (e) run_once 통계 정확성 ─────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeHokaHttp(
        by_keyword={
            "Mach": [
                _mk_tile("1176251-FCG", "Mach Remastered", 170.0),
                _mk_tile("1199999-NEW", "Mach 99", 180.0),
            ],
        }
    )
    adapter = HokaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        keywords=("Mach",),
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["collected_to_queue"] == 1
    assert stats["soldout_dropped"] == 0
    assert stats["invalid_sku"] == 0


async def test_usd_to_krw_override(bus, kream_db):
    """환율 override 가 매칭 retail_price 에 반영되는가."""
    fake_http = _FakeHokaHttp(
        by_keyword={
            "Mach": [_mk_tile("1176251-FCG", "Mach Remastered", 170.0)],
        }
    )
    adapter = HokaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        keywords=("Mach",),
        usd_to_krw=1500,
    )
    _, variants = await adapter.dump_catalog()
    assert variants[0]["price_krw"] == 255000  # 170 * 1500
    matches, _ = await adapter.match_to_kream(variants)
    assert matches and matches[0].retail_price == 255000


# ─── (f) DEFAULT_USD_TO_KRW 상수 기본값 확인 ──────────────


def test_default_usd_to_krw_constant():
    assert DEFAULT_USD_TO_KRW == 1400
