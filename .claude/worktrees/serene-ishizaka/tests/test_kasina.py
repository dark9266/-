"""카시나 크롤러 단위 테스트 — 파싱 순수 함수 + httpx mock 통합."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers import kasina as kasina_mod
from src.crawlers.kasina import (
    NB_INLINE_RE,
    KasinaCrawler,
    _parse_search_item,
    _parse_sizes_from_options,
)


class TestParseSearchItem:
    def test_basic(self):
        it = {
            "productNo": 133292773,
            "productName": "나이키 SB 덩크 로우",
            "productNameEn": "NIKE SB DUNK LOW PRO",
            "brandNameKo": "NIKE",
            "productManagementCd": "HF3704-003",
            "salePrice": 149000,
            "isSoldOut": False,
        }
        row = _parse_search_item(it)
        assert row["product_id"] == "133292773"
        assert row["model_number"] == "HF3704-003"
        assert row["price"] == 149000
        assert row["brand"] == "NIKE"
        assert row["url"].endswith("/products/133292773")

    def test_no_product_no(self):
        row = _parse_search_item({"productNo": None, "productName": "x"})
        assert row["product_id"] == ""
        assert row["url"] == ""


class TestParseSizesFromOptions:
    def test_color_size_two_layer(self):
        """COLOR > SIZE 2-layer 구조 (전형적 신발)."""
        opts = {
            "multiLevelOptions": [
                {
                    "value": "BLACK",
                    "children": [
                        {
                            "value": "270",
                            "saleType": "AVAILABLE",
                            "stockCnt": -999,
                            "forcedSoldOut": False,
                            "buyPrice": 149000,
                        },
                        {
                            "value": "280",
                            "saleType": "SOLDOUT",
                            "stockCnt": 0,
                            "forcedSoldOut": False,
                            "buyPrice": 149000,
                        },
                    ],
                }
            ]
        }
        rows = _parse_sizes_from_options(opts)
        assert len(rows) == 2
        assert rows[0] == {"size": "270", "in_stock": True, "price": 149000}
        assert rows[1] == {"size": "280", "in_stock": False, "price": 149000}

    def test_forced_soldout_blocks_available(self):
        """forcedSoldOut=True → AVAILABLE이라도 in_stock=False (회귀)."""
        opts = {
            "multiLevelOptions": [
                {
                    "value": "BLACK",
                    "children": [
                        {
                            "value": "270",
                            "saleType": "AVAILABLE",
                            "stockCnt": -999,
                            "forcedSoldOut": True,
                            "buyPrice": 149000,
                        }
                    ],
                }
            ]
        }
        rows = _parse_sizes_from_options(opts)
        assert rows == [{"size": "270", "in_stock": False, "price": 149000}]

    def test_size_only_single_layer(self):
        """children 없이 SIZE 단일 레이어."""
        opts = {
            "multiLevelOptions": [
                {
                    "value": "270",
                    "saleType": "AVAILABLE",
                    "forcedSoldOut": False,
                    "buyPrice": 100000,
                },
                {
                    "value": "280",
                    "saleType": "SOLDOUT",
                    "forcedSoldOut": False,
                    "buyPrice": 100000,
                },
            ]
        }
        rows = _parse_sizes_from_options(opts)
        assert len(rows) == 2
        assert rows[0]["in_stock"] is True
        assert rows[1]["in_stock"] is False

    def test_empty(self):
        assert _parse_sizes_from_options({}) == []
        assert _parse_sizes_from_options({"multiLevelOptions": []}) == []


class TestNbInlineRegex:
    def test_extract_from_korean_name(self):
        name = "999휴머니티 X 뉴발란스 U2000ETC 라이트 블루"
        codes = NB_INLINE_RE.findall(name)
        assert "U2000ETC" in codes

    def test_extract_from_english_name(self):
        en = "NEW BALANCE M2002RXD"
        codes = NB_INLINE_RE.findall(en)
        assert "M2002RXD" in codes


def _make_mock_resp(payload: dict, status: int = 200):
    resp = AsyncMock()
    resp.status_code = status
    resp.json = lambda: payload
    return resp


@asynccontextmanager
async def _noop_acquire():
    yield


class TestSearchProductsExact:
    """Nike/adidas EXACT 매칭 경로."""

    @pytest.mark.asyncio
    async def test_exact_management_cd_hit(self):
        crawler = KasinaCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        payload = {
            "totalCount": 1,
            "items": [
                {
                    "productNo": 133292773,
                    "productName": "나이키 SB 덩크",
                    "brandNameKo": "NIKE",
                    "productManagementCd": "HF3704-003",
                    "salePrice": 149000,
                }
            ],
        }
        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(payload)
            gc.return_value = client
            results = await crawler.search_products("HF3704-003")

        assert len(results) == 1
        assert results[0]["product_id"] == "133292773"
        assert results[0]["model_number"] == "HF3704-003"
        # EXACT 경로는 URL에 filter.productManagementCd 포함
        called_url = client.get.call_args.args[0]
        assert "/products/search" in called_url
        params = client.get.call_args.kwargs["params"]
        assert params["filter.productManagementCd"] == "HF3704-003"


class TestGetProductDetailMocked:
    @pytest.mark.asyncio
    async def test_full_chain(self):
        crawler = KasinaCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        detail = {
            "baseInfo": {
                "productNo": 133292773,
                "productName": "나이키 SB 덩크 로우",
                "productManagementCd": "HF3704-003",
            },
            "price": {"salePrice": 149000, "immediateDiscountAmt": 0},
            "status": {"saleStatusType": "ONSALE", "soldout": False},
            "brand": {"name": "NIKE", "nameKo": "NIKE"},
        }
        options = {
            "multiLevelOptions": [
                {
                    "value": "BLACK",
                    "children": [
                        {
                            "value": "270",
                            "saleType": "AVAILABLE",
                            "stockCnt": -999,
                            "forcedSoldOut": False,
                            "buyPrice": 149000,
                        },
                        {
                            "value": "280",
                            "saleType": "SOLDOUT",
                            "forcedSoldOut": False,
                            "buyPrice": 149000,
                        },
                    ],
                }
            ]
        }

        async def fake_get(url, params=None):
            if url.endswith("/options"):
                return _make_mock_resp(options)
            return _make_mock_resp(detail)

        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.side_effect = fake_get
            gc.return_value = client
            product = await crawler.get_product_detail("133292773")

        assert product is not None
        assert product.source == "kasina"
        assert product.model_number == "HF3704-003"
        assert len(product.sizes) == 1  # 280은 SOLDOUT 제외
        assert product.sizes[0].size == "270"
        assert product.sizes[0].in_stock is True

    @pytest.mark.asyncio
    async def test_soldout_returns_none(self):
        crawler = KasinaCrawler()
        crawler._rate_limiter = AsyncMock()
        crawler._rate_limiter.acquire = _noop_acquire

        detail = {
            "baseInfo": {"productManagementCd": "X"},
            "price": {"salePrice": 0},
            "status": {"saleStatusType": "ONSALE", "soldout": True},
            "brand": {},
        }
        with patch.object(crawler, "_get_client") as gc:
            client = AsyncMock()
            client.get.return_value = _make_mock_resp(detail)
            gc.return_value = client
            product = await crawler.get_product_detail("999")

        assert product is None


class TestRegistryRegistration:
    def test_kasina_registered(self):
        # 다른 테스트가 RETAIL_CRAWLERS.clear()를 했을 수 있음 → 강제 재등록
        from src.crawlers.registry import RETAIL_CRAWLERS, register
        register("kasina", kasina_mod.kasina_crawler, "카시나")
        assert "kasina" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["kasina"]["label"] == "카시나"


@pytest.fixture(autouse=True)
def _clear_nb_cache():
    """매 테스트마다 NB 캐시 초기화 — 테스트 간 격리."""
    kasina_mod._nb_cache = None
    yield
    kasina_mod._nb_cache = None
