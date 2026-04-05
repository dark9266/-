"""ABC마트 크롤러 단위 테스트 — HTML 파싱 순수 함수."""

from src.crawlers.abcmart import (
    AbcMartCrawler,
    _extract_model_number,
    _parse_products_from_html,
    _parse_schema_org,
)


class TestExtractModelNumber:
    """모델번호 추출 테스트."""

    def test_hyphen_pattern(self):
        """하이픈 포함 모델번호 추출."""
        assert _extract_model_number("나이키 덩크 로우 DQ8423-100") == "DQ8423-100"

    def test_alpha_num_pattern(self):
        """알파벳+숫자 모델번호 추출."""
        assert _extract_model_number("아디다스 삼바 IG1025 화이트") == "IG1025"

    def test_no_model(self):
        """모델번호 없는 문자열 → 빈 문자열."""
        assert _extract_model_number("일반 상품 이름만 있음") == ""


class TestParseSchemaOrg:
    """schema.org JSON-LD 파싱 테스트."""

    def test_parse_product(self):
        """Product 타입 JSON-LD 파싱."""
        html = '''
        <script type="application/ld+json">
        {"@type": "Product", "name": "나이키 에어포스", "brand": {"name": "Nike"},
         "offers": {"price": "119000"}}
        </script>
        '''
        data = _parse_schema_org(html)
        assert data["name"] == "나이키 에어포스"
        assert data["offers"]["price"] == "119000"

    def test_parse_no_product(self):
        """Product가 아닌 JSON-LD → 빈 dict."""
        html = '''
        <script type="application/ld+json">
        {"@type": "Organization", "name": "ABC마트"}
        </script>
        '''
        assert _parse_schema_org(html) == {}


class TestParseProductsFromHtml:
    """검색 결과 HTML 파싱 테스트."""

    def test_parse_item_list(self):
        """schema.org ItemList에서 상품 추출."""
        html = '''
        <script type="application/ld+json">
        {"@type": "ItemList", "itemListElement": [
            {"item": {"@type": "Product", "name": "덩크 로우 DQ8423-100",
             "url": "https://abcmart.a-rt.com/product/12345",
             "offers": {"price": "139000"}, "brand": {"name": "Nike"}}}
        ]}
        </script>
        '''
        results = _parse_products_from_html(html)
        assert len(results) == 1
        assert results[0]["product_id"] == "12345"
        assert results[0]["model_number"] == "DQ8423-100"
        assert results[0]["price"] == 139000

    def test_parse_empty_html(self):
        """상품 없는 HTML → 빈 리스트."""
        assert _parse_products_from_html("<html><body></body></html>") == []


class TestAbcMartParseSizes:
    """사이즈 파싱 테스트."""

    def setup_method(self):
        self.crawler = AbcMartCrawler()

    def test_parse_sold_out_excluded(self):
        """품절 사이즈 제외."""
        html = '''
        <option value="1" data-size="260" data-stock="5">260</option>
        <option value="2" data-size="270" data-stock="0">270 (품절)</option>
        <option value="3" data-size="280" data-stock="3">280</option>
        '''
        sizes = self.crawler._parse_sizes_from_html(html, 139000)
        assert len(sizes) == 2
        assert sizes[0].size == "260"
        assert sizes[1].size == "280"
        assert all(s.in_stock for s in sizes)
