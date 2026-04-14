"""반스 크롤러 단위 테스트 — 가격 파싱 + SKU 파싱 + httpx mock."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers.vans import (
    VansCrawler,
    _parse_price,
    _parse_sku_data,
    _parse_size_options,
    _match_sku_to_size,
)


# --------------- _parse_price 테스트 ---------------


class TestParsePrice:
    def test_normal_with_unit(self):
        assert _parse_price("39,200 원") == 39200

    def test_normal_no_comma(self):
        assert _parse_price("69000") == 69000

    def test_with_comma_and_unit(self):
        assert _parse_price("169,000 원") == 169000

    def test_empty(self):
        assert _parse_price("") == 0

    def test_none_like(self):
        assert _parse_price("0") == 0

    def test_whitespace(self):
        assert _parse_price("  49,000 원  ") == 49000

    def test_no_digits(self):
        assert _parse_price("원") == 0


# --------------- _parse_sku_data 테스트 ---------------


class TestParseSkuData:
    def test_single_quote_format(self):
        html = """<div data-sku-data='[{"price":"39,200 원","retailPrice":"49,000 원","salePrice":"39,200 원","quantity":1,"inventoryType":"CHECK_QUANTITY","upc":"VN000CRYRUS104000M","skuId":194717,"locationSumQuantity":2,"selectedOptions":[1384,215]}]'></div>"""
        result = _parse_sku_data(html)
        assert len(result) == 1
        assert result[0]["skuId"] == 194717
        assert result[0]["quantity"] == 1
        assert result[0]["selectedOptions"] == [1384, 215]

    def test_multiple_skus(self):
        html = """<div data-sku-data='[{"skuId":1,"quantity":2,"selectedOptions":[10,20]},{"skuId":2,"quantity":0,"selectedOptions":[10,21]}]'></div>"""
        result = _parse_sku_data(html)
        assert len(result) == 2
        assert result[0]["skuId"] == 1
        assert result[1]["quantity"] == 0

    def test_no_sku_data(self):
        html = "<div>no data here</div>"
        result = _parse_sku_data(html)
        assert result == []

    def test_double_quote_format(self):
        html = '<div data-sku-data="[{&quot;skuId&quot;:1}]"></div>'
        # JSON 내 HTML entity는 파싱 실패 → 빈 리스트
        result = _parse_sku_data(html)
        assert isinstance(result, list)

    def test_malformed_json(self):
        html = """<div data-sku-data='[{bad json}]'></div>"""
        result = _parse_sku_data(html)
        assert result == []


# --------------- _parse_size_options 테스트 ---------------


class TestParseSizeOptions:
    def test_option_tags(self):
        html = """
        <select>
            <option value="215">220</option>
            <option value="216">225</option>
            <option value="217">230</option>
        </select>
        """
        result = _parse_size_options(html)
        assert result == {215: "220", 216: "225", 217: "230"}

    def test_no_options(self):
        html = "<div>nothing</div>"
        result = _parse_size_options(html)
        assert result == {}

    def test_data_value_pattern(self):
        html = """
        <li data-value="300">250</li>
        <li data-value="301">255</li>
        """
        result = _parse_size_options(html)
        assert result == {300: "250", 301: "255"}


# --------------- _match_sku_to_size 테스트 ---------------


class TestMatchSkuToSize:
    def test_basic_match(self):
        sku_list = [
            {
                "price": "39,200 원",
                "retailPrice": "49,000 원",
                "salePrice": "39,200 원",
                "quantity": 1,
                "locationSumQuantity": 2,
                "selectedOptions": [1384, 215],
                "skuId": 100,
            }
        ]
        size_options = {215: "220", 216: "225"}
        result = _match_sku_to_size(sku_list, size_options)
        assert len(result) == 1
        assert result[0]["size"] == "220"
        assert result[0]["price"] == 39200
        assert result[0]["original_price"] == 49000
        assert result[0]["in_stock"] is True

    def test_out_of_stock(self):
        sku_list = [
            {
                "price": "69,000 원",
                "retailPrice": "69,000 원",
                "salePrice": "",
                "quantity": 0,
                "locationSumQuantity": 0,
                "selectedOptions": [1384, 215],
                "skuId": 200,
            }
        ]
        size_options = {215: "220"}
        result = _match_sku_to_size(sku_list, size_options)
        assert len(result) == 1
        assert result[0]["in_stock"] is False

    def test_no_size_match(self):
        sku_list = [
            {
                "price": "50,000 원",
                "retailPrice": "50,000 원",
                "salePrice": "",
                "quantity": 3,
                "locationSumQuantity": 3,
                "selectedOptions": [999, 888],
                "skuId": 300,
            }
        ]
        size_options = {215: "220"}
        result = _match_sku_to_size(sku_list, size_options)
        assert len(result) == 1
        assert result[0]["size"] == ""  # 매칭 실패


# --------------- httpx mock 헬퍼 ---------------


def _make_mock_resp(content, status: int = 200, is_json: bool = True):
    resp = AsyncMock()
    resp.status_code = status
    if is_json:
        resp.json = lambda: content
    else:
        resp.text = content
    return resp


@asynccontextmanager
async def _noop_acquire():
    yield


# --------------- search_products 테스트 ---------------


class TestSearchProducts:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        crawler = VansCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "productList": [
                {
                    "id": 42717,
                    "name": "클래식 슬립온 컬러 띠어리",
                    "model": "VN000D6YFRQ",
                    "modelGroup": "Classic Slip-On",
                    "url": "/PRODUCT/VN000D6YFRQ",
                    "retailPrice": {"amount": 69000, "currency": "KRW"},
                    "salePrice": None,
                    "active": True,
                    "soldOut": False,
                    "color": "핑크",
                    "productMedia": {
                        "url": "https://img.vans.com/VN000D6YFRQ-HERO.jpg"
                    },
                }
            ]
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("VN000D6YFRQ")

        assert len(results) == 1
        assert results[0]["product_id"] == "VN000D6YFRQ"
        assert results[0]["model_number"] == "VN000D6YFRQ"
        assert results[0]["brand"] == "Vans"
        assert results[0]["price"] == 69000
        assert results[0]["original_price"] == 69000
        assert results[0]["url"] == "https://www.vans.co.kr/PRODUCT/VN000D6YFRQ"
        assert results[0]["image_url"] == "https://img.vans.com/VN000D6YFRQ-HERO.jpg"
        assert results[0]["is_sold_out"] is False

    @pytest.mark.asyncio
    async def test_search_with_sale_price(self):
        crawler = VansCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "productList": [
                {
                    "id": 100,
                    "name": "세일 상품",
                    "model": "VN0A5KXXBKA",
                    "url": "/PRODUCT/VN0A5KXXBKA",
                    "retailPrice": {"amount": 89000},
                    "salePrice": {"amount": 62300},
                    "active": True,
                    "soldOut": False,
                    "productMedia": {"url": ""},
                }
            ]
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("VN0A5KXXBKA")

        assert len(results) == 1
        assert results[0]["price"] == 62300  # salePrice 우선
        assert results[0]["original_price"] == 89000

    @pytest.mark.asyncio
    async def test_search_filters_sold_out(self):
        crawler = VansCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "productList": [
                {
                    "id": 1,
                    "name": "재고 있음",
                    "model": "VN001",
                    "retailPrice": {"amount": 69000},
                    "salePrice": None,
                    "active": True,
                    "soldOut": False,
                    "productMedia": {"url": ""},
                },
                {
                    "id": 2,
                    "name": "품절",
                    "model": "VN002",
                    "retailPrice": {"amount": 69000},
                    "salePrice": None,
                    "active": True,
                    "soldOut": True,
                    "productMedia": {"url": ""},
                },
                {
                    "id": 3,
                    "name": "비활성",
                    "model": "VN003",
                    "retailPrice": {"amount": 69000},
                    "salePrice": None,
                    "active": False,
                    "soldOut": False,
                    "productMedia": {"url": ""},
                },
            ]
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("VN")

        assert len(results) == 1
        assert results[0]["model_number"] == "VN001"

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self):
        crawler = VansCrawler()
        results = await crawler.search_products("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        crawler = VansCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({}, status=429)
            gc.return_value = client
            results = await crawler.search_products("test")

        assert results == []


# --------------- get_product_detail 테스트 ---------------


SAMPLE_DETAIL_HTML = """<!DOCTYPE html>
<html>
<head>
<title>클래식 슬립온 - Vans</title>
<meta property="og:title" content="클래식 슬립온 컬러 띠어리">
<meta property="og:image" content="https://img.vans.com/VN000D6YFRQ.jpg">
</head>
<body>
<select name="size">
    <option value="215">220</option>
    <option value="216">225</option>
    <option value="217">230</option>
