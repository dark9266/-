"""나이키 크롤러 단위 테스트 — HTML 파싱 순수 함수."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers.nike import (
    NikeCrawler,
    _extract_next_data,
    _parse_products_from_wall,
    _parse_sizes_from_pdp,
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


class TestLaunchProductSkipped:
    """LAUNCH 상품(SNKRS 큐 방식)은 차익거래 대상에서 제외."""

    LAUNCH_PDP_HTML = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"selectedProduct":{'
        '"productInfo":{"title":"Nike Mind 001"},'
        '"prices":{"currentPrice":119000,"initialPrice":119000},'
        '"consumerReleaseType":"LAUNCH",'
        '"statusModifier":"BUYABLE_LINE",'
        '"sizes":['
        '{"localizedLabel":"240","status":"ACTIVE"},'
        '{"localizedLabel":"250","status":"ACTIVE"}'
        "]}}}}"
        "</script>"
    )

    NORMAL_PDP_HTML = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"selectedProduct":{'
        '"productInfo":{"title":"Nike Pegasus"},'
        '"prices":{"currentPrice":139000,"initialPrice":139000},'
        '"consumerReleaseType":"RELEASE",'
        '"statusModifier":"BUYABLE_BUY",'
        '"sizes":['
        '{"localizedLabel":"270","status":"ACTIVE"},'
        '{"localizedLabel":"280","status":"ACTIVE"}'
        "]}}}}"
        "</script>"
    )

    @staticmethod
    def _make_mock_limiter():
        @asynccontextmanager
        async def _noop():
            yield

        limiter = AsyncMock()
        limiter.acquire = _noop
        return limiter

    async def _run(self, crawler, html):
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        crawler._rate_limiter = self._make_mock_limiter()
        with patch.object(crawler, "_get_client") as mock_client:
            client = AsyncMock()
            client.get.return_value = mock_resp
            mock_client.return_value = client
            return await crawler.get_product_detail("HQ4307-001")

    @pytest.mark.asyncio
    async def test_launch_product_returns_none(self):
        """consumerReleaseType=LAUNCH → None (회귀: HQ4307-001)."""
        product = await self._run(NikeCrawler(), self.LAUNCH_PDP_HTML)
        assert product is None

    @pytest.mark.asyncio
    async def test_buyable_line_skipped(self):
        """statusModifier=BUYABLE_LINE → None (큐 기반 구매)."""
        html = self.LAUNCH_PDP_HTML.replace(
            '"consumerReleaseType":"LAUNCH"', '"consumerReleaseType":"FLOW"'
        )
        product = await self._run(NikeCrawler(), html)
        assert product is None

    @pytest.mark.asyncio
    async def test_out_of_stock_searchable_skipped(self):
        """statusModifier=OUT_OF_STOCK_SEARCHABLE → None (회귀: HM4740-001).

        나이키가 sizes[].status=ACTIVE 를 반환하면서도 상품 레벨에서
        OUT_OF_STOCK_SEARCHABLE 인 경우 — sizes 배열을 믿으면 안 됨.
        """
        html = self.NORMAL_PDP_HTML.replace(
            '"statusModifier":"BUYABLE_BUY"',
            '"statusModifier":"OUT_OF_STOCK_SEARCHABLE"',
        )
        product = await self._run(NikeCrawler(), html)
        assert product is None

    @pytest.mark.asyncio
    async def test_normal_release_returns_product(self):
        """RELEASE + BUYABLE_BUY 상품은 정상 처리."""
        product = await self._run(NikeCrawler(), self.NORMAL_PDP_HTML)
        assert product is not None
        assert len(product.sizes) == 2
        assert {s.size for s in product.sizes} == {"270", "280"}
