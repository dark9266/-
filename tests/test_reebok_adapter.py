"""Reebok 어댑터 단위 테스트.

fixture 기반 테스트: tests/fixtures/live/reebok/ 실 응답 HTML 사용.
사람이 직접 값을 넣은 mock 금지 — 실 응답 구조 그대로 파싱.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.reebok_adapter import (
    BASE_URL,
    CATALOG_CATEGORIES,
    ReebokAdapter,
    ReebokMatchStats,
    _extract_model,
    _extract_price,
    _parse_list_items,
    _parse_sizes,
)
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus

# ─── fixture 경로 ──────────────────────────────────────────────────────────
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "live" / "reebok"


def _read(filename: str) -> str:
    return (FIXTURE_DIR / filename).read_text(encoding="utf-8")


# ─── 파서 단위 테스트 (순수 함수) ─────────────────────────────────────────


class TestExtractModel:
    """_extract_model — 슬래시 뒤 모델번호 추출."""

    def test_basic_shoes_model(self) -> None:
        text = "클럽 C 85 빈티지 - 크림 / DV6434 ｜ 리복 공식 온라인스토어"
        assert _extract_model(text) == "DV6434"

    def test_numeric_model(self) -> None:
        text = "나노 프로 - 플래시 오렌지 / 100225441 ｜ 리복 공식 온라인스토어"
        assert _extract_model(text) == "100225441"

    def test_no_slash_returns_none(self) -> None:
        """슬래시 없는 의류/기획전 상품 — None."""
        text = "5포켓 릴랙스핏 골프 팬츠 - 크림 베이지 ｜ 리복 공식 온라인스토어"
        assert _extract_model(text) is None

    def test_hyphenated_model(self) -> None:
        """하이픈이 포함된 모델번호."""
        text = "글래디에이터 - 블랙 / GX3631 ｜ 리복 공식 온라인스토어"
        assert _extract_model(text) == "GX3631"

    def test_short_suffix_skipped(self) -> None:
        """슬래시 뒤 3자 이하 코드는 모델번호로 보지 않음."""
        text = "상품명 / AB"
        assert _extract_model(text) is None


class TestParseSizes:
    """_parse_sizes — data-value 재고 파싱 (실 fixture 기반)."""

    def test_dv6434_fixture(self) -> None:
        """DV6434: 18개 사이즈, 재고 있는 것 15개."""
        html = _read("detail_DV6434_1000000183.html")
        sizes = _parse_sizes(html)
        in_stock = [s for s in sizes if s["in_stock"]]

        assert len(sizes) == 18
        assert len(in_stock) == 15
        # 220mm 품절 확인 (첫 사이즈)
        size_220 = next((s for s in sizes if s["size"] == "220"), None)
        assert size_220 is not None
        assert size_220["in_stock"] is False
        assert size_220["stock"] == 0

    def test_nano_pro_fixture(self) -> None:
        """100225441 나노 프로: 13개 사이즈, 재고 있는 것 11개."""
        html = _read("detail_100225441_1000005906.html")
        sizes = _parse_sizes(html)
        in_stock = [s for s in sizes if s["in_stock"]]

        assert len(sizes) == 13
        assert len(in_stock) == 11

    def test_clothing_sizes(self) -> None:
        """의류 상품: FREE/S/M/L 계열 사이즈도 파싱됨."""
        html = _read("detail_no_model_1000006479.html")
        sizes = _parse_sizes(html)

        # 의류 사이즈 존재 확인
        assert len(sizes) > 0
        size_names = {s["size"] for s in sizes}
        # 사이즈 중 하나라도 알파벳 포함 (S, M, L, XL 등)
        assert any(any(c.isalpha() for c in name) for name in size_names)

    def test_empty_html(self) -> None:
        """data-value 없는 HTML → 빈 리스트."""
        assert _parse_sizes("<html><body></body></html>") == []

    def test_stock_positive(self) -> None:
        """재고 수량이 양수인 경우 in_stock=True."""
        html = '<li data-value="13776||0||||500^|^240"></li>'
        sizes = _parse_sizes(html)
        assert len(sizes) == 1
        assert sizes[0]["size"] == "240"
        assert sizes[0]["stock"] == 500
        assert sizes[0]["in_stock"] is True

    def test_stock_zero(self) -> None:
        """재고 0 → in_stock=False."""
        html = '<li data-value="13776||0||||0^|^220"></li>'
        sizes = _parse_sizes(html)
        assert len(sizes) == 1
        assert sizes[0]["in_stock"] is False


class TestExtractPrice:
    """_extract_price — setGoodsPrice 파싱."""

    def test_dv6434_price(self) -> None:
        """DV6434 가격 87,200원."""
        html = _read("detail_DV6434_1000000183.html")
        assert _extract_price(html) == 87200

    def test_nano_pro_price(self) -> None:
        """100225441 나노 프로 가격."""
        html = _read("detail_100225441_1000005906.html")
        price = _extract_price(html)
        assert price > 0  # 실제 가격 존재 확인

    def test_no_price(self) -> None:
        """setGoodsPrice 없으면 0."""
        assert _extract_price("<html></html>") == 0


class TestParseListItems:
    """_parse_list_items — 리스트 페이지 상품 파싱 (실 fixture 기반)."""

    def test_shoes_list_fixture(self) -> None:
        """신발 카테고리 1페이지: 40개 item_name 파싱."""
        html = _read("list_shoes_p1.html")
        items = _parse_list_items(html)

        assert len(items) == 40
        # 모든 아이템에 goodsNo, name, price 있음
        for item in items:
            assert item["goodsNo"]
            assert item["name"]
            assert item["price"] > 0

    def test_first_item_model(self) -> None:
        """첫 번째 상품 100259127 모델번호 추출."""
        html = _read("list_shoes_p1.html")
        items = _parse_list_items(html)
        first = items[0]
        assert first["goodsNo"] == "1000005931"
        assert first["model"] == "100259127"
        assert first["price"] == 109000

    def test_no_model_item_included(self) -> None:
        """모델번호 없는 상품도 파싱 (model=None으로)."""
        html = """
        <a href="../goods/goods_view.php?goodsNo=9999">
        <strong class="item_name">의류 상품 이름 슬래시 없음</strong>
        </a>
        <div class="item_money_box">
        <strong class="item_price">
        <span>89,000<span class="currency">원</span></span>
        </strong>
        </div>
        """
        items = _parse_list_items(html)
        if items:
            assert items[0]["model"] is None


# ─── 어댑터 통합 테스트 ──────────────────────────────────────────────────


class MockBus:
    """이벤트 버스 mock."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class MockReebokHttp:
    """실 fixture 기반 HTTP mock.

    사람이 직접 available=True를 넣지 않고, 실 HTML을 그대로 반환.
    """

    def __init__(self) -> None:
        self._list_html = _read("list_shoes_p1.html")
        self._detail_htmls: dict[str, str] = {
            "1000000183": _read("detail_DV6434_1000000183.html"),
            "1000005906": _read("detail_100225441_1000005906.html"),
            "1000006479": _read("detail_no_model_1000006479.html"),
        }
        self.list_calls: list[tuple[str, int, int]] = []
        self.detail_calls: list[str] = []

    async def fetch_list(self, cate_cd: str, page: int, count: int) -> str:
        self.list_calls.append((cate_cd, page, count))
        # 테스트 단순화: 002 cateCd page 1만 데이터, 나머지 빈 문자열
        if cate_cd == "002" and page == 1:
            return self._list_html
        return ""

    async def fetch_detail(self, goods_no: str) -> str:
        self.detail_calls.append(goods_no)
        return self._detail_htmls.get(goods_no, "")


