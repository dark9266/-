"""튠 크롤러 단위 테스트 — variant 파싱 + GraphQL mock."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers.tune import (
    TuneCrawler,
    _parse_variant_title,
    _is_hidden,
    _extract_product_id,
    _parse_product_node,
    _escape_graphql_string,
)


class TestParseVariantTitle:
    def test_model_and_size(self):
        model, size = _parse_variant_title("IQ3446-010 / 230")
        assert model == "IQ3446-010"
        assert size == "230"

    def test_no_size(self):
        model, size = _parse_variant_title("IQ3446-010")
        assert model == "IQ3446-010"
        assert size == ""

    def test_empty(self):
        model, size = _parse_variant_title("")
        assert model == ""
        assert size == ""

    def test_clothing_size(self):
        model, size = _parse_variant_title("ABC123 / M")
        assert model == "ABC123"
        assert size == "M"

    def test_multiple_slashes(self):
        model, size = _parse_variant_title("MODEL-001 / XL / extra")
        assert model == "MODEL-001"
        assert size == "XL / extra"


class TestIsHidden:
    def test_hidden_true(self):
        node = {"metafields": [{"key": "hidden", "value": "true"}]}
        assert _is_hidden(node) is True

    def test_hidden_false(self):
        node = {"metafields": [{"key": "hidden", "value": "false"}]}
        assert _is_hidden(node) is False

    def test_no_metafields(self):
        node = {"metafields": [None]}
        assert _is_hidden(node) is False

    def test_empty_metafields(self):
        node = {"metafields": []}
        assert _is_hidden(node) is False

    def test_missing_metafields_key(self):
        node = {}
        assert _is_hidden(node) is False


class TestExtractProductId:
    def test_normal_gid(self):
        assert _extract_product_id("gid://shopify/Product/12345") == "12345"

    def test_just_number(self):
        assert _extract_product_id("12345") == "12345"

    def test_long_id(self):
        assert _extract_product_id("gid://shopify/Product/10327112057107") == "10327112057107"


class TestEscapeGraphqlString:
    def test_quotes(self):
        assert _escape_graphql_string('hello "world"') == 'hello \\"world\\"'

    def test_backslash(self):
        assert _escape_graphql_string("a\\b") == "a\\\\b"

    def test_clean(self):
        assert _escape_graphql_string("IQ3446-010") == "IQ3446-010"


class TestParseProductNode:
    def test_normal_product(self):
        node = {
            "id": "gid://shopify/Product/10327112057107",
            "title": "ASICS HYPERSYNC",
            "handle": "asics-hypersync-abc",
            "vendor": "Asics",
            "metafields": [{"key": "hidden", "value": "false"}],
            "variants": {
                "edges": [
                    {
                        "node": {
                            "title": "1203A879-021 / 270",
                            "price": {"amount": "169000.0"},
                            "compareAtPrice": {"amount": "169000.0"},
                            "availableForSale": True,
                            "quantityAvailable": 3,
                        }
                    },
                    {
                        "node": {
                            "title": "1203A879-021 / 280",
                            "price": {"amount": "169000.0"},
                            "compareAtPrice": None,
                            "availableForSale": False,
                            "quantityAvailable": 0,
                        }
                    },
                ]
            },
            "images": {"edges": [{"node": {"url": "https://cdn.shopify.com/img.jpg"}}]},
        }
        result = _parse_product_node(node)
        assert result is not None
        assert result["product_id"] == "10327112057107"
        assert result["model_number"] == "1203A879-021"
        assert result["brand"] == "Asics"
        assert result["price"] == 169000
        assert result["url"] == "https://tune.kr/products/asics-hypersync-abc"
        assert result["image_url"] == "https://cdn.shopify.com/img.jpg"
        assert len(result["sizes"]) == 2
        assert result["sizes"][0]["size"] == "270"
        assert result["sizes"][0]["in_stock"] is True
        assert result["sizes"][1]["size"] == "280"
        assert result["sizes"][1]["in_stock"] is False
        assert result["is_sold_out"] is False

    def test_hidden_product_returns_none(self):
        node = {
            "id": "gid://shopify/Product/999",
            "title": "Hidden",
            "handle": "hidden",
            "vendor": "X",
            "metafields": [{"key": "hidden", "value": "true"}],
            "variants": {"edges": []},
            "images": {"edges": []},
        }
        assert _parse_product_node(node) is None

    def test_all_sold_out(self):
        node = {
            "id": "gid://shopify/Product/555",
            "title": "Sold Out Shoe",
            "handle": "sold-out-shoe",
            "vendor": "Nike",
            "metafields": [],
            "variants": {
                "edges": [
                    {
                        "node": {
                            "title": "ABC-001 / 260",
                            "price": {"amount": "100000.0"},
                            "compareAtPrice": None,
                            "availableForSale": False,
                            "quantityAvailable": 0,
                        }
                    }
                ]
            },
            "images": {"edges": []},
        }
        result = _parse_product_node(node)
        assert result is not None
        assert result["is_sold_out"] is True

    def test_no_variants(self):
        node = {
            "id": "gid://shopify/Product/888",
            "title": "Empty",
            "handle": "empty",
            "vendor": "X",
            "metafields": [],
            "variants": {"edges": []},
            "images": {"edges": []},
        }
        result = _parse_product_node(node)
        assert result is not None
        assert result["is_sold_out"] is True
        assert result["model_number"] == ""

    def test_compare_at_price_none_falls_back(self):
        node = {
            "id": "gid://shopify/Product/777",
            "title": "Sale Item",
            "handle": "sale-item",
            "vendor": "Adidas",
            "metafields": [],
            "variants": {
                "edges": [
                    {
                        "node": {
                            "title": "FX5502 / 250",
                            "price": {"amount": "89000.0"},
                            "compareAtPrice": None,
                            "availableForSale": True,
                            "quantityAvailable": 1,
                        }
                    }
                ]
            },
            "images": {"edges": []},
        }
        result = _parse_product_node(node)
        assert result["price"] == 89000
        assert result["original_price"] == 89000  # fallback to price


# --------------- httpx mock 기반 통합 테스트 ---------------


def _make_mock_resp(payload: dict, status: int = 200):
    resp = AsyncMock()
    resp.status_code = status
    resp.json = lambda: payload
    return resp


@asynccontextmanager
async def _noop_acquire():
    yield


class TestSearchProducts:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        crawler = TuneCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "data": {
                "products": {
                    "edges": [
                        {
                            "node": {
                                "id": "gid://shopify/Product/10327112057107",
                                "title": "ASICS HYPERSYNC",
                                "handle": "asics-hypersync",
                                "vendor": "Asics",
                                "metafields": [],
                                "variants": {
                                    "edges": [
                                        {
                                            "node": {
                                                "title": "1203A879-021 / 270",
                                                "price": {"amount": "169000.0"},
                                                "compareAtPrice": None,
                                                "availableForSale": True,
                                                "quantityAvailable": 2,
                                            }
                                        }
                                    ]
                                },
                                "images": {
                                    "edges": [
                                        {"node": {"url": "https://cdn.shopify.com/img.jpg"}}
                                    ]
                                },
                            }
                        }
                    ]
                }
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("1203A879-021")

        assert len(results) == 1
        assert results[0]["product_id"] == "10327112057107"
        assert results[0]["model_number"] == "1203A879-021"
        assert results[0]["sizes"][0]["size"] == "270"

        # GET 방식 확인 (params에 query 포함)
        call_kwargs = client.get.call_args.kwargs
        assert "query" in call_kwargs.get("params", {})

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self):
        crawler = TuneCrawler()
        results = await crawler.search_products("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_filters_hidden(self):
        crawler = TuneCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "data": {
                "products": {
                    "edges": [
                        {
                            "node": {
                                "id": "gid://shopify/Product/111",
                                "title": "Visible",
                                "handle": "visible",
                                "vendor": "A",
                                "metafields": [],
                                "variants": {
                                    "edges": [
                                        {
                                            "node": {
                                                "title": "M-001 / 260",
                                                "price": {"amount": "100000.0"},
                                                "compareAtPrice": None,
                                                "availableForSale": True,
                                                "quantityAvailable": 1,
                                            }
                                        }
                                    ]
                                },
                                "images": {"edges": []},
                            }
                        },
                        {
                            "node": {
                                "id": "gid://shopify/Product/222",
                                "title": "Hidden Item",
                                "handle": "hidden-item",
                                "vendor": "B",
                                "metafields": [{"key": "hidden", "value": "true"}],
                                "variants": {"edges": []},
                                "images": {"edges": []},
                            }
                        },
                    ]
                }
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("test")

        assert len(results) == 1
        assert results[0]["product_id"] == "111"

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        crawler = TuneCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({}, status=429)
            gc.return_value = client
            results = await crawler.search_products("test")

        assert results == []


class TestGetProductDetail:
    @pytest.mark.asyncio
    async def test_detail_returns_product(self):
        crawler = TuneCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "data": {
                "node": {
                    "id": "gid://shopify/Product/10327112057107",
                    "title": "ASICS HYPERSYNC",
                    "handle": "asics-hypersync",
                    "vendor": "Asics",
                    "metafields": [],
                    "variants": {
                        "edges": [
                            {
                                "node": {
                                    "title": "1203A879-021 / 270",
                                    "price": {"amount": "169000.0"},
                                    "compareAtPrice": {"amount": "199000.0"},
                                    "availableForSale": True,
                                    "quantityAvailable": 3,
                                }
                            },
                            {
                                "node": {
                                    "title": "1203A879-021 / 280",
                                    "price": {"amount": "169000.0"},
                                    "compareAtPrice": None,
                                    "availableForSale": False,
                                    "quantityAvailable": 0,
                                }
                            },
                        ]
                    },
                    "images": {
                        "edges": [{"node": {"url": "https://cdn.shopify.com/img.jpg"}}]
                    },
                }
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            product = await crawler.get_product_detail("10327112057107")

        assert product is not None
        assert product.source == "tune"
        assert product.product_id == "10327112057107"
        assert product.model_number == "1203A879-021"
        assert product.brand == "Asics"
        assert len(product.sizes) == 1  # 280은 품절 제외
        assert product.sizes[0].size == "270"
        assert product.sizes[0].price == 169000
        assert product.sizes[0].original_price == 199000
        assert product.sizes[0].in_stock is True
        assert product.sizes[0].discount_rate > 0

    @pytest.mark.asyncio
    async def test_detail_empty_id(self):
        crawler = TuneCrawler()
        result = await crawler.get_product_detail("")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_hidden_returns_none(self):
        crawler = TuneCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "data": {
                "node": {
                    "id": "gid://shopify/Product/999",
                    "title": "Hidden",
                    "handle": "hidden",
                    "vendor": "X",
                    "metafields": [{"key": "hidden", "value": "true"}],
                    "variants": {"edges": []},
                    "images": {"edges": []},
                }
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            product = await crawler.get_product_detail("999")

        assert product is None

    @pytest.mark.asyncio
    async def test_detail_http_error(self):
        crawler = TuneCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({}, status=500)
            gc.return_value = client
            product = await crawler.get_product_detail("12345")

        assert product is None


class TestRegistration:
    def test_tune_registered(self):
        from src.crawlers.registry import RETAIL_CRAWLERS, register
        from src.crawlers.tune import tune_crawler

        register("tune", tune_crawler, "튠")
        assert "tune" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["tune"]["label"] == "튠"