</select>
<div data-sku-data='[{"price":"39,200 원","retailPrice":"49,000 원","salePrice":"39,200 원","quantity":2,"inventoryType":"CHECK_QUANTITY","upc":"VN000D6YFRQ220","skuId":100,"locationSumQuantity":2,"selectedOptions":[1384,215]},{"price":"39,200 원","retailPrice":"49,000 원","salePrice":"39,200 원","quantity":0,"inventoryType":"CHECK_QUANTITY","upc":"VN000D6YFRQ225","skuId":101,"locationSumQuantity":0,"selectedOptions":[1384,216]},{"price":"39,200 원","retailPrice":"49,000 원","salePrice":"39,200 원","quantity":1,"inventoryType":"CHECK_QUANTITY","upc":"VN000D6YFRQ230","skuId":102,"locationSumQuantity":1,"selectedOptions":[1384,217]}]'></div>
</body>
</html>"""


class TestGetProductDetail:
    @pytest.mark.asyncio
    async def test_detail_returns_product(self):
        crawler = VansCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            resp = AsyncMock()
            resp.status_code = 200
            resp.text = SAMPLE_DETAIL_HTML
            client.get.return_value = resp
            gc.return_value = client
            product = await crawler.get_product_detail("VN000D6YFRQ")

        assert product is not None
        assert product.source == "vans"
        assert product.product_id == "VN000D6YFRQ"
        assert product.model_number == "VN000D6YFRQ"
        assert product.brand == "Vans"
        assert product.name == "클래식 슬립온 컬러 띠어리"
        assert product.image_url == "https://img.vans.com/VN000D6YFRQ.jpg"

        # 3개 SKU 중 quantity > 0인 2개만
        assert len(product.sizes) == 2
        assert product.sizes[0].size == "220"
        assert product.sizes[0].price == 39200
        assert product.sizes[0].original_price == 49000
        assert product.sizes[0].discount_rate > 0
        assert product.sizes[1].size == "230"

    @pytest.mark.asyncio
    async def test_detail_no_discount(self):
        crawler = VansCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        html = """<html>
