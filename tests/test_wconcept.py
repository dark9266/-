"""W컨셉 크롤러 단위 테스트 -- JSON 검색 + HTML 상세 파싱 + mock 통합."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers import wconcept as wconcept_mod
from src.crawlers.wconcept import (
    WconceptCrawler,
    _extract_model_number,
    _parse_detail_html,
    _parse_search_response,
)


# ---- 모델번호 추출 ----


class TestExtractModelNumber:
    def test_bracket_pattern(self):
        assert _extract_model_number("[IB5824-001] W NIKE AIR SUPERFLY") == "IB5824-001"

    def test_hyphen_at_end(self):
        assert _extract_model_number("NIKE AIR FORCE 1 CW2288-111") == "CW2288-111"

    def test_asics_hyphen(self):
        assert _extract_model_number("ASICS GEL-KAYANO 14 1203A537-110") == "1203A537-110"

    def test_no_hyphen(self):
        assert _extract_model_number("ADIDAS SAMBA OG IE4195") == "IE4195"

    def test_vans_long(self):
        assert _extract_model_number("VANS OLD SKOOL VN000BW5BKA") == "VN000BW5BKA"

    def test_no_model(self):
        assert _extract_model_number("일반 상품명") == ""

    def test_empty(self):
        assert _extract_model_number("") == ""

    def test_none_safe(self):
        assert _extract_model_number(None) == ""

    def test_bracket_takes_priority(self):
        assert _extract_model_number("[AB1234-001] NIKE SOMETHING CD5678-002") == "AB1234-001"


# ---- 검색 API 응답 파싱 ----


SEARCH_RESPONSE_FIXTURE = {
    "data": {
        "product": {
            "items": [
                {
                    "itemCd": "50001234",
                    "itemName": "[CW2288-111] NIKE AIR FORCE 1 07",
                    "brandName": "NIKE",
                    "salePrice": 139000,
                    "originalPrice": 169000,
                    "mainImageUrl": "https://img.wconcept.co.kr/test.jpg",
                    "soldOutYn": "N",
                },
                {
                    "itemCd": "50005678",
                    "itemName": "ADIDAS SAMBA OG IE4195",
                    "brandName": "ADIDAS",
                    "salePrice": 119000,
                    "originalPrice": 139000,
                    "mainImageUrl": "",
                    "soldOutYn": "Y",
                },
            ]
        }
    }
}


class TestParseSearchResponse:
    def test_parse_two_products(self):
        results = _parse_search_response(SEARCH_RESPONSE_FIXTURE)
        assert len(results) == 2

    def test_first_product_fields(self):
        results = _parse_search_response(SEARCH_RESPONSE_FIXTURE)
        first = results[0]
        assert first["product_id"] == "50001234"
        assert first["name"] == "[CW2288-111] NIKE AIR FORCE 1 07"
        assert first["brand"] == "NIKE"
        assert first["model_number"] == "CW2288-111"
        assert first["price"] == 139000
        assert first["is_sold_out"] is False

    def test_soldout_flag(self):
        results = _parse_search_response(SEARCH_RESPONSE_FIXTURE)
        second = results[1]
        assert second["is_sold_out"] is True
        assert second["model_number"] == "IE4195"

    def test_empty_data(self):
        assert _parse_search_response({}) == []
        assert _parse_search_response({"data": {}}) == []

    def test_alt_items_path(self):
        alt = {"data": {"items": [{"itemCd": "99", "itemName": "TEST", "brandName": "X"}]}}
        results = _parse_search_response(alt)
        assert len(results) == 1
        assert results[0]["product_id"] == "99"


# ---- 상세 HTML 파싱 ----


DETAIL_HTML_FIXTURE = """
<html>
<script>
var brazeJson = {"itemName":"[IB5824-001] W NIKE AIR SUPERFLY","brandName":"NIKE"};
var statusCd = '01';
var skuqty = [35,27,847,545,0,0];
</script>
<input type="hidden" name="saleprice" value="90300"/>
<input type="hidden" name="originalPrice" value="129000"/>
<select>
  <option value="opt1" optionvalue="220">220</option>
  <option value="opt2" optionvalue="225">225</option>
  <option value="opt3" optionvalue="230">230</option>
  <option value="opt4" optionvalue="235">235</option>
  <option value="opt5" optionvalue="240">240</option>
  <option value="opt6" optionvalue="245">245</option>
