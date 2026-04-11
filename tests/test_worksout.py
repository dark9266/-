"""웍스아웃 크롤러 단위 테스트 — 검색/상세 API mock + 필터링."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers.worksout import (
    WorksoutCrawler,
    _parse_sizes,
    _should_skip,
    _all_sold_out,
    _parse_search_product,
)


# --------------- 순수 파싱 함수 테스트 ---------------


class TestParseSizes:
    def test_normal_sizes(self):
        data = [
            {"sizeName": "240", "isSoldOut": False, "isLast": True},
            {"sizeName": "250", "isSoldOut": True, "isLast": False},
            {"sizeName": "260", "isSoldOut": False, "isLast": False},
        ]
        result = _parse_sizes(data)
        assert len(result) == 3
        assert result[0]["size"] == "240"
        assert result[0]["in_stock"] is True
        assert result[1]["size"] == "250"
        assert result[1]["in_stock"] is False
        assert result[2]["size"] == "260"
        assert result[2]["in_stock"] is True

    def test_empty_list(self):
        assert _parse_sizes([]) == []

    def test_none_input(self):
        assert _parse_sizes(None) == []

    def test_invalid_items_skipped(self):
        data = [
            {"sizeName": "240", "isSoldOut": False},
            {"sizeName": "", "isSoldOut": False},
            None,
            "invalid",
        ]
        result = _parse_sizes(data)
        assert len(result) == 1
        assert result[0]["size"] == "240"


class TestShouldSkip:
    def test_online_product(self):
        assert _should_skip({"onlyOffline": False, "isPreOrder": False}) is False

    def test_offline_only(self):
        assert _should_skip({"onlyOffline": True, "isPreOrder": False}) is True

    def test_preorder(self):
        assert _should_skip({"onlyOffline": False, "isPreOrder": True}) is True

    def test_both(self):
        assert _should_skip({"onlyOffline": True, "isPreOrder": True}) is True

    def test_missing_keys(self):
        assert _should_skip({}) is False


class TestAllSoldOut:
    def test_some_in_stock(self):
        sizes = [
            {"size": "240", "in_stock": False},
            {"size": "250", "in_stock": True},
        ]
        assert _all_sold_out(sizes) is False

    def test_all_out(self):
        sizes = [
            {"size": "240", "in_stock": False},
            {"size": "250", "in_stock": False},
        ]
        assert _all_sold_out(sizes) is True

    def test_empty(self):
        assert _all_sold_out([]) is True


class TestParseSearchProduct:
    def test_normal_product(self):
        item = {
            "productId": 186566,
            "brandName": "NIKE",
            "productName": "DUNK LOW QS",
            "productKoreanName": "덩크 로우 QS",
            "currentPrice": 159000,
            "initialPrice": 159000,
            "discountedRate": 0.0,
            "onlyOffline": False,
            "isPreOrder": False,
            "sizes": [
                {"sizeName": "240", "isSoldOut": False, "isLast": True},
                {"sizeName": "250", "isSoldOut": False, "isLast": False},
            ],
        }
        result = _parse_search_product(item)
        assert result is not None
        assert result["product_id"] == "186566"
        assert result["name"] == "DUNK LOW QS"
        assert result["brand"] == "NIKE"
        assert result["model_number"] == ""
        assert result["price"] == 159000
        assert result["original_price"] == 159000
        assert result["url"] == "https://www.worksout.co.kr/product/186566"
        assert result["is_sold_out"] is False
        assert len(result["sizes"]) == 2
        assert result["sizes"][0]["size"] == "240"
        assert result["sizes"][0]["price"] == 159000
        assert result["sizes"][0]["in_stock"] is True

    def test_offline_skipped(self):
        item = {
            "productId": 100,
            "brandName": "NIKE",
            "productName": "OFFLINE ONLY",
            "currentPrice": 100000,
            "initialPrice": 100000,
            "onlyOffline": True,
            "isPreOrder": False,
            "sizes": [{"sizeName": "260", "isSoldOut": False}],
        }
        assert _parse_search_product(item) is None

    def test_preorder_skipped(self):
        item = {
            "productId": 200,
            "brandName": "ADIDAS",
            "productName": "PREORDER SHOE",
            "currentPrice": 120000,
            "initialPrice": 120000,
            "onlyOffline": False,
            "isPreOrder": True,
            "sizes": [{"sizeName": "270", "isSoldOut": False}],
        }
        assert _parse_search_product(item) is None

    def test_all_sold_out_skipped(self):
        item = {
            "productId": 300,
            "brandName": "NB",
            "productName": "SOLD OUT SHOE",
            "currentPrice": 130000,
            "initialPrice": 130000,
            "onlyOffline": False,
            "isPreOrder": False,
            "sizes": [
                {"sizeName": "250", "isSoldOut": True},
                {"sizeName": "260", "isSoldOut": True},
            ],
        }
        assert _parse_search_product(item) is None

    def test_discounted_product(self):
        item = {
            "productId": 400,
            "brandName": "NIKE",
            "productName": "SALE SHOE",
            "currentPrice": 89000,
            "initialPrice": 159000,
            "onlyOffline": False,
            "isPreOrder": False,
            "sizes": [{"sizeName": "260", "isSoldOut": False}],
        }
        result = _parse_search_product(item)
        assert result is not None
        assert result["price"] == 89000
        assert result["original_price"] == 159000

    def test_no_product_id(self):
        item = {
            "brandName": "NIKE",
            "productName": "NO ID",
            "currentPrice": 100000,
            "sizes": [{"sizeName": "260", "isSoldOut": False}],
        }
        assert _parse_search_product(item) is None

    def test_korean_name_fallback(self):
        item = {
            "productId": 500,
            "brandName": "ASICS",
            "productName": "",
            "productKoreanName": "젤카야노 14",
            "currentPrice": 200000,
            "initialPrice": 200000,
            "onlyOffline": False,
            "isPreOrder": False,
            "sizes": [{"sizeName": "270", "isSoldOut": False}],
        }
        result = _parse_search_product(item)
        assert result is not None
        assert result["name"] == "젤카야노 14"


# --------------- httpx mock 기반 통합 테스트 ---------------


def _make_mock_resp(payload: dict, status: int = 200):
    resp = AsyncMock()
    resp.status_code = status
    resp.json = lambda: payload
    resp.text = ""
    return resp


@asynccontextmanager
async def _noop_acquire():
    yield


class TestSearchProducts:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "code": "SUCCESS",
            "payload": {
                "products": {
                    "content": [
                        {
                            "productId": 186566,
                            "brandName": "NIKE",
                            "productName": "DUNK LOW QS",
                            "productKoreanName": "덩크 로우 QS",
                            "currentPrice": 159000,
                            "initialPrice": 159000,
                            "onlyOffline": False,
                            "isPreOrder": False,
                            "sizes": [
                                {
                                    "sizeName": "240",
                                    "isSoldOut": False,
                                    "isLast": True,
                                },
                                {
                                    "sizeName": "250",
                                    "isSoldOut": False,
                                    "isLast": False,
                                },
                            ],
                        }
                    ],
                    "totalElements": 1,
                    "totalPages": 1,
                }
            },
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("DUNK LOW")

        assert len(results) == 1
        assert results[0]["product_id"] == "186566"
        assert results[0]["name"] == "DUNK LOW QS"
        assert results[0]["brand"] == "NIKE"
        assert results[0]["model_number"] == ""
        assert len(results[0]["sizes"]) == 2

        # GET 호출 확인
        call_kwargs = client.get.call_args.kwargs
        assert "searchKeyword" in call_kwargs.get("params", {})

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self):
        crawler = WorksoutCrawler()
        results = await crawler.search_products("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_filters_offline_and_preorder(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "code": "SUCCESS",
            "payload": {
                "products": {
                    "content": [
                        {
                            "productId": 100,
                            "brandName": "NIKE",
                            "productName": "ONLINE SHOE",
                            "currentPrice": 100000,
                            "initialPrice": 100000,
                            "onlyOffline": False,
                            "isPreOrder": False,
                            "sizes": [
                                {"sizeName": "260", "isSoldOut": False},
                            ],
                        },
                        {
                            "productId": 200,
                            "brandName": "NIKE",
                            "productName": "OFFLINE SHOE",
                            "currentPrice": 100000,
                            "initialPrice": 100000,
                            "onlyOffline": True,
                            "isPreOrder": False,
                            "sizes": [
                                {"sizeName": "260", "isSoldOut": False},
                            ],
                        },
                        {
                            "productId": 300,
                            "brandName": "ADIDAS",
                            "productName": "PREORDER SHOE",
                            "currentPrice": 120000,
                            "initialPrice": 120000,
                            "onlyOffline": False,
                            "isPreOrder": True,
                            "sizes": [
                                {"sizeName": "270", "isSoldOut": False},
                            ],
                        },
                    ],
                    "totalElements": 3,
                    "totalPages": 1,
                }
            },
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("shoes")

        assert len(results) == 1
        assert results[0]["product_id"] == "100"

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({}, status=429)
            gc.return_value = client
            results = await crawler.search_products("test")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_empty_response(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "code": "SUCCESS",
            "payload": {
                "products": {
                    "content": [],
                    "totalElements": 0,
                    "totalPages": 0,
                }
            },
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("nonexistent")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_sold_out_filtered(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "code": "SUCCESS",
            "payload": {
                "products": {
                    "content": [
                        {
                            "productId": 999,
                            "brandName": "NIKE",
                            "productName": "SOLD OUT",
                            "currentPrice": 100000,
                            "initialPrice": 100000,
                            "onlyOffline": False,
                            "isPreOrder": False,
                            "sizes": [
                                {"sizeName": "250", "isSoldOut": True},
                                {"sizeName": "260", "isSoldOut": True},
                            ],
                        },
                    ],
                    "totalElements": 1,
                    "totalPages": 1,
                }
            },
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("sold out")

        assert results == []


class TestGetProductDetail:
    @pytest.mark.asyncio
    async def test_detail_returns_product(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "code": "SUCCESS",
            "payload": {
                "productCode": "NB25SUSESN00002004",
                "productName": "BB550GWB",
                "productKoreanName": "BB550GWB",
                "brandName": "NEW BALANCE",
                "currentPrice": 159000,
                "initialPrice": 159000,
                "productSizes": [
                    {"sizeName": "260", "isSoldOut": False, "isLast": False},
                    {"sizeName": "270", "isSoldOut": False, "isLast": False},
                    {"sizeName": "280", "isSoldOut": True, "isLast": False},
                ],
            },
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            product = await crawler.get_product_detail("186566")

        assert product is not None
        assert product.source == "worksout"
        assert product.product_id == "186566"
        assert product.name == "BB550GWB"
        assert product.brand == "NEW BALANCE"
        assert product.model_number == ""
        assert product.url == "https://www.worksout.co.kr/product/186566"
        assert len(product.sizes) == 2  # 280은 품절 제외
        assert product.sizes[0].size == "260"
        assert product.sizes[0].price == 159000
        assert product.sizes[0].in_stock is True
        assert product.sizes[1].size == "270"

    @pytest.mark.asyncio
    async def test_detail_with_discount(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "code": "SUCCESS",
            "payload": {
                "productCode": "NK001",
                "productName": "AIR MAX 90",
                "brandName": "NIKE",
                "currentPrice": 119000,
                "initialPrice": 169000,
                "productSizes": [
                    {"sizeName": "260", "isSoldOut": False},
                ],
            },
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            product = await crawler.get_product_detail("12345")

        assert product is not None
        assert len(product.sizes) == 1
        assert product.sizes[0].price == 119000
        assert product.sizes[0].original_price == 169000
        assert product.sizes[0].discount_rate > 0
        assert product.sizes[0].discount_type == "할인"

    @pytest.mark.asyncio
    async def test_detail_empty_id(self):
        crawler = WorksoutCrawler()
        result = await crawler.get_product_detail("")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_http_error(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({}, status=404)
            gc.return_value = client
            product = await crawler.get_product_detail("99999")

        assert product is None

    @pytest.mark.asyncio
    async def test_detail_all_sold_out(self):
        crawler = WorksoutCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "code": "SUCCESS",
            "payload": {
                "productCode": "XX001",
                "productName": "ALL SOLD",
                "brandName": "ASICS",
                "currentPrice": 150000,
                "initialPrice": 150000,
                "productSizes": [
                    {"sizeName": "260", "isSoldOut": True},
                    {"sizeName": "270", "isSoldOut": True},
                ],
            },
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            product = await crawler.get_product_detail("88888")

        assert product is not None
        assert len(product.sizes) == 0  # 모두 품절이므로 빈 리스트


class TestRegistration:
    def test_worksout_registered(self):
        from src.crawlers.registry import RETAIL_CRAWLERS, register
        from src.crawlers.worksout import worksout_crawler

        register("worksout", worksout_crawler, "웍스아웃")
        assert "worksout" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["worksout"]["label"] == "웍스아웃"
