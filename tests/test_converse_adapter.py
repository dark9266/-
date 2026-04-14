"""컨버스 코리아 푸시 어댑터 테스트.

실호출 금지 — sitemap.xml + detail HTML 페이지 응답을 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.adapters.converse_adapter import (
    BASE_URL,
    ConverseAdapter,
    ConverseMatchStats,
    _color_tokens_kr,
    _extract_model_number,
    _extract_price,
    _extract_title,
    _is_soldout,
)
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus

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


class _FakeConverseHttp:
    """sitemap + detail mock — 미리 등록한 매핑을 그대로 반환."""

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


def _mk_sitemap(entries: list[tuple[str, str]]) -> str:
    """entries: list of (slug, product_no)."""
    body = []
    for slug, pno in entries:
        body.append(f"<url><loc>https://www.converse.co.kr/product/{slug}/{pno}/</loc></url>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(body) + "</urlset>"
    )


def _mk_detail(
    *,
    title: str = "Converse - 척 70 하이 블랙",
    model: str = "162050C",
    price: int = 109000,
    soldout: bool = False,
) -> str:
    soldout_block = '<div class="soldout_msg">품절 상품입니다</div>' if soldout else ""
    return f"""<html><head>
<meta property="og:title" content="{title}" />
<meta property="product:price:amount" content="{price}" />
</head><body>
<div class="ma_product_code">{model}</div>
{soldout_block}
</body></html>"""


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
            # (1) 단일 매칭 — 162050C
            {
                "product_id": "1001",
                "name": "컨버스 척 70 하이 블랙",
                "model_number": "162050C",
                "brand": "Converse",
            },
            # (2) 슬래시 결합형 — M3310C/M3310 (둘 다 키)
            {
                "product_id": "1002",
                "name": "컨버스 척 테일러 올스타 하이 클래식 블랙 모노크롬",
                "model_number": "M3310C/M3310",
                "brand": "Converse",
            },
            # (3) 동일 모델번호 다중 색상 후보 — A20645C 블랙/화이트
            {
                "product_id": "1003",
                "name": "컨버스 런스타 샌들 레더 블랙",
                "model_number": "A20645C",
                "brand": "Converse",
            },
            {
                "product_id": "1004",
                "name": "컨버스 런스타 샌들 레더 화이트",
                "model_number": "A20645C",
                "brand": "Converse",
            },
        ],
    )
    return str(path)


# ─── (a) 순수 추출 함수 ───────────────────────────────────


def test_extract_model_number_from_div():
    html = '<div class="ma_product_code">A20645C</div>'
    assert _extract_model_number(html) == "A20645C"


def test_extract_model_number_fallback_pattern():
    html = "<p>품번: 162050C 입고 안내</p>"
    assert _extract_model_number(html) == "162050C"


def test_extract_model_number_none():
    assert _extract_model_number("<html>no model here</html>") == ""


def test_extract_price_meta():
    html = '<meta property="product:price:amount" content="125000" />'
    assert _extract_price(html) == 125000


def test_extract_title_og():
    html = '<meta property="og:title" content="Converse - 척 70 하이" />'
    assert _extract_title(html) == "Converse - 척 70 하이"


def test_color_tokens_kr_basic():
    assert "블랙" in _color_tokens_kr("Black Leather")
    assert "화이트" in _color_tokens_kr("Off White / Cream")


def test_is_soldout_marker():
    assert _is_soldout('<div class="soldout_msg">품절</div>') is True
    assert _is_soldout("<div>판매중</div>") is False


# ─── (b) dump_catalog publish ─────────────────────────────


async def test_dump_catalog_publishes_event(bus, kream_db):
    sitemap = _mk_sitemap(
        [
            ("chuck-70-hi-black", "6517"),
            ("runstar-sandal-leather-black", "15458"),
        ]
    )
    details = {
        "https://www.converse.co.kr/product/chuck-70-hi-black/6517/": _mk_detail(
            title="Converse - 척 70 하이 블랙",
            model="162050C",
            price=109000,
        ),
        "https://www.converse.co.kr/product/runstar-sandal-leather-black/15458/": _mk_detail(
            title="Converse - 런스타 샌들 레더 블랙",
            model="A20645C",
            price=125000,
        ),
    }
    fake = _FakeConverseHttp(sitemap, details)
    adapter = ConverseAdapter(
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

    assert event.source == "converse"
    assert event.product_count == 2
    assert len(items) == 2
    assert fake.sitemap_calls == 1
    assert len(fake.detail_calls) == 2
    assert received and received[0].product_count == 2
    # 모델번호 추출 확인
    models = sorted(it["model_number"] for it in items)
    assert models == ["162050C", "A20645C"]


# ─── (c) match_to_kream — 분류 ─────────────────────────────


async def test_match_to_kream_classifies(bus, kream_db):
    adapter = ConverseAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakeConverseHttp("", {}),
        request_interval_sec=0,
    )

    items = [
        # (1) 단일 매칭
        {
            "product_no": "6517",
            "url": f"{BASE_URL}/product/chuck-70-hi-black/6517/",
            "model_number": "162050C",
            "title": "Converse - 척 70 하이 블랙",
            "price": 109000,
            "soldout": False,
        },
        # (2) 슬래시 결합 매칭 — M3310C → 크림 "M3310C/M3310" 인덱스 가능
        {
            "product_no": "6520",
            "url": f"{BASE_URL}/product/all-star-mono-black/6520/",
            "model_number": "M3310C",
            "title": "Converse - 척 테일러 올스타 모노크롬 블랙",
            "price": 89000,
            "soldout": False,
        },
        # (3) 색상 disambiguation — A20645C 두 후보 중 "블랙" 토큰으로 1004 제외
        {
            "product_no": "15458",
            "url": f"{BASE_URL}/product/runstar-sandal-black/15458/",
            "model_number": "A20645C",
            "title": "Converse - 런스타 샌들 블랙",
            "price": 125000,
            "soldout": False,
        },
        # (4) 미등재 신상 → collect_queue
        {
            "product_no": "9999",
            "url": f"{BASE_URL}/product/new-thing/9999/",
            "model_number": "Z99999C",
            "title": "Converse - 신상 모델",
            "price": 99000,
            "soldout": False,
        },
        # (5) 모델번호 없음
        {
            "product_no": "8888",
            "url": f"{BASE_URL}/product/broken/8888/",
            "model_number": "",
            "title": "Converse - 파싱 실패",
            "price": 0,
            "soldout": False,
        },
        # (6) 품절 상품 — 매칭 가능하지만 후보 발행 보류
        {
            "product_no": "7777",
            "url": f"{BASE_URL}/product/chuck-70-soldout/7777/",
            "model_number": "162050C",
            "title": "Converse - 척 70 하이 블랙 (품절)",
            "price": 109000,
            "soldout": True,
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(items)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert isinstance(stats, ConverseMatchStats)
    assert stats.dumped == 6
    assert stats.no_model_number == 1
    assert stats.soldout_dropped == 1
    # 단일 + 슬래시 + 색상 = 3 매칭. 품절은 같은 norm 이라 dedup 으로 컨슈머
    assert stats.matched == 3
    assert stats.matched_by_color == 1

    assert len(matches) == 3
    by_model = {m.model_no: m for m in matches}
    assert "162050C" in by_model
    assert "M3310C" in by_model
    assert "A20645C" in by_model
    assert by_model["162050C"].kream_product_id == 1001
    assert by_model["M3310C"].kream_product_id == 1002
    assert by_model["A20645C"].kream_product_id == 1003  # 블랙
    assert by_model["A20645C"].source == "converse"

    # collect_queue: Z99999C 1건
    assert _count_queue(kream_db) == 1

    assert len(received) == 3


# ─── (d) run_once 통계 정확성 ──────────────────────────────


async def test_run_once_stats(bus, kream_db):
    sitemap = _mk_sitemap(
        [
            ("chuck-70-hi-black", "6517"),
            ("new-unknown", "9999"),
        ]
    )
    details = {
        "https://www.converse.co.kr/product/chuck-70-hi-black/6517/": _mk_detail(
            model="162050C", price=109000
        ),
        "https://www.converse.co.kr/product/new-unknown/9999/": _mk_detail(
            title="Converse - 신상", model="Z99999C", price=99000
        ),
    }
    fake = _FakeConverseHttp(sitemap, details)
    adapter = ConverseAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        request_interval_sec=0,
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["collected_to_queue"] == 1
    assert stats["soldout_dropped"] == 0
    assert stats["no_model_number"] == 0


# ─── (e) sitemap 비어있을 때 ──────────────────────────────


async def test_dump_catalog_empty_sitemap(bus, kream_db):
    fake = _FakeConverseHttp("", {})
    adapter = ConverseAdapter(
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


# ─── (f) HTTP detail 실패 시 격리 ──────────────────────────


async def test_dump_catalog_handles_detail_failure(bus, kream_db):
    sitemap = _mk_sitemap([("chuck-70-hi-black", "6517")])

    class _BrokenHttp(_FakeConverseHttp):
        async def fetch_detail(self, url: str) -> str:
            raise RuntimeError("boom")

    fake = _BrokenHttp(sitemap, {})
    adapter = ConverseAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        request_interval_sec=0,
    )
    event, items = await adapter.dump_catalog()
    assert event.product_count == 0
    assert items == []
    assert adapter._last_fetch_errors == 1


# ─── (g) sitemap 파서 단위 ─────────────────────────────────


def test_parse_sitemap_products_dedup():
    xml = _mk_sitemap(
        [
            ("a", "100"),
            ("b", "200"),
            ("a-dup", "100"),  # 같은 product_no — dedup
        ]
    )
    parsed = ConverseAdapter._parse_sitemap_products(xml)
    assert len(parsed) == 2
    assert parsed[0][1] == "100"
    assert parsed[1][1] == "200"