</select>
</html>
"""


class TestParseDetailHtml:
    def test_name_brand(self):
        result = _parse_detail_html(DETAIL_HTML_FIXTURE)
        assert result["name"] == "[IB5824-001] W NIKE AIR SUPERFLY"
        assert result["brand"] == "NIKE"

    def test_model_number(self):
        result = _parse_detail_html(DETAIL_HTML_FIXTURE)
        assert result["model_number"] == "IB5824-001"

    def test_price(self):
        result = _parse_detail_html(DETAIL_HTML_FIXTURE)
        assert result["price"] == 90300
        assert result["original_price"] == 129000

    def test_not_sold_out(self):
        result = _parse_detail_html(DETAIL_HTML_FIXTURE)
        assert result["is_sold_out"] is False

    def test_sold_out_status(self):
        html = DETAIL_HTML_FIXTURE.replace("statusCd = '01'", "statusCd = '04'")
        result = _parse_detail_html(html)
        assert result["is_sold_out"] is True

    def test_sizes_count(self):
        result = _parse_detail_html(DETAIL_HTML_FIXTURE)
        assert len(result["sizes"]) == 6

    def test_sizes_stock(self):
        result = _parse_detail_html(DETAIL_HTML_FIXTURE)
        # 220: 35 (in stock), 240: 0 (sold out)
        assert result["sizes"][0]["size"] == "220"
        assert result["sizes"][0]["in_stock"] is True
        assert result["sizes"][0]["qty"] == 35
        assert result["sizes"][4]["size"] == "240"
        assert result["sizes"][4]["in_stock"] is False
        assert result["sizes"][4]["qty"] == 0

    def test_empty_html(self):
        result = _parse_detail_html("")
        assert result["name"] == ""
        assert result["sizes"] == []


# ---- httpx mock 통합 ----


def _make_mock_resp(text: str = "", status: int = 200, json_data: dict | None = None):
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


@asynccontextmanager
async def _noop_acquire():
    yield


class TestSearchProductsMocked:
    @pytest.mark.asyncio
    async def test_search_returns_parsed_results(self):
        crawler = WconceptCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.post.return_value = _make_mock_resp(
                json_data=SEARCH_RESPONSE_FIXTURE
            )
            gc.return_value = client
            results = await crawler.search_products("CW2288-111")

        assert len(results) == 2
        assert results[0]["product_id"] == "50001234"
        assert results[0]["model_number"] == "CW2288-111"

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self):
        crawler = WconceptCrawler()
        results = await crawler.search_products("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        crawler = WconceptCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.post.return_value = _make_mock_resp(status=500)
            gc.return_value = client
            results = await crawler.search_products("test")

        assert results == []


class TestGetProductDetailMocked:
    @pytest.mark.asyncio
    async def test_full_detail(self):
        crawler = WconceptCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(text=DETAIL_HTML_FIXTURE)
            gc.return_value = client
            product = await crawler.get_product_detail("50001234")

        assert product is not None
        assert product.source == "wconcept"
        assert product.model_number == "IB5824-001"
        assert product.brand == "NIKE"
        # 재고 있는 사이즈만: 220, 225, 230, 235 (240, 245는 qty=0)
        assert len(product.sizes) == 4
        assert product.sizes[0].size == "220"
        assert product.sizes[0].price == 90300

    @pytest.mark.asyncio
    async def test_sold_out_returns_none(self):
        crawler = WconceptCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        html = DETAIL_HTML_FIXTURE.replace("statusCd = '01'", "statusCd = '04'")
        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(text=html)
            gc.return_value = client
            product = await crawler.get_product_detail("50001234")

        assert product is None

    @pytest.mark.asyncio
    async def test_discount_rate(self):
        crawler = WconceptCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(text=DETAIL_HTML_FIXTURE)
            gc.return_value = client
            product = await crawler.get_product_detail("50001234")

        assert product is not None
        # 90300 / 129000 = ~0.30 discount
        assert product.sizes[0].discount_type == "세일"
        assert product.sizes[0].discount_rate == pytest.approx(0.30, abs=0.01)

    @pytest.mark.asyncio
    async def test_empty_product_id(self):
        crawler = WconceptCrawler()
        result = await crawler.get_product_detail("")
        assert result is None

    @pytest.mark.asyncio
    async def test_http_404_returns_none(self):
        crawler = WconceptCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(status=404)
            gc.return_value = client
            product = await crawler.get_product_detail("INVALID")

        assert product is None


class TestRegistration:
    def test_wconcept_registered(self):
        from src.crawlers.registry import RETAIL_CRAWLERS, register

        register("wconcept", wconcept_mod.wconcept_crawler, "W컨셉")
        assert "wconcept" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["wconcept"]["label"] == "W컨셉"
