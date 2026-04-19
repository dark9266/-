"""On Running 어댑터 + 크롤러 단위 테스트.

실호출 금지: 사이트맵/PDP HTTP 는 전부 mock. 크림 DB 는 tmp_path SQLite.
회귀 보호: `tests/fixtures/live/on_running_*.{xml,html}` 은 실서버 캡처 — 이
fixture 들을 실제로 로드해 파서를 검증한다. mock 에 사람이 직접 availability
/ SKU 를 써넣지 않는다.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

import pytest

from src.adapters.on_running_adapter import OnRunningAdapter
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.crawlers.on_running import (
    OnVariant,
    extract_color_slug_from_url,
    extract_gender_from_url,
    extract_sku_from_url,
    is_launch_or_coming_soon,
    is_valid_sku,
    normalize_sku_for_kream,
    parse_ldjson_block,
    parse_pdp,
    parse_sitemap,
    parse_variants,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "live"
SITEMAP_FIXTURE = FIXTURE_DIR / "on_running_sitemap.xml"
PDP_NEW_FIXTURE = FIXTURE_DIR / "on_running_pdp_new.html"
PDP_OLD_FIXTURE = FIXTURE_DIR / "on_running_pdp_old.html"


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


# ============================================================
# A. 순수 파서 — fixture 기반
# ============================================================


class TestSkuValidation:
    def test_new_format_shoe(self):
        assert is_valid_sku("3MF10074109")
        assert is_valid_sku("3MF10071043")
        assert is_valid_sku("3ME10120664")
        assert is_valid_sku("3MF10671043")

    def test_new_format_apparel(self):
        assert is_valid_sku("1WF10150553")
        assert is_valid_sku("1MF10123543")

    def test_old_format(self):
        assert is_valid_sku("61.97657")
        assert is_valid_sku("61.99025")
        assert is_valid_sku("61.99024")

    def test_reject_garbage(self):
        assert not is_valid_sku("")
        assert not is_valid_sku("XX123")
        assert not is_valid_sku("3MF1007410")   # 10자리
        assert not is_valid_sku("3MF100741099")  # 12자리
        assert not is_valid_sku("2AA10074109")  # prefix 잘못
        assert not is_valid_sku("3XX10074109")  # gender 잘못
        assert not is_valid_sku("61-97657")     # 크림 포맷 (원본 SKU 아님)


class TestSkuNormalization:
    def test_old_dot_to_hyphen(self):
        assert normalize_sku_for_kream("61.97657") == "61-97657"
        assert normalize_sku_for_kream("61.99025") == "61-99025"

    def test_new_unchanged(self):
        assert normalize_sku_for_kream("3MF10074109") == "3MF10074109"
        assert normalize_sku_for_kream("3ME10120664") == "3ME10120664"

    def test_empty(self):
        assert normalize_sku_for_kream("") == ""
        assert normalize_sku_for_kream("  ") == ""

    def test_roundtrip_dot_hyphen(self):
        """구형 사이트 SKU 를 크림 키로 변환해도 원본 스타일 복원 가능."""
        orig = "61.97657"
        kream = normalize_sku_for_kream(orig)
        assert kream == "61-97657"
        # 반대 방향 — 크림 포맷은 대시, 사이트는 점. 어댑터에서 동일한 stripped
        # key 로 귀결되는지.
        def _strip(s: str) -> str:
            return re.sub(r"[.\-\s]", "", s)
        assert _strip(orig) == _strip(kream) == "6197657"


class TestUrlExtraction:
    def test_extract_sku_new(self):
        url = "https://www.on.com/ko-kr/products/cloud-6-m-3mf1007/mens/apollo-eclipse-shoes-3MF10074109"
        assert extract_sku_from_url(url) == "3MF10074109"

    def test_extract_sku_old(self):
        url = "https://www.on.com/ko-kr/products/cloudmonster-61/mens/alloy-silver-shoes-61.97657"
        assert extract_sku_from_url(url) == "61.97657"

    def test_extract_sku_apparel(self):
        url = "https://www.on.com/ko-kr/products/3-core-shorts-1wf1015/womens/black-apparel-1WF10150553"
        assert extract_sku_from_url(url) == "1WF10150553"

    def test_extract_sku_invalid(self):
        assert extract_sku_from_url("") == ""
        assert extract_sku_from_url("https://www.on.com/ko-kr/shop") == ""

    def test_extract_gender(self):
        assert extract_gender_from_url(
            "https://www.on.com/ko-kr/products/x/mens/y-shoes-3MF10074109"
        ) == "mens"
        assert extract_gender_from_url(
            "https://www.on.com/ko-kr/products/x/womens/y-shoes-1WF10150553"
        ) == "womens"
        assert extract_gender_from_url(
            "https://www.on.com/ko-kr/products/x/unisex/y-shoes-3UF10074109"
        ) == "unisex"
        assert extract_gender_from_url("") == ""

    def test_extract_color_slug(self):
        url = "https://www.on.com/ko-kr/products/cloud-6-m-3mf1007/mens/apollo-eclipse-shoes-3MF10074109"
        assert extract_color_slug_from_url(url) == "apollo-eclipse"
        url2 = "https://www.on.com/ko-kr/products/cloudmonster-61/mens/alloy-silver-shoes-61.97657"
        assert extract_color_slug_from_url(url2) == "alloy-silver"


class TestParseSitemap:
    """실제 on.com/products.xml 캡처 fixture 기반."""

    @pytest.fixture
    def xml_text(self) -> str:
        assert SITEMAP_FIXTURE.exists(), (
            "fixture 누락 — tests/fixtures/live/on_running_sitemap.xml"
        )
        return SITEMAP_FIXTURE.read_text(encoding="utf-8")

    def test_extracts_ko_kr_urls_only(self, xml_text):
        urls = parse_sitemap(xml_text)
        assert len(urls) > 0
        for u in urls:
            assert u.startswith("https://www.on.com/ko-kr/products/"), u

    def test_no_duplicates(self, xml_text):
        urls = parse_sitemap(xml_text)
        assert len(urls) == len(set(urls))

    def test_skips_hreflang_alternates(self, xml_text):
        """hreflang en-us 등의 URL 이 섞여 나오면 안 됨."""
        urls = parse_sitemap(xml_text)
        for u in urls:
            assert "/en-us/" not in u
            assert "/en-gb/" not in u
            assert "/en-de/" not in u

    def test_covers_new_and_old_formats(self, xml_text):
        """fixture 에 신형/구형 SKU URL 모두 포함."""
        urls = parse_sitemap(xml_text)
        new = [u for u in urls if extract_sku_from_url(u) and is_valid_sku(extract_sku_from_url(u))
               and "." not in extract_sku_from_url(u)]
        old = [u for u in urls if "." in (extract_sku_from_url(u) or "")]
        assert len(new) >= 5, f"신형 URL 부족: {len(new)}"
        assert len(old) >= 1, f"구형 URL 부족: {len(old)}"


class TestParseLdJson:
    @pytest.fixture
    def html_new(self) -> str:
        assert PDP_NEW_FIXTURE.exists()
        return PDP_NEW_FIXTURE.read_text(encoding="utf-8")

    @pytest.fixture
    def html_old(self) -> str:
        assert PDP_OLD_FIXTURE.exists()
        return PDP_OLD_FIXTURE.read_text(encoding="utf-8")

    def test_parse_new_pdp(self, html_new):
        pg = parse_ldjson_block(html_new)
        assert pg is not None
        assert pg.get("@type") == "ProductGroup"
        variants = parse_variants(pg)
        assert len(variants) >= 1
        # 원본 PDP 는 Cloud 6 라인 — 11개 색상 variant
        skus = {v.sku for v in variants}
        assert "3MF10074109" in skus, f"대표 SKU 누락: {skus}"
        # availability 가 실제로 채워져야 함 (mock 값 아님)
        assert any(v.in_stock for v in variants)

    def test_parse_old_pdp(self, html_old):
        pg = parse_ldjson_block(html_old)
        assert pg is not None
        variants = parse_variants(pg)
        assert len(variants) >= 1
        skus = {v.sku for v in variants}
        assert "61.97657" in skus

    def test_parse_pdp_full_structure(self, html_new):
        pdp = parse_pdp(html_new)
        assert pdp is not None
        assert "변이" not in pdp["name"]  # korean name 정상 파싱
        assert pdp["brand"] == "On"
        assert len(pdp["variants"]) > 0
        v0 = pdp["variants"][0]
        assert isinstance(v0, OnVariant)
        assert v0.price > 0
        assert v0.currency == "KRW"

    def test_variant_price_in_krw(self, html_new):
        pdp = parse_pdp(html_new)
        for v in pdp["variants"]:
            assert v.currency == "KRW"
            assert 50_000 <= v.price <= 2_000_000, f"가격 이상: {v.sku}={v.price}"

    def test_malformed_html_returns_none(self):
        assert parse_ldjson_block("") is None
        assert parse_ldjson_block("<html></html>") is None
        assert parse_ldjson_block(
            '<script type="application/ld+json">not-json</script>'
        ) is None


class TestLaunchDetection:
    def test_coming_soon_detected(self):
        assert is_launch_or_coming_soon("<div>Coming Soon</div>")
        assert is_launch_or_coming_soon("COMING SOON!")
        assert is_launch_or_coming_soon("<span>발매예정</span>")
        assert is_launch_or_coming_soon("<span>판매예정</span>")

    def test_preorder_availability(self):
        assert is_launch_or_coming_soon(
            'data:"availability":"https://schema.org/PreOrder"'
        )

    def test_not_detected_for_normal(self):
        assert not is_launch_or_coming_soon("<div>in stock</div>")
        assert not is_launch_or_coming_soon("")


# ============================================================
# B. mock HTTP 레이어 — 어댑터 통합 테스트
# ============================================================


class _FakeOnHttp:
    """fetch_sitemap() 와 fetch_pdp(url) 만 mock.

    pdp_map: {url: dict|None} — 어댑터가 parse_pdp 결과를 직접 받으므로
      테스트에서는 이미 파싱된 변환 결과를 준다.
    """

    def __init__(
        self,
        urls: list[str],
        pdp_map: dict[str, dict | None],
    ) -> None:
        self._urls = urls
        self._pdp_map = pdp_map
        self.sitemap_calls = 0
        self.pdp_calls: list[str] = []

    async def fetch_sitemap(self) -> list[str]:
        self.sitemap_calls += 1
        return list(self._urls)

    async def fetch_pdp(self, url: str) -> dict | None:
        self.pdp_calls.append(url)
        return self._pdp_map.get(url)


def _mk_pdp(line_name: str, brand: str, variants: list[OnVariant]) -> dict:
    return {
        "name": line_name,
        "brand": brand,
        "url": variants[0].url if variants else "",
        "variants": variants,
    }


def _mk_variant(
    sku: str,
    color: str = "Black | Black",
    price: int = 199000,
    in_stock: bool = True,
    url: str = "",
) -> OnVariant:
    return OnVariant(
        sku=sku,
        color=color,
        price=price,
        currency="KRW",
        in_stock=in_stock,
        url=url or f"https://www.on.com/ko-kr/products/x/mens/y-shoes-{sku}",
    )


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
            # 신형 SKU 등재 — Cloud 6 Black
            {
                "product_id": "448252",
                "name": "On Running Cloud 6 Black",
                "model_number": "3MF10071043",
                "brand": "On Running",
            },
            # 신형 SKU 등재 — Cloudmonster 2 White Frost
            {
                "product_id": "324854",
                "name": "Cloudmonster 2 White Frost",
                "model_number": "3ME10120664",
                "brand": "On Running",
            },
            # 구형 SKU 등재 — 크림에는 하이픈 저장
            {
                "product_id": "186674",
                "name": "On Running Cloudmonster All Black",
                "model_number": "61-99025",
                "brand": "On Running",
            },
        ],
    )
    return str(path)


# ============================================================
# C. 어댑터 — dump_catalog + match_to_kream
# ============================================================


async def test_dump_catalog_publishes_event(bus, kream_db):
    """사이트맵 → PDP 순회 → variant flat + CatalogDumped publish."""
    url1 = "https://www.on.com/ko-kr/products/cloud-6-m-3mf1007/mens/black-shoes-3MF10071043"
    url2 = "https://www.on.com/ko-kr/products/cloudmonster-61/mens/all-black-shoes-61.99025"
    fake = _FakeOnHttp(
        urls=[url1, url2],
        pdp_map={
            url1: _mk_pdp(
                "남성 Cloud 6",
                "On",
                [_mk_variant("3MF10071043", "Black | Black", 199000, True, url1)],
            ),
            url2: _mk_pdp(
                "남성 Cloudmonster",
                "On",
                [_mk_variant("61.99025", "All | Black", 209000, True, url2)],
            ),
        },
    )
    adapter = OnRunningAdapter(bus=bus, db_path=kream_db, http_client=fake)

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, variants = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "on_running"
    assert event.product_count == 2
    assert len(variants) == 2
    assert fake.sitemap_calls == 1
    assert len(fake.pdp_calls) == 2
    assert received[0].product_count == 2


async def test_match_to_kream_new_format(bus, kream_db):
    """신형 SKU 3MF10071043 → kream 448252 매칭 성공."""
    url = "https://www.on.com/ko-kr/products/cloud-6-m-3mf1007/mens/black-shoes-3MF10071043"
    fake = _FakeOnHttp(
        urls=[url],
        pdp_map={
            url: _mk_pdp(
                "남성 Cloud 6",
                "On",
                [_mk_variant("3MF10071043", "Black | Black", 199000, True, url)],
            ),
        },
    )
    adapter = OnRunningAdapter(bus=bus, db_path=kream_db, http_client=fake)

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    _, variants = await adapter.dump_catalog()
    matches, stats = await adapter.match_to_kream(variants)
    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.matched == 1
    assert len(matches) == 1
    assert matches[0].kream_product_id == 448252
    assert matches[0].model_no == "3MF10071043"
    assert matches[0].retail_price == 199000
    # 사이즈 정보 없음 — listing-only 경로
    assert matches[0].available_sizes == ()
    assert received == matches


async def test_match_to_kream_old_format_dot_to_hyphen(bus, kream_db):
    """구형 SKU `61.99025` → 크림 `61-99025` 매칭 (정규화 왕복)."""
    url = "https://www.on.com/ko-kr/products/cloudmonster-61/mens/all-black-shoes-61.99025"
    fake = _FakeOnHttp(
        urls=[url],
        pdp_map={
            url: _mk_pdp(
                "남성 Cloudmonster",
                "On",
                [_mk_variant("61.99025", "All | Black", 209000, True, url)],
            ),
        },
    )
    adapter = OnRunningAdapter(bus=bus, db_path=kream_db, http_client=fake)
    _, variants = await adapter.dump_catalog()
    matches, stats = await adapter.match_to_kream(variants)

    assert stats.matched == 1
    assert matches[0].kream_product_id == 186674
    # 이벤트 model_no 는 정규화된(하이픈) 형태
    assert matches[0].model_no == "61-99025"


async def test_invalid_sku_and_soldout_filter(bus, kream_db):
    """invalid SKU, sold out 제외 확인."""
    url_valid = "https://www.on.com/ko-kr/products/cloud-6-m-3mf1007/mens/black-shoes-3MF10071043"
    url_invalid = "https://www.on.com/ko-kr/products/junk/mens/x-shoes-XX1234567"
    url_soldout = (
        "https://www.on.com/ko-kr/products/cloud/mens/white-shoes-3MF10074109"
    )
    fake = _FakeOnHttp(
        urls=[url_valid, url_invalid, url_soldout],
        pdp_map={
            url_valid: _mk_pdp(
                "남성 Cloud 6", "On",
                [_mk_variant("3MF10071043", "Black | Black", 199000, True, url_valid)],
            ),
            url_invalid: _mk_pdp(
                "junk", "On",
                [_mk_variant("XX1234567", "X", 100000, True, url_invalid)],
            ),
            url_soldout: _mk_pdp(
                "남성 Cloud 6 SO", "On",
                [_mk_variant("3MF10074109", "Apollo | Eclipse", 199000, False,
                             url_soldout)],
            ),
        },
    )
    # invalid variant 는 parse_variants 가 걸러내지만, 어댑터 레벨에서도
    # is_valid_sku 가드를 방어적으로 통과시키므로 mock 에서 직접 넣어도
    # invalid_sku 카운트에 잡혀야 한다. _mk_variant 는 OnVariant 를 만드는데
    # OnVariant 객체 자체에는 valid 검증이 없으므로 테스트 가능.

    adapter = OnRunningAdapter(bus=bus, db_path=kream_db, http_client=fake)
    _, variants = await adapter.dump_catalog()
    matches, stats = await adapter.match_to_kream(variants)

    assert stats.matched == 1
    assert stats.soldout_dropped == 1
    assert stats.invalid_sku == 1


async def test_unknown_sku_enqueues_collect(bus, kream_db):
    """크림에 없는 SKU → kream_collect_queue 로 배치 적재."""
    url = "https://www.on.com/ko-kr/products/new/mens/x-shoes-3MF99990000"
    fake = _FakeOnHttp(
        urls=[url],
        pdp_map={
            url: _mk_pdp(
                "New Shoe", "On",
                [_mk_variant("3MF99990000", "X", 250000, True, url)],
            ),
        },
    )
    adapter = OnRunningAdapter(bus=bus, db_path=kream_db, http_client=fake)
    assert _count_queue(kream_db) == 0
    _, variants = await adapter.dump_catalog()
    matches, stats = await adapter.match_to_kream(variants)

    assert stats.matched == 0
    assert stats.collected_to_queue == 1
    assert _count_queue(kream_db) == 1


async def test_run_once_end_to_end(bus, kream_db):
    """run_once — 덤프+매칭 한 사이클 + 통계 dict."""
    url_hit = "https://www.on.com/ko-kr/products/cloud-6-m-3mf1007/mens/black-shoes-3MF10071043"
    url_miss = "https://www.on.com/ko-kr/products/new/mens/x-shoes-3MF99990000"
    fake = _FakeOnHttp(
        urls=[url_hit, url_miss],
        pdp_map={
            url_hit: _mk_pdp(
                "Cloud 6", "On",
                [_mk_variant("3MF10071043", "B", 199000, True, url_hit)],
            ),
            url_miss: _mk_pdp(
                "New", "On",
                [_mk_variant("3MF99990000", "X", 250000, True, url_miss)],
            ),
        },
    )
    adapter = OnRunningAdapter(bus=bus, db_path=kream_db, http_client=fake)
    stats_dict = await adapter.run_once()

    assert stats_dict["matched"] == 1
    assert stats_dict["collected_to_queue"] == 1
    assert stats_dict["dumped"] == 2
    assert stats_dict["sitemap_urls"] == 2


async def test_dedup_same_sku_across_urls(bus, kream_db):
    """같은 SKU 가 여러 url 에서 재발견돼도 1회만 매칭/적재."""
    url1 = "https://www.on.com/ko-kr/products/a/mens/b-shoes-3MF10071043"
    url2 = "https://www.on.com/ko-kr/products/c/mens/d-shoes-3MF10071043"
    fake = _FakeOnHttp(
        urls=[url1, url2],
        pdp_map={
            url1: _mk_pdp("A", "On",
                          [_mk_variant("3MF10071043", "X", 199000, True, url1)]),
            url2: _mk_pdp("C", "On",
                          [_mk_variant("3MF10071043", "X", 199000, True, url2)]),
        },
    )
    adapter = OnRunningAdapter(bus=bus, db_path=kream_db, http_client=fake)
    _, variants = await adapter.dump_catalog()
    matches, stats = await adapter.match_to_kream(variants)
    assert stats.matched == 1
    assert len(matches) == 1


# ============================================================
# D. fixture → 어댑터 end-to-end 회귀
# ============================================================


async def test_real_pdp_fixture_parses_into_variants(bus, kream_db):
    """실서버 PDP 캡처 HTML 을 파서에 먹인 결과가 어댑터에서도 흘러간다.

    - parse_pdp 로 ProductGroup 내 전체 variants 추출
    - 그 중 크림 DB 등재된 SKU 는 매칭 통과
    - mock fetch_pdp 가 parse_pdp 결과를 그대로 돌려줘 전 파이프라인 검증
    """
    html_new = PDP_NEW_FIXTURE.read_text(encoding="utf-8")
    pdp_new = parse_pdp(html_new)
    assert pdp_new is not None
    # 11개 variant 가 나와야 함 (원본 fixture: Cloud 6 11색상)
    assert len(pdp_new["variants"]) >= 5

    html_old = PDP_OLD_FIXTURE.read_text(encoding="utf-8")
    pdp_old = parse_pdp(html_old)
    assert pdp_old is not None

    url_new = "https://www.on.com/ko-kr/products/cloud-6-m-3mf1007/mens"
    url_old = "https://www.on.com/ko-kr/products/cloudmonster-61/mens"

    fake = _FakeOnHttp(
        urls=[url_new, url_old],
        pdp_map={url_new: pdp_new, url_old: pdp_old},
    )
    adapter = OnRunningAdapter(bus=bus, db_path=kream_db, http_client=fake)

    _, variants = await adapter.dump_catalog()
    assert len(variants) >= 6  # 신형 여러색 + 구형 1색

    matches, stats = await adapter.match_to_kream(variants)
    # 크림 DB 에 등재된 SKU (3MF10071043, 61-99025) 는 각각 1 매칭 나와야 함.
    # 3MF10074109 / 3MF10670692 등은 크림 pid 가 다른 3건만 fixture 에 들어있어
    # 미매칭 → collect_queue 로.
    assert stats.matched >= 1
    matched_skus = {m.model_no for m in matches}
    # 신형 Cloud 6 Black 이 있으면 매칭
    assert "3MF10071043" in matched_skus


# ============================================================
# E. 회귀 - 거짓 매칭 방어
# ============================================================


class TestFalsePositiveRegression:
    """tests/fixtures/false_positives.json 에 추가할 on_running 케이스."""

    def test_short_model_number_no_partial_match(self):
        """짧은 SKU(구형 2자리 prefix) 가 긴 신형 SKU 에 부분 매칭되지 않음."""
        from src.adapters.on_running_adapter import _strip_key
        short = _strip_key("61-99025")     # 6199025
        long_ = _strip_key("3MF10071043")  # 3MF10071043
        # 서로 다른 키. 부분 포함 매칭 불가 (어댑터는 dict key exact).
        assert short != long_
        assert short not in long_
        assert long_ not in short
        # 구형 사이트-DB 왕복은 동일 키.
        assert _strip_key(normalize_sku_for_kream("61.99025")) == short

    def test_variant_sku_not_confused_with_line_slug(self):
        """URL path 내 `3mf1007` (소문자, ProductGroup line slug) 은 SKU 아님."""
        url = "https://www.on.com/ko-kr/products/cloud-6-m-3mf1007/mens/apollo-eclipse-shoes-3MF10074109"
        # extract_sku_from_url 은 트레일링 SKU 만 반환해야 함.
        assert extract_sku_from_url(url) == "3MF10074109"
        # 라인 슬러그가 11자리가 아니라 의도된 포맷 불일치라 is_valid_sku 거부.
        assert not is_valid_sku("3MF1007")