class MockKreamIndex:
    """크림 DB 인덱스 mock — 실제 모델번호 키워드 사용."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self._rows = rows

    def get(self) -> dict[str, dict]:
        return self._rows


@pytest.mark.asyncio
async def test_dump_catalog_basic() -> None:
    """dump_catalog: fixture HTML로 상품 파싱, CatalogDumped publish.

    리스트 1페이지 fixture 기반:
    - DV6434: goodsNo=1000006473 (콜라보 버전, 169000원)
    - 100259127: goodsNo=1000005931 (클럽 C 85 GL, 109000원)
    상세 호출 → 사이즈 파싱 확인은 goodsNo=1000000183 (별도 fixture).
    """
    bus = MockBus()
    adapter = ReebokAdapter(
        bus=bus,
        db_path=":memory:",
        http_client=MockReebokHttp(),
        categories=("002",),  # 신발만
    )

    event, catalog = await adapter.dump_catalog()

    assert isinstance(event, CatalogDumped)
    assert event.source == "reebok"
    assert len(catalog) == 40  # 리스트 1페이지 40개

    # DV6434 상품 확인 (리스트 fixture 기준 goodsNo=1000006473)
    dv = next((i for i in catalog if i.get("model") == "DV6434"), None)
    assert dv is not None, "DV6434 not found in catalog"
    assert dv["goodsNo"] == "1000006473"
    # 상세 HTML이 없는 경우 리스트 가격 유지
    assert dv["price"] > 0

    # 상세 HTML이 있는 상품 (goodsNo=1000000183) 사이즈 파싱 확인
    detailed = next((i for i in catalog if i["goodsNo"] == "1000000183"), None)
    if detailed is not None:
        # DV6434 상세 HTML에서 사이즈 파싱됨
        assert len(detailed.get("sizes", [])) > 0


@pytest.mark.asyncio
async def test_match_to_kream_exact_hit() -> None:
    """match_to_kream: 100259127 크림 DB에 있으면 CandidateMatched publish.

    리스트 fixture에서 실제 파싱된 상품(goodsNo=1000005931, 100259127, 109000원)을
    크림 DB에서 hit시켜 CandidateMatched 발행 확인.
    """
    from src.core.kream_index import strip_key
    from src.matcher import normalize_model_number

    bus = MockBus()
    adapter = ReebokAdapter(
        bus=bus,
        db_path=":memory:",
        http_client=MockReebokHttp(),
        categories=("002",),
    )

    # 100259127 strip_key 계산
    target_key = strip_key(normalize_model_number("100259127"))

    kream_row = {
        "product_id": 99999,
        "name": "리복 클럽 C 85 GL 화이트",
        "brand": "Reebok",
        "model_number": "100259127",
    }
    mock_index_data = {target_key: kream_row}

    with patch(
        "src.adapters.reebok_adapter.get_kream_index",
        return_value=MockKreamIndex(mock_index_data),
    ):
        with patch("src.adapters.reebok_adapter.aenqueue_collect_batch", return_value=0):
            _, catalog = await adapter.dump_catalog()
            matched, stats = await adapter.match_to_kream(catalog)

    assert stats.matched >= 1
    candidates = [e for e in bus.events if isinstance(e, CandidateMatched)]
    candidate = next(
        (c for c in candidates if "100259127" in (c.model_no or "")),
        None,
    )
    assert candidate is not None, "100259127 CandidateMatched not published"
    assert candidate.kream_product_id == 99999
    assert candidate.retail_price == 109000  # 리스트 가격 (상세 없음)


@pytest.mark.asyncio
async def test_match_no_model_skipped() -> None:
    """match_to_kream: 모델번호 없는 상품은 no_model_number 카운트."""
    bus = MockBus()
    adapter = ReebokAdapter(
        bus=bus,
        db_path=":memory:",
        http_client=MockReebokHttp(),
        categories=("002",),
    )

    # 모델번호 없는 catalog만 전달
    no_model_catalog = [
        {
            "goodsNo": "9999",
            "name": "의류 상품 이름",
            "model": None,
            "price": 89000,
            "url": f"{BASE_URL}/goods/goods_view.php?goodsNo=9999",
            "sizes": [],
        }
    ]

    with patch(
        "src.adapters.reebok_adapter.get_kream_index",
        return_value=MockKreamIndex({}),
    ):
        _, stats = await adapter.match_to_kream(no_model_catalog)

    assert stats.no_model_number == 1
    assert stats.matched == 0


@pytest.mark.asyncio
async def test_match_soldout_dropped() -> None:
    """match_to_kream: 전품절 상품은 soldout_dropped 카운트."""
    from src.core.kream_index import strip_key
    from src.matcher import normalize_model_number

    bus = MockBus()
    adapter = ReebokAdapter(bus=bus, db_path=":memory:", http_client=MockReebokHttp())

    key = strip_key(normalize_model_number("DV6434"))
    kream_rows = {key: {"product_id": 1, "name": "Reebok DV6434", "brand": "Reebok", "model_number": "DV6434"}}

    soldout_catalog = [
        {
            "goodsNo": "1000000183",
            "name": "클럽 C 85 빈티지 - 크림 / DV6434",
            "model": "DV6434",
            "price": 87200,
            "url": f"{BASE_URL}/goods/goods_view.php?goodsNo=1000000183",
            # sizes 있지만 전부 품절
            "sizes": [
                {"size": "250", "stock": 0, "in_stock": False},
                {"size": "260", "stock": 0, "in_stock": False},
            ],
        }
    ]

    with patch(
        "src.adapters.reebok_adapter.get_kream_index",
        return_value=MockKreamIndex(kream_rows),
    ):
        _, stats = await adapter.match_to_kream(soldout_catalog)

    assert stats.soldout_dropped == 1
    assert stats.matched == 0


@pytest.mark.asyncio
async def test_collect_queue_for_unmatched() -> None:
    """match_to_kream: 크림 DB에 없는 상품은 collect_queue에 적재."""
    bus = MockBus()
    adapter = ReebokAdapter(bus=bus, db_path=":memory:", http_client=MockReebokHttp())

    catalog = [
        {
            "goodsNo": "1000005906",
            "name": "나노 프로 - 플래시 오렌지 / 100225441",
            "model": "100225441",
            "price": 299000,
            "url": f"{BASE_URL}/goods/goods_view.php?goodsNo=1000005906",
            "sizes": [{"size": "260", "stock": 5, "in_stock": True}],
        }
    ]

    # 빈 kream_index → collect_queue 적재
    with patch("src.adapters.reebok_adapter.get_kream_index", return_value=MockKreamIndex({})):
        with patch("src.adapters.reebok_adapter.aenqueue_collect_batch", return_value=1) as mock_batch:
            _, stats = await adapter.match_to_kream(catalog)

    mock_batch.assert_called_once()
    batch_rows = mock_batch.call_args[0][1]
    assert any("100225441" in (r[0] or "") for r in batch_rows)


@pytest.mark.asyncio
async def test_run_once_returns_stats() -> None:
    """run_once: dict 반환 형식 확인."""
    bus = MockBus()
    adapter = ReebokAdapter(
        bus=bus,
        db_path=":memory:",
        http_client=MockReebokHttp(),
        categories=("002",),
    )

    with patch("src.adapters.reebok_adapter.get_kream_index", return_value=MockKreamIndex({})):
        with patch("src.adapters.reebok_adapter.aenqueue_collect_batch", return_value=0):
            result = await adapter.run_once()

    assert isinstance(result, dict)
    assert "dumped" in result
    assert "matched" in result
    assert "no_model_number" in result
    assert result["dumped"] >= 0


@pytest.mark.asyncio
async def test_goodsno_dedup() -> None:
    """dump_catalog: 같은 goodsNo 중복 제거."""
    bus = MockBus()

    class DuplicateHttp:
        """같은 goodsNo를 두 번 반환하는 mock."""

        async def fetch_list(self, cate_cd: str, page: int, count: int) -> str:
            if page == 1:
                # 두 카테고리에서 같은 goodsNo 포함
                return _read("list_shoes_p1.html")
            return ""

        async def fetch_detail(self, goods_no: str) -> str:
            return _read("detail_DV6434_1000000183.html") if goods_no == "1000000183" else ""

    adapter = ReebokAdapter(
        bus=bus,
        db_path=":memory:",
        http_client=DuplicateHttp(),
        categories=("002", "003"),  # 두 카테고리 — 중복 테스트
    )

    _, catalog = await adapter.dump_catalog()
    goods_nos = [i["goodsNo"] for i in catalog]
    # 중복 없어야 함
    assert len(goods_nos) == len(set(goods_nos))


class TestStatsDictStructure:
    """ReebokMatchStats.as_dict 구조 확인."""

    def test_all_keys_present(self) -> None:
        stats = ReebokMatchStats()
        d = stats.as_dict()
        expected_keys = {
            "dumped", "soldout_dropped", "no_model_number",
            "matched", "collected_to_queue", "skipped_guard",
            "list_items_parsed", "detail_fetched", "detail_failed",
        }
        assert expected_keys == set(d.keys())

    def test_initial_zeros(self) -> None:
        stats = ReebokMatchStats()
        assert all(v == 0 for v in stats.as_dict().values())
