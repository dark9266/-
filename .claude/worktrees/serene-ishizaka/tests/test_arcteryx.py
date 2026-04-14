"""아크테릭스 크롤러 단위 테스트."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.crawlers.arcteryx import (
    ArcteryxCrawler,
    _parse_options,
    _parse_search_item,
    normalize_arcteryx_size,
)

# --------------- 파싱 함수 테스트 ---------------


class TestParseSearchItem:
    """_parse_search_item 파싱 테스트."""

    def test_valid_item(self):
        item = {
            "product_id": 685124,
            "product_name": "알파 SV 재킷 남성",
            "sale_state": "ON",
            "sell_price": 1112000,
            "retail_price": 1390000,
            "option_image": [
                {
                    "image_chip": "https://product.arcteryx.co.kr/img/685124.jpg",
                    "base_color": "BLACK",
                }
            ],
        }
        result = _parse_search_item(item)
        assert result is not None
        assert result["product_id"] == "685124"
        assert result["name"] == "알파 SV 재킷 남성"
        assert result["brand"] == "Arc'teryx"
        assert result["price"] == 1112000
        assert result["original_price"] == 1390000
        assert result["url"] == "https://arcteryx.co.kr/products/685124"
        assert result["image_url"] == "https://product.arcteryx.co.kr/img/685124.jpg"
        assert result["is_sold_out"] is False
        assert result["model_number"] == ""

    def test_sold_out_item_returns_none(self):
        item = {
            "product_id": 12345,
            "product_name": "솔드아웃 상품",
            "sale_state": "SOLDOUT",
            "sell_price": 500000,
            "retail_price": 500000,
        }
        result = _parse_search_item(item)
        assert result is None

    def test_missing_product_id_returns_none(self):
        item = {
            "product_name": "이름만 있는 상품",
            "sale_state": "ON",
            "sell_price": 100000,
        }
        result = _parse_search_item(item)
        assert result is None

    def test_no_option_image(self):
        item = {
            "product_id": 99999,
            "product_name": "이미지 없음",
            "sale_state": "ON",
            "sell_price": 200000,
            "retail_price": 250000,
            "option_image": [],
        }
        result = _parse_search_item(item)
        assert result is not None
        assert result["image_url"] == ""


class TestParseOptions:
    """_parse_options 파싱 테스트."""

    def test_valid_options(self):
        data = {
            "has_options": True,
            "options": [
                {
                    "level": 1,
                    "label": "Colour",
                    "code": "AJPSM07555",
                    "type": "COLOR_CHIP",
                    "values": [
                        {
                            "id": 56479,
                            "value": "BLACK",
                            "sale_state": "ON",
                            "parent_ids": [0],
                        }
                    ],
                },
                {
                    "level": 2,
                    "label": "Size",
                    "type": "TEXT",
                    "values": [
                        {
                            "id": 56480,
                            "value": "XS",
                            "sale_state": "ON",
                            "sell_price": 1112000,
                            "is_orderable": True,
                            "stock": 24,
                            "parent_ids": [0, 56479],
                        },
                        {
                            "id": 56481,
                            "value": "SM",
                            "sale_state": "ON",
                            "sell_price": 1112000,
                            "is_orderable": True,
                            "stock": 10,
                            "parent_ids": [0, 56479],
                        },
                        {
                            "id": 56482,
                            "value": "MD",
                            "sale_state": "SOLDOUT",
                            "sell_price": 1112000,
                            "is_orderable": False,
                            "stock": 0,
                            "parent_ids": [0, 56479],
                        },
                    ],
                },
            ],
        }
        model_number, sizes = _parse_options(data)
        assert model_number == "AJPSM07555"
        assert len(sizes) == 3
        # XS: 재고 있음
        assert sizes[0]["size"] == "XS"
        assert sizes[0]["in_stock"] is True
        assert sizes[0]["price"] == 1112000
        # SM: 재고 있음
        assert sizes[1]["size"] == "SM"
        assert sizes[1]["in_stock"] is True
        # MD: 품절
        assert sizes[2]["size"] == "MD"
        assert sizes[2]["in_stock"] is False

    def test_empty_options(self):
        data = {"options": []}
        model_number, sizes = _parse_options(data)
        assert model_number == ""
        assert sizes == []

    def test_no_level_1(self):
        """Colour 옵션이 없는 경우 model_number 빈 문자열."""
        data = {
            "options": [
                {
                    "level": 2,
                    "label": "Size",
                    "values": [
                        {
                            "value": "LG",
                            "sale_state": "ON",
                            "sell_price": 500000,
                            "is_orderable": True,
                            "stock": 5,
                        }
                    ],
                }
            ]
        }
        model_number, sizes = _parse_options(data)
        assert model_number == ""
        assert len(sizes) == 1
        assert sizes[0]["size"] == "LG"


class TestNormalizeSize:
    """사이즈 변환 테스트."""

    def test_standard_mappings(self):
        assert normalize_arcteryx_size("SM") == "S"
        assert normalize_arcteryx_size("MD") == "M"
        assert normalize_arcteryx_size("LG") == "L"
        assert normalize_arcteryx_size("XS") == "XS"
        assert normalize_arcteryx_size("XL") == "XL"
        assert normalize_arcteryx_size("XXL") == "2XL"

    def test_case_insensitive(self):
        assert normalize_arcteryx_size("sm") == "S"
        assert normalize_arcteryx_size("Md") == "M"
        assert normalize_arcteryx_size("lg") == "L"

    def test_passthrough(self):
        """매핑에 없는 사이즈는 그대로 반환."""
        assert normalize_arcteryx_size("ONE SIZE") == "ONE SIZE"
        assert normalize_arcteryx_size("28") == "28"


# --------------- 크롤러 통합 테스트 (모킹) ---------------


MOCK_SEARCH_RESPONSE = {
    "success": True,
    "data": {
        "count": 2,
        "rows": [
            {
                "product_id": 685124,
                "product_name": "알파 SV 재킷 남성",
                "sale_state": "ON",
                "sell_price": 1112000,
                "retail_price": 1390000,
                "option_image": [{"image_chip": "https://product.arcteryx.co.kr/img/1.jpg"}],
            },
            {
                "product_id": 685200,
                "product_name": "베타 LT 재킷 남성",
                "sale_state": "ON",
                "sell_price": 690000,
                "retail_price": 690000,
                "option_image": [],
            },
        ],
        "paginate": {
            "total": 2,
            "per_page": 40,
            "last_page": 1,
            "current_page": 1,
        },
    },
}

MOCK_OPTIONS_RESPONSE = {
    "success": True,
    "data": {
        "has_options": True,
        "options": [
            {
                "level": 1,
                "label": "Colour",
                "code": "AJPSM07555",
                "type": "COLOR_CHIP",
                "values": [{"id": 56479, "value": "BLACK", "sale_state": "ON"}],
            },
            {
                "level": 2,
                "label": "Size",
                "type": "TEXT",
                "values": [
                    {
                        "id": 56480,
                        "value": "SM",
                        "sale_state": "ON",
                        "sell_price": 1112000,
                        "is_orderable": True,
                        "stock": 24,
                    },
                    {
                        "id": 56481,
                        "value": "LG",
                        "sale_state": "ON",
                        "sell_price": 1112000,
                        "is_orderable": True,
                        "stock": 8,
                    },
                ],
            },
        ],
    },
}


def _make_mock_response(data: dict, status: int = 200):
    """httpx.Response 모킹 객체 생성.

    httpx.Response.json()은 동기 메서드이므로 MagicMock 사용.
    """
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = data
    mock.text = json.dumps(data)
    return mock


@pytest.fixture
def crawler():
    return ArcteryxCrawler()


class TestSearchProducts:
    """search_products 모킹 테스트."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self, crawler):
        mock_resp = _make_mock_response(MOCK_SEARCH_RESPONSE)
        with patch.object(crawler, "_get_client") as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            mock_client.return_value = client

            results = await crawler.search_products("알파 SV")

        assert len(results) == 2
        assert results[0]["product_id"] == "685124"
        assert results[0]["name"] == "알파 SV 재킷 남성"
        assert results[1]["product_id"] == "685200"

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self, crawler):
        results = await crawler.search_products("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_http_error(self, crawler):
        mock_resp = _make_mock_response({}, status=500)
        with patch.object(crawler, "_get_client") as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            mock_client.return_value = client

            results = await crawler.search_products("테스트")

        assert results == []


class TestGetProductDetail:
    """get_product_detail 모킹 테스트."""

    @pytest.mark.asyncio
    async def test_detail_returns_product(self, crawler):
        mock_resp = _make_mock_response(MOCK_OPTIONS_RESPONSE)
        with patch.object(crawler, "_get_client") as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            mock_client.return_value = client

            product = await crawler.get_product_detail("685124")

        assert product is not None
        assert product.source == "arcteryx"
        assert product.product_id == "685124"
        assert product.model_number == "AJPSM07555"
        assert product.brand == "Arc'teryx"
        assert len(product.sizes) == 2
        # SM -> S 변환 확인
        assert product.sizes[0].size == "S"
        assert product.sizes[0].price == 1112000
        # LG -> L 변환 확인
        assert product.sizes[1].size == "L"

    @pytest.mark.asyncio
    async def test_detail_empty_id(self, crawler):
        result = await crawler.get_product_detail("")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_http_error(self, crawler):
        mock_resp = _make_mock_response({}, status=404)
        with patch.object(crawler, "_get_client") as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            mock_client.return_value = client

            result = await crawler.get_product_detail("999999")

        assert result is None

    @pytest.mark.asyncio
    async def test_detail_no_options(self, crawler):
        no_opts = {
            "success": True,
            "data": {"has_options": False, "options": []},
        }
        mock_resp = _make_mock_response(no_opts)
        with patch.object(crawler, "_get_client") as mock_client:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            mock_client.return_value = client

            result = await crawler.get_product_detail("685124")

        assert result is None