<head><meta property="og:title" content="정가 상품"></head>
<body>
<select><option value="300">250</option></select>
<div data-sku-data='[{"price":"69,000 원","retailPrice":"69,000 원","salePrice":"","quantity":5,"locationSumQuantity":5,"selectedOptions":[100,300],"skuId":500}]'></div>
</body></html>"""

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            resp = AsyncMock()
            resp.status_code = 200
            resp.text = html
            client.get.return_value = resp
            gc.return_value = client
            product = await crawler.get_product_detail("VN0TEST")

        assert product is not None
        assert len(product.sizes) == 1
        assert product.sizes[0].price == 69000
        assert product.sizes[0].original_price == 69000
        assert product.sizes[0].discount_rate == 0.0
        assert product.sizes[0].discount_type == ""

    @pytest.mark.asyncio
    async def test_detail_empty_id(self):
        crawler = VansCrawler()
        result = await crawler.get_product_detail("")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_http_error(self):
        crawler = VansCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            resp = AsyncMock()
            resp.status_code = 404
            client.get.return_value = resp
            gc.return_value = client
            product = await crawler.get_product_detail("NOTEXIST")

        assert product is None

    @pytest.mark.asyncio
    async def test_detail_no_sku_data(self):
        crawler = VansCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        html = "<html><body>no sku data here</body></html>"

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            resp = AsyncMock()
            resp.status_code = 200
            resp.text = html
            client.get.return_value = resp
            gc.return_value = client
            product = await crawler.get_product_detail("VN0EMPTY")

        assert product is None


# --------------- 등록 테스트 ---------------


class TestRegistration:
    def test_vans_registered(self):
        from src.crawlers.registry import RETAIL_CRAWLERS, register
        from src.crawlers.vans import vans_crawler

        register("vans", vans_crawler, "반스")
        assert "vans" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["vans"]["label"] == "반스"
