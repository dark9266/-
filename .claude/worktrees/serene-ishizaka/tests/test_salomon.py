"""살로몬 크롤러 단위 테스트 — variant 파싱 + httpx mock."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers.salomon import (
    SalomonCrawler,
    _parse_product_json,
    _parse_variant,
)

# --------------- 파싱 함수 테스트 ---------------


class TestParseVariant:
    def test_normal_variant(self):
        v = {
            "sku": "L47988000",
            "option2": "230",
            "price": "280000",
            "compare_at_price": "",
            "available": True,
        }
        result = _parse_variant(v)
        assert result["size"] == "230"
        assert result["sku"] == "L47988000"
        assert result["price"] == 280000
        assert result["original_price"] == 280000
        assert result["in_stock"] is True

    def test_sale_variant(self):
        v = {
            "sku": "L47200100",
            "option2": "270",
            "price": "199000",
            "compare_at_price": "259000",
            "available": True,
        }
        result = _parse_variant(v)
        assert result["price"] == 199000
        assert result["original_price"] == 259000
        assert result["in_stock"] is True

    def test_sold_out_variant(self):
        v = {
            "sku": "L47988000",
            "option2": "260",
            "price": "280000",
            "compare_at_price": "",
            "available": False,
        }
        result = _parse_variant(v)
        assert result["in_stock"] is False

    def test_empty_sku(self):
        v = {
            "sku": "",
            "option2": "250",
            "price": "100000",
            "compare_at_price": "",
            "available": True,
        }
        result = _parse_variant(v)
        assert result["sku"] == ""
        assert result["size"] == "250"

    def test_missing_fields(self):
        v = {}
        result = _parse_variant(v)
        assert result["size"] == ""
        assert result["sku"] == ""
        assert result["price"] == 0
        assert result["original_price"] == 0
        assert result["in_stock"] is False

    def test_sku_uppercase(self):
        """소문자 SKU도 대문자로 정규화."""
        v = {
            "sku": "l47988000",
            "option2": "230",
            "price": "280000",
            "compare_at_price": "",
            "available": True,
        }
        result = _parse_variant(v)
        assert result["sku"] == "L47988000"


class TestParseProductJson:
    def test_normal_product(self):
        product = {
            "handle": "l47988000",
            "title": "XT-6",
            "vendor": "Salomon Korea",
            "images": [{"src": "https://cdn.shopify.com/img.jpg"}],
            "variants": [
                {
                    "sku": "L47988000",
                    "option1": "레이니 데이 / 팔로마 / 실버",
                    "option2": "230",
                    "price": "280000",
                    "compare_at_price": "",
                    "available": True,
                },
                {
                    "sku": "L47988000",
                    "option1": "레이니 데이 / 팔로마 / 실버",
                    "option2": "260",
                    "price": "280000",
                    "compare_at_price": "",
                    "available": False,
                },
            ],
        }
        result = _parse_product_json(product)
        assert result is not None
        assert result["product_id"] == "l47988000"
        assert result["name"] == "XT-6"
        assert result["brand"] == "Salomon"
        assert result["model_number"] == "L47988000"
        assert result["price"] == 280000
        assert result["url"] == "https://salomon.co.kr/products/l47988000"
        assert result["image_url"] == "https://cdn.shopify.com/img.jpg"
        assert len(result["sizes"]) == 2
        assert result["sizes"][0]["size"] == "230"
        assert result["sizes"][0]["in_stock"] is True
        assert result["sizes"][1]["size"] == "260"
        assert result["sizes"][1]["in_stock"] is False
        assert result["is_sold_out"] is False

    def test_all_sold_out(self):
        product = {
            "handle": "l12345678",
            "title": "Sold Out Shoe",
            "vendor": "Salomon Korea",
            "images": [],
            "variants": [
                {
                    "sku": "L12345678",
                    "option2": "250",
                    "price": "200000",
                    "compare_at_price": "",
                    "available": False,
                }
            ],
        }
        result = _parse_product_json(product)
        assert result is not None
        assert result["is_sold_out"] is True

    def test_no_variants(self):
        product = {
            "handle": "empty",
            "title": "Empty",
            "vendor": "Salomon Korea",
            "images": [],
            "variants": [],
        }
        result = _parse_product_json(product)
        assert result is not None
        assert result["is_sold_out"] is True
        assert result["model_number"] == ""

    def test_empty_product(self):
        assert _parse_product_json(None) is None

    def test_minimal_product(self):
        """최소 필드만 있는 상품."""
        product = {"handle": "test", "title": "", "vendor": "", "images": [], "variants": []}
        result = _parse_product_json(product)
        assert result is not None
        assert result["product_id"] == "test"

    def test_sale_product(self):
        product = {
            "handle": "l99900000",
            "title": "Sale Item",
            "vendor": "Salomon Korea",
            "images": [],
            "variants": [
                {
                    "sku": "L99900000",
                    "option2": "270",
                    "price": "159000",
                    "compare_at_price": "219000",
                    "available": True,
                }
            ],
        }
        result = _parse_product_json(product)
        assert result["price"] == 159000
        assert result["original_price"] == 219000

    def test_no_images(self):
        product = {
            "handle": "l00000000",
            "title": "No Image",
            "vendor": "Salomon Korea",
            "images": [],
            "variants": [],
        }
        result = _parse_product_json(product)
        assert result["image_url"] == ""


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
    async def test_search_exact_match(self):
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "product": {
                "handle": "l47988000",
                "title": "XT-6",
                "vendor": "Salomon Korea",
                "images": [{"src": "https://cdn.shopify.com/img.jpg"}],
                "variants": [
                    {
                        "sku": "L47988000",
                        "option1": "레이니 데이 / 팔로마 / 실버",
                        "option2": "230",
                        "price": "280000",
                        "compare_at_price": "",
                        "available": True,
                    },
                    {
                        "sku": "L47988000",
                        "option2": "260",
                        "price": "280000",
                        "compare_at_price": "",
                        "available": True,
                    },
                ],
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("L47988000")

        assert len(results) == 1
        assert results[0]["product_id"] == "l47988000"
        assert results[0]["model_number"] == "L47988000"
        assert results[0]["brand"] == "Salomon"
        assert len(results[0]["sizes"]) == 2

        # URL 확인: handle 소문자로 조회
        call_args = client.get.call_args
        assert "l47988000" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self):
        crawler = SalomonCrawler()
        results = await crawler.search_products("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_404(self):
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({}, status=404)
            gc.return_value = client
            results = await crawler.search_products("NONEXISTENT")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_http_error(self):
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({}, status=500)
            gc.return_value = client
            results = await crawler.search_products("L47988000")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_sku_mismatch(self):
        """SKU가 keyword와 불일치하면 빈 리스트."""
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "product": {
                "handle": "l47988000",
                "title": "XT-6",
                "vendor": "Salomon Korea",
                "images": [],
                "variants": [
                    {
                        "sku": "L47988000",
                        "option2": "230",
                        "price": "280000",
                        "compare_at_price": "",
                        "available": True,
                    }
                ],
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            # 다른 모델번호로 검색 — handle이 같아도 SKU 불일치
            results = await crawler.search_products("L99999999")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_lowercase_keyword(self):
        """소문자 키워드도 정상 처리."""
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "product": {
                "handle": "l47988000",
                "title": "XT-6",
                "vendor": "Salomon Korea",
                "images": [],
                "variants": [
                    {
                        "sku": "L47988000",
                        "option2": "230",
                        "price": "280000",
                        "compare_at_price": "",
                        "available": True,
                    }
                ],
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("l47988000")

        assert len(results) == 1


class TestGetProductDetail:
    @pytest.mark.asyncio
    async def test_detail_returns_product(self):
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "product": {
                "handle": "l47988000",
                "title": "XT-6",
                "vendor": "Salomon Korea",
                "images": [{"src": "https://cdn.shopify.com/img.jpg"}],
                "variants": [
                    {
                        "sku": "L47988000",
                        "option1": "레이니 데이 / 팔로마 / 실버",
                        "option2": "230",
                        "price": "280000",
                        "compare_at_price": "320000",
                        "available": True,
                    },
                    {
                        "sku": "L47988000",
                        "option2": "260",
                        "price": "280000",
                        "compare_at_price": "",
                        "available": False,
                    },
                ],
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            product = await crawler.get_product_detail("l47988000")

        assert product is not None
        assert product.source == "salomon"
        assert product.product_id == "l47988000"
        assert product.model_number == "L47988000"
        assert product.brand == "Salomon"
        assert len(product.sizes) == 1  # 260은 품절 제외
        assert product.sizes[0].size == "230"
        assert product.sizes[0].price == 280000
        assert product.sizes[0].original_price == 320000
        assert product.sizes[0].in_stock is True
        assert product.sizes[0].discount_rate > 0

    @pytest.mark.asyncio
    async def test_detail_empty_id(self):
        crawler = SalomonCrawler()
        result = await crawler.get_product_detail("")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_http_error(self):
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({}, status=500)
            gc.return_value = client
            product = await crawler.get_product_detail("l47988000")

        assert product is None

    @pytest.mark.asyncio
    async def test_detail_no_product_key(self):
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp({"errors": "Not found"})
            gc.return_value = client
            product = await crawler.get_product_detail("nonexistent")

        assert product is None

    @pytest.mark.asyncio
    async def test_detail_all_sizes_in_stock(self):
        """모든 사이즈 재고 있는 경우."""
        crawler = SalomonCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "product": {
                "handle": "l47200100",
                "title": "SPEEDCROSS 6",
                "vendor": "Salomon Korea",
                "images": [],
                "variants": [
                    {
                        "sku": "L47200100",
                        "option2": "250",
                        "price": "189000",
                        "compare_at_price": "",
                        "available": True,
                    },
                    {
                        "sku": "L47200100",
                        "option2": "260",
                        "price": "189000",
                        "compare_at_price": "",
                        "available": True,
                    },
                    {
                        "sku": "L47200100",
                        "option2": "270",
                        "price": "189000",
                        "compare_at_price": "",
                        "available": True,
                    },
                ],
            }
        }

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            product = await crawler.get_product_detail("l47200100")

        assert product is not None
        assert len(product.sizes) == 3
        assert all(s.in_stock for s in product.sizes)


class TestRegistration:
    def test_salomon_registered(self):
        # 다른 테스트가 RETAIL_CRAWLERS.clear()를 했을 수 있음 → 강제 재등록
        from src.crawlers.registry import RETAIL_CRAWLERS, register
        from src.crawlers.salomon import salomon_crawler

        register("salomon", salomon_crawler, "살로몬")
        assert "salomon" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["salomon"]["label"] == "살로몬"
