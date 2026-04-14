"""EQL 크롤러 단위 테스트 -- HTML 파싱 + mock 통합."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers import eql as eql_mod
from src.crawlers.eql import (
    EqlCrawler,
    _extract_model_from_name,
    _parse_detail_sizes,
    _parse_search_html,
)


# ---- 모델번호 추출 ----


class TestExtractModelFromName:
    def test_nike_hyphen(self):
        assert _extract_model_from_name("WMNS NIKE FIRST SIGHT NOIR HQ2409-001") == "HQ2409-001"

    def test_asics_hyphen(self):
        assert _extract_model_from_name("ASICS GEL-KAYANO 14 1203A537-110") == "1203A537-110"

    def test_adidas_no_hyphen(self):
        assert _extract_model_from_name("ADIDAS SAMBA OG IE4195") == "IE4195"

    def test_vans_long_code(self):
        assert _extract_model_from_name("VANS OLD SKOOL VN000BW5BKA") == "VN000BW5BKA"

    def test_no_model(self):
        assert _extract_model_from_name("일반 상품명") == ""

    def test_empty(self):
        assert _extract_model_from_name("") == ""

    def test_none_safe(self):
        assert _extract_model_from_name(None) == ""

    def test_multiple_takes_last(self):
        """여러 코드가 있으면 끝(마지막)것을 가져온다."""
        result = _extract_model_from_name("NIKE AIR MAX 97 DQ9131-001")
        assert result == "DQ9131-001"


# ---- 검색 HTML 파싱 ----


SEARCH_HTML_FIXTURE = """
<ul class="product_list">
<li class="product_item ">
  <a href="/product/GM0026032747213/detail"
     godNo="GM0026032747213"
     godNm="WMNS NIKE FIRST SIGHT NOIR HQ2409-001"
     brndNm="NIKE"
     eqlOtltYn="N">
    <div class="price_wrap">
      <span class="discount">51%</span>
      <span class="current">149,000</span>
    </div>
  </a>
  <button lastSalePrc="149000" brndNm="NIKE"></button>
</li>
<li class="product_item is_soldout">
  <a href="/product/GM0026032747999/detail"
     godNo="GM0026032747999"
     godNm="ADIDAS SAMBA OG IE4195"
     brndNm="ADIDAS"
     eqlOtltYn="N">
    <div class="price_wrap">
      <span class="current">139,000</span>
    </div>
  </a>
  <button lastSalePrc="139000" brndNm="ADIDAS"></button>
</li>
</ul>
"""


class TestParseSearchHtml:
    def test_parse_two_products(self):
        results = _parse_search_html(SEARCH_HTML_FIXTURE)
        assert len(results) == 2

    def test_first_product_fields(self):
        results = _parse_search_html(SEARCH_HTML_FIXTURE)
        first = results[0]
        assert first["product_id"] == "GM0026032747213"
        assert first["name"] == "WMNS NIKE FIRST SIGHT NOIR HQ2409-001"
        assert first["brand"] == "NIKE"
        assert first["model_number"] == "HQ2409-001"
        assert first["is_sold_out"] is False
        assert first["url"].endswith("/product/GM0026032747213/detail")

    def test_soldout_flag(self):
        results = _parse_search_html(SEARCH_HTML_FIXTURE)
        second = results[1]
        assert second["is_sold_out"] is True
        assert second["brand"] == "ADIDAS"
        assert second["model_number"] == "IE4195"

    def test_price_extracted(self):
        results = _parse_search_html(SEARCH_HTML_FIXTURE)
        assert results[0]["price"] == 149000
        assert results[1]["price"] == 139000

    def test_empty_html(self):
        assert _parse_search_html("") == []
        assert _parse_search_html("<html></html>") == []


# ---- 상세 사이즈 파싱 ----


DETAIL_HTML_FIXTURE = """
<input type="hidden" id="lastSalePrc" value="149000"/>
<input type="hidden" id="godNm" value="WMNS NIKE FIRST SIGHT NOIR HQ2409-001">
<input type="hidden" id="brndNm" value="NIKE"/>

