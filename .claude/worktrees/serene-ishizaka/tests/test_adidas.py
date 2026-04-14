"""아디다스 크롤러 단위 테스트 — taxonomy API 파싱 순수 함수."""

from src.crawlers.adidas import (
    AdidasCrawler,
    _parse_items,
)


class TestParseItems:
    """taxonomy API 응답 파싱 테스트."""

    def test_parse_normal_response(self):
        """정상 응답에서 상품 추출."""
        data = {
            "itemList": {
                "count": 2,
                "items": [
                    {
                        "productId": "B75806",
                        "displayName": "삼바 OG",
                        "price": 149000,
                        "salePrice": 149000,
                        "link": "/삼바-og/B75806.html",
                        "image": {"src": "https://assets.adidas.com/B75806.jpg"},
                        "orderable": 1,
                        "availableSizes": ["260", "270", "hidden", "280"],
                    },
                    {
                        "productId": "IE3437",
                        "displayName": "삼바 OG W",
                        "price": 149000,
                        "salePrice": 119000,
                        "link": "/삼바-og-w/IE3437.html",
                        "image": {"src": "https://assets.adidas.com/IE3437.jpg"},
                        "orderable": 1,
                        "availableSizes": ["230", "240", "hidden"],
                    },
                ],
            }
        }
        results = _parse_items(data)
        assert len(results) == 2

        # 첫 번째 상품
        assert results[0]["product_id"] == "B75806"
        assert results[0]["model_number"] == "B75806"
        assert results[0]["name"] == "삼바 OG"
        assert results[0]["price"] == 149000
        assert results[0]["original_price"] == 149000
        assert results[0]["sizes"] == ["260", "270", "280"]
        assert results[0]["is_sold_out"] is False

        # 두 번째 상품 (할인)
        assert results[1]["product_id"] == "IE3437"
        assert results[1]["price"] == 119000
        assert results[1]["original_price"] == 149000
        assert results[1]["sizes"] == ["230", "240"]

    def test_parse_empty_response(self):
        """빈 응답 → 빈 리스트."""
        assert _parse_items({}) == []
        assert _parse_items({"itemList": {}}) == []
        assert _parse_items({"itemList": {"items": []}}) == []

    def test_parse_no_product_id_skipped(self):
        """productId 없는 항목 스킵."""
        data = {
            "itemList": {
                "items": [
                    {"displayName": "이름만 있음", "price": 100000},
                ]
            }
        }
        assert _parse_items(data) == []

    def test_parse_sold_out_item(self):
        """orderable=0 → is_sold_out=True."""
        data = {
            "itemList": {
                "items": [
                    {
                        "productId": "XX1234",
                        "displayName": "품절 상품",
                        "price": 100000,
                        "salePrice": 0,
                        "orderable": 0,
                        "availableSizes": [],
                    },
                ]
            }
        }
        results = _parse_items(data)
        assert len(results) == 1
        assert results[0]["is_sold_out"] is True
        assert results[0]["price"] == 100000  # salePrice 0이면 price 사용

    def test_hidden_sizes_filtered(self):
        """availableSizes에서 'hidden' 엔트리 제거."""
        data = {
            "itemList": {
                "items": [
                    {
                        "productId": "AB1234",
                        "displayName": "테스트",
                        "price": 100000,
                        "availableSizes": ["hidden", "260", "hidden", "270"],
                    },
                ]
            }
        }
        results = _parse_items(data)
        assert results[0]["sizes"] == ["260", "270"]


class TestAdidasCrawlerInit:
    """크롤러 초기화 테스트."""

    def test_init(self):
        """인스턴스 생성."""
        crawler = AdidasCrawler()
        assert crawler._client is None
