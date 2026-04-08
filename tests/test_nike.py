"""나이키 크롤러 단위 테스트 — HTML 파싱 순수 함수."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers.nike import (
    NikeCrawler,
    _extract_next_data,
    _parse_products_from_wall,
    _parse_sizes_from_pdp,
    _parse_stock_from_threads,
)


class TestExtractNextData:
    """__NEXT_DATA__ JSON 추출 테스트."""

    def test_extract_next_data(self):
        """정상 __NEXT_DATA__ 추출."""
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"test":1}}}'
            "</script>"
        )
        data = _extract_next_data(html)
        assert data["props"]["pageProps"]["test"] == 1

    def test_extract_next_data_empty(self):
        """__NEXT_DATA__ 없는 HTML → 빈 dict."""
        assert _extract_next_data("<html><body></body></html>") == {}


class TestParseProductsFromWall:
    """검색 결과 Wall 파싱 테스트."""

    def test_parse_products(self):
        """Wall productGroupings → 상품 리스트."""
        next_data = {
            "props": {
                "pageProps": {
                    "initialState": {
                        "Wall": {
                            "productGroupings": [
                                {
                                    "products": [
                                        {
                                            "productCode": "DQ8423-100",
                                            "copy": {
                                                "title": "나이키 덩크 로우",
                                                "subTitle": "남성 신발",
                                            },
                                            "prices": {
                                                "currency": "KRW",
                                                "currentPrice": 139000,
                                                "initialPrice": 139000,
                                            },
                                            "pdpUrl": {
                                                "url": "https://www.nike.com/kr/t/dunk/DQ8423-100",
                                            },
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
        results = _parse_products_from_wall(next_data)
        assert len(results) == 1
        assert results[0]["product_id"] == "DQ8423-100"
        assert results[0]["model_number"] == "DQ8423-100"
        assert results[0]["price"] == 139000
        assert results[0]["brand"] == "Nike"

    def test_parse_empty_wall(self):
        """빈 Wall → 빈 리스트."""
        next_data = {"props": {"pageProps": {"initialState": {"Wall": {}}}}}
        assert _parse_products_from_wall(next_data) == []

    def test_parse_no_product_code_skipped(self):
        """productCode 없는 항목 스킵."""
        next_data = {
            "props": {
                "pageProps": {
                    "initialState": {
                        "Wall": {
                            "productGroupings": [
                                {"products": [{"copy": {"title": "No Code"}}]}
                            ]
                        }
                    }
                }
            }
        }
        assert _parse_products_from_wall(next_data) == []


class TestParseSizesFromPdp:
    """상품 상세 사이즈 파싱 테스트."""

    def test_parse_sizes_available(self):
        """selectedProduct.sizes에서 사이즈 추출 (2024+ 구조)."""
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"selectedProduct":{"sizes":['
            '{"localizedLabel":"270","label":"270","status":"ACTIVE","skuId":"123"},'
            '{"localizedLabel":"280","label":"280","status":"OUT_OF_STOCK","skuId":"456"}'
            ']}}}}'
            "</script>"
        )
        sizes = _parse_sizes_from_pdp(html)
        assert len(sizes) == 2
        assert sizes[0]["size"] == "270"
        assert sizes[0]["available"] is True
        assert sizes[1]["size"] == "280"
        assert sizes[1]["available"] is False

    def test_parse_sizes_empty(self):
        """사이즈 데이터 없음 → 빈 리스트."""
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"selectedProduct":{}}}}'
            "</script>"
        )
        assert _parse_sizes_from_pdp(html) == []


class TestParseStockFromThreads:
    """threads API 재고 파싱 테스트."""

    THREADS_RESPONSE = {
        "objects": [
            {
                "productInfo": [
                    {
                        "skus": [
                            {
                                "gtin": "00196975566839",
                                "countrySpecifications": [
                                    {"localizedSize": "240", "size": "6"}
                                ],
                            },
                            {
                                "gtin": "00196975566846",
                                "countrySpecifications": [
                                    {"localizedSize": "250", "size": "7"}
                                ],
                            },
                            {
                                "gtin": "00196975566853",
                                "countrySpecifications": [
                                    {"localizedSize": "260", "size": "8"}
                                ],
                            },
                        ],
                        "availableGtins": [
                            {
                                "gtin": "00196975566839",
                                "available": False,
                                "level": "OOS",
                            },
                            {
                                "gtin": "00196975566846",
                                "available": True,
                                "level": "HIGH",
                            },
                            {
                                "gtin": "00196975566853",
                                "available": True,
                                "level": "LOW",
                            },
                        ],
                    }
                ]
            }
        ]
    }

    def test_parse_available_gtins(self):
        """availableGtins + skus 매핑으로 사이즈별 재고 판정."""
        stock = _parse_stock_from_threads(self.THREADS_RESPONSE)
        assert stock == {"240": False, "250": True, "260": True}

    def test_empty_response(self):
        """빈 응답 → 빈 딕셔너리."""
        assert _parse_stock_from_threads({}) == {}
        assert _parse_stock_from_threads({"objects": []}) == {}

    def test_no_product_info(self):
        """productInfo 없음 → 빈 딕셔너리."""
        assert _parse_stock_from_threads({"objects": [{}]}) == {}


class TestOosSizeExcluded:
    """품절 사이즈가 get_product_detail에서 제외되는지 테스트."""

    PDP_HTML = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"selectedProduct":{'
        '"productInfo":{"title":"Nike Mind 001"},'
        '"prices":{"currentPrice":139000,"initialPrice":139000},'
        '"sizes":['
        '{"localizedLabel":"240","status":"ACTIVE"},'
        '{"localizedLabel":"250","status":"ACTIVE"},'
        '{"localizedLabel":"260","status":"ACTIVE"}'
        "]}}}}"
        "</script>"
    )

    @staticmethod
    def _make_mock_limiter():
        """async with limiter.acquire(): 호환 mock 생성."""
        @asynccontextmanager
        async def _noop():
            yield

        limiter = AsyncMock()
        limiter.acquire = _noop
        return limiter

    @pytest.mark.asyncio
    async def test_oos_size_filtered(self):
        """threads API에서 OOS인 사이즈 240이 결과에서 제외."""
        stock_map = {"240": False, "250": True, "260": True}

        crawler = NikeCrawler()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.text = self.PDP_HTML
        crawler._rate_limiter = self._make_mock_limiter()

        with (
            patch.object(crawler, "_fetch_stock_by_threads_api", return_value=stock_map),
            patch.object(crawler, "_get_client") as mock_client,
        ):
            client = AsyncMock()
            client.get.return_value = mock_resp
            mock_client.return_value = client

            product = await crawler.get_product_detail("HQ4307-001")

        assert product is not None
        size_labels = [s.size for s in product.sizes]
        assert "240" not in size_labels
        assert "250" in size_labels
        assert "260" in size_labels

    @pytest.mark.asyncio
    async def test_api_failure_fallback(self):
        """threads API 실패 시 기존 로직(status=ACTIVE) 전부 포함."""
        crawler = NikeCrawler()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.text = self.PDP_HTML
        crawler._rate_limiter = self._make_mock_limiter()

        with (
            patch.object(crawler, "_fetch_stock_by_threads_api", return_value={}),
            patch.object(crawler, "_get_client") as mock_client,
        ):
            client = AsyncMock()
            client.get.return_value = mock_resp
            mock_client.return_value = client

            product = await crawler.get_product_detail("HQ4307-001")

        assert product is not None
        assert len(product.sizes) == 3  # fallback: 전부 포함
