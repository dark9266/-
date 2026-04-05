"""나이키 크롤러 단위 테스트 — HTML 파싱 순수 함수."""

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