<input name="sizeItmNo" id="sizeItmNoIT001" value="IT001"
       onlineUsefulInvQty="4"/>
<input name="sizeItmNm" id="sizeItmNm240" value="240"/>

<input name="sizeItmNo" id="sizeItmNoIT002" value="IT002"
       onlineUsefulInvQty="0"/>
<input name="sizeItmNm" id="sizeItmNm250" value="250"/>

<input name="sizeItmNo" id="sizeItmNoIT003" value="IT003"
       onlineUsefulInvQty="2"/>
<input name="sizeItmNm" id="sizeItmNm260" value="260"/>
"""


class TestParseDetailSizes:
    def test_sizes_with_stock(self):
        rows = _parse_detail_sizes(DETAIL_HTML_FIXTURE)
        assert len(rows) == 3

    def test_in_stock_flag(self):
        rows = _parse_detail_sizes(DETAIL_HTML_FIXTURE)
        assert rows[0]["size"] == "240"
        assert rows[0]["in_stock"] is True
        assert rows[0]["qty"] == 4

        assert rows[1]["size"] == "250"
        assert rows[1]["in_stock"] is False
        assert rows[1]["qty"] == 0

        assert rows[2]["size"] == "260"
        assert rows[2]["in_stock"] is True
        assert rows[2]["qty"] == 2

    def test_empty_html(self):
        assert _parse_detail_sizes("") == []


# ---- httpx mock 통합 ----


def _make_mock_resp(text: str, status: int = 200):
    resp = AsyncMock()
    resp.status_code = status
    resp.text = text
    return resp


@asynccontextmanager
async def _noop_acquire():
    yield


class TestSearchProductsMocked:
    @pytest.mark.asyncio
    async def test_search_returns_parsed_results(self):
        crawler = EqlCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(SEARCH_HTML_FIXTURE)
            gc.return_value = client
            results = await crawler.search_products("HQ2409-001")

        assert len(results) == 2
        assert results[0]["product_id"] == "GM0026032747213"
        assert results[0]["model_number"] == "HQ2409-001"

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self):
        crawler = EqlCrawler()
        results = await crawler.search_products("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        crawler = EqlCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp("", status=500)
            gc.return_value = client
            results = await crawler.search_products("test")

        assert results == []


class TestGetProductDetailMocked:
    @pytest.mark.asyncio
    async def test_full_detail(self):
        crawler = EqlCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(DETAIL_HTML_FIXTURE)
            gc.return_value = client
            product = await crawler.get_product_detail("GM0026032747213")

        assert product is not None
        assert product.source == "eql"
        assert product.name == "WMNS NIKE FIRST SIGHT NOIR HQ2409-001"
        assert product.model_number == "HQ2409-001"
        assert product.brand == "NIKE"
        # 재고 있는 사이즈만: 240, 260 (250은 qty=0)
        assert len(product.sizes) == 2
        assert product.sizes[0].size == "240"
        assert product.sizes[0].price == 149000
        assert product.sizes[1].size == "260"
        assert product.sizes[1].in_stock is True

    @pytest.mark.asyncio
    async def test_empty_product_id(self):
        crawler = EqlCrawler()
        result = await crawler.get_product_detail("")
        assert result is None

    @pytest.mark.asyncio
    async def test_http_404_returns_none(self):
        crawler = EqlCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp("", status=404)
            gc.return_value = client
            product = await crawler.get_product_detail("INVALID")

        assert product is None


class TestRegistration:
    def test_eql_registered(self):
        from src.crawlers.registry import RETAIL_CRAWLERS, register

        register("eql", eql_mod.eql_crawler, "EQL")
        assert "eql" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["eql"]["label"] == "EQL"
