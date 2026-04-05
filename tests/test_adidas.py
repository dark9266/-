"""아디다스 크롤러 단위 테스트 — HTML 파싱 순수 함수."""

from src.crawlers.adidas import (
    AdidasCrawler,
    _extract_items_from_state,
    _extract_model_id,
    _parse_schema_org,
    _parse_search_results,
)


class TestExtractModelId:
    """아디다스 모델 ID 추출 테스트."""

    def test_standard_pattern(self):
        """표준 아디다스 모델 ID (IG1025)."""
        assert _extract_model_id("삼바 OG IG1025 화이트") == "IG1025"

    def test_gw_pattern(self):
        """GW 접두사 모델 ID."""
        assert _extract_model_id("가젤 GW2871") == "GW2871"

    def test_no_model(self):
        """모델 ID 없음 → 빈 문자열."""
        assert _extract_model_id("일반 상품명") == ""


class TestParseSearchResults:
    """검색 결과 파싱 테스트."""

    def test_parse_schema_item_list(self):
        """schema.org ItemList에서 상품 추출."""
        html = '''
        <script type="application/ld+json">
        {"@type": "ItemList", "itemListElement": [
            {"item": {"@type": "Product", "name": "삼바 OG IG1025",
             "url": "https://www.adidas.co.kr/IG1025.html",
             "offers": {"price": "159000"}}}
        ]}
        </script>
        '''
        results = _parse_search_results(html)
        assert len(results) == 1
        assert results[0]["model_number"] == "IG1025"
        assert results[0]["price"] == 159000

    def test_parse_empty(self):
        """빈 HTML → 빈 리스트."""
        assert _parse_search_results("<html></html>") == []


class TestExtractItemsFromState:
    """window.__STATE__ 상품 추출 테스트."""

    def test_extract_from_item_list(self):
        """itemList 키에서 상품 추출."""
        state = {
            "search": {
                "itemList": [
                    {
                        "displayName": "삼바 OG",
                        "modelId": "IG1025",
                        "salePrice": 159000,
                        "productId": "IG1025",
                        "link": "/IG1025.html",
                    }
                ]
            }
        }
        items = _extract_items_from_state(state)
        assert len(items) == 1
        assert items[0]["model_number"] == "IG1025"
        assert items[0]["price"] == 159000

    def test_extract_empty(self):
        """빈 state → 빈 리스트."""
        assert _extract_items_from_state({}) == []


class TestAdidasParseSizes:
    """사이즈 파싱 테스트."""

    def setup_method(self):
        self.crawler = AdidasCrawler()

    def test_parse_variation_list(self):
        """variation_list JSON에서 사이즈 추출."""
        html = '''
        <script>
        "variation_list":[
            {"size":"260","availability_status":"IN_STOCK"},
            {"size":"270","availability_status":"NOT_AVAILABLE"},
            {"size":"280","availability_status":"IN_STOCK"}
        ]
        </script>
        '''
        sizes = self.crawler._parse_sizes_from_html(html, 159000, 159000)
        assert len(sizes) == 2
        assert sizes[0].size == "260"
        assert sizes[1].size == "280"
        assert all(s.in_stock for s in sizes)

    def test_parse_no_sizes(self):
        """사이즈 데이터 없음 → 빈 리스트."""
        sizes = self.crawler._parse_sizes_from_html("<html></html>", 0, 0)
        assert sizes == []
