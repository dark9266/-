"""크림 __NUXT_DATA__ 파서 단위 테스트.

실제 네트워크 호출 없이 _unflatten_nuxt와 데이터 추출 로직을 검증한다.
"""

import json
import pytest
from src.crawlers.kream import KreamCrawler, _unflatten_nuxt


# ─── devalue unflatten 테스트 ────────────────────────────

class TestUnflattenNuxt:
    """_unflatten_nuxt 역직렬화 테스트."""

    def test_primitive_string(self):
        # index 0 = "hello"
        assert _unflatten_nuxt(["hello"]) == "hello"

    def test_primitive_number(self):
        assert _unflatten_nuxt([42]) == 42

    def test_primitive_bool(self):
        assert _unflatten_nuxt([True]) is True
        assert _unflatten_nuxt([False]) is False

    def test_primitive_null(self):
        assert _unflatten_nuxt([None]) is None

    def test_simple_object(self):
        # [root_obj, "hello", 42]
        # root_obj = {"name": 1, "age": 2}  → name→"hello", age→42
        parsed = [{"name": 1, "age": 2}, "hello", 42]
        result = _unflatten_nuxt(parsed)
        assert result == {"name": "hello", "age": 42}

    def test_simple_array(self):
        # [root_arr, 10, 20, 30]
        # root_arr = [1, 2, 3]  → [10, 20, 30]
        parsed = [[1, 2, 3], 10, 20, 30]
        result = _unflatten_nuxt(parsed)
        assert result == [10, 20, 30]

    def test_nested_object(self):
        # index 0: {"product": 1}
        # index 1: {"name": 2, "price": 3}
        # index 2: "Nike Air"
        # index 3: 120000
        parsed = [{"product": 1}, {"name": 2, "price": 3}, "Nike Air", 120000]
        result = _unflatten_nuxt(parsed)
        assert result == {"product": {"name": "Nike Air", "price": 120000}}

    def test_reactive_wrapper(self):
        # ["Reactive", 1] wraps index 1
        parsed = [["Reactive", 1], {"name": 2}, "test"]
        result = _unflatten_nuxt(parsed)
        assert result == {"name": "test"}

    def test_shallow_ref_wrapper(self):
        parsed = [["ShallowRef", 1], "value"]
        result = _unflatten_nuxt(parsed)
        assert result == "value"

    def test_empty_input(self):
        assert _unflatten_nuxt([]) is None
        assert _unflatten_nuxt(None) is None

    def test_complex_structure(self):
        """실제 Nuxt 페이지와 유사한 복잡한 구조."""
        parsed = [
            ["Reactive", 1],           # 0: root → Reactive(1)
            {"data": 2},               # 1: {data: →2}
            {"product": 3, "sizes": 6},  # 2: {product: →3, sizes: →6}
            {"id": 4, "name": 5, "style_code": 8},  # 3: product
            12345,                      # 4: id
            "Nike Dunk Low",            # 5: name
            [7],                        # 6: sizes array → [→7]
            {"size": 9, "buy_now_price": 10, "sell_now_price": 11},  # 7: size item
            "DD1391-100",               # 8: style_code
            "270",                      # 9: size
            150000,                     # 10: buy_now_price
            130000,                     # 11: sell_now_price
        ]
        result = _unflatten_nuxt(parsed)
        assert result["data"]["product"]["id"] == 12345
        assert result["data"]["product"]["name"] == "Nike Dunk Low"
        assert result["data"]["product"]["style_code"] == "DD1391-100"
        assert result["data"]["sizes"][0]["size"] == "270"
        assert result["data"]["sizes"][0]["buy_now_price"] == 150000
        assert result["data"]["sizes"][0]["sell_now_price"] == 130000


# ─── __NUXT_DATA__ 추출 테스트 ────────────────────────────

class TestExtractNuxtData:
    """HTML에서 __NUXT_DATA__ 추출 테스트."""

    def test_basic_extraction(self):
        payload = json.dumps(["hello"])
        html = f'<html><script id="__NUXT_DATA__" type="application/json">{payload}</script></html>'
        result = KreamCrawler._extract_nuxt_data(html)
        assert result == "hello"

    def test_with_data_ssr(self):
        payload = json.dumps([{"key": 1}, "value"])
        html = f'<html><script id="__NUXT_DATA__" type="application/json" data-ssr="true">{payload}</script></html>'
        result = KreamCrawler._extract_nuxt_data(html)
        assert result == {"key": "value"}

    def test_no_nuxt_data(self):
        html = "<html><body>no data</body></html>"
        assert KreamCrawler._extract_nuxt_data(html) is None

    def test_invalid_json(self):
        html = '<html><script id="__NUXT_DATA__" type="application/json">{invalid}</script></html>'
        assert KreamCrawler._extract_nuxt_data(html) is None


# ─── 상품 데이터 파싱 테스트 ─────────────────────────────

class TestProductParsing:
    """_parse_product_data 테스트."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    def test_basic_product(self):
        data = {
            "id": 12345,
            "name": "Nike Dunk Low Retro White Black",
            "style_code": "DD1391-100",
            "brand": {"name": "Nike"},
            "image_url": "https://kream-phinf.pstatic.net/...",
        }
        product = self.crawler._parse_product_data(data, "12345")
        assert product is not None
        assert product.product_id == "12345"
        assert product.name == "Nike Dunk Low Retro White Black"
        assert product.model_number == "DD1391-100"
        assert product.brand == "Nike"

    def test_nested_product(self):
        data = {"product": {
            "name": "Jordan 1 Retro High",
            "styleCode": "DZ5485-612",
            "brandName": "Jordan",
        }}
        product = self.crawler._parse_product_data(data, "99999")
        assert product is not None
        assert product.name == "Jordan 1 Retro High"
        assert product.model_number == "DZ5485-612"

    def test_empty_name(self):
        data = {"id": 123}
        product = self.crawler._parse_product_data(data, "123")
        assert product is None


# ─── 사이즈별 시세 파싱 테스트 ────────────────────────────

class TestSizePriceParsing:
    """사이즈별 가격 파싱 테스트."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    def test_parse_size_list(self):
        items = [
            {"size": "250", "buy_now_price": 120000, "sell_now_price": 100000},
            {"size": "260", "buy_now_price": 130000, "sell_now_price": 110000},
            {"size": "270", "buy_now_price": 150000, "sell_now_price": 130000},
        ]
        prices = self.crawler._parse_size_list(items)
        assert len(prices) == 3
        assert prices[0].size == "250"
        assert prices[0].buy_now_price == 120000
        assert prices[0].sell_now_price == 100000
        assert prices[2].size == "270"
        assert prices[2].buy_now_price == 150000

    def test_parse_size_list_various_keys(self):
        """다양한 키 이름에 대응."""
        items = [
            {"name": "260", "lowestAsk": 130000, "highestBid": 110000},
            {"option": "270", "buyNowPrice": 150000, "sellNowPrice": 130000},
        ]
        prices = self.crawler._parse_size_list(items)
        assert len(prices) == 2
        assert prices[0].size == "260"
        assert prices[0].buy_now_price == 130000
        assert prices[0].sell_now_price == 110000

    def test_parse_size_map(self):
        data = {
            "250": {"buyNowPrice": 120000, "sellNowPrice": 100000},
            "260": {"buyNowPrice": 130000, "sellNowPrice": 110000},
        }
        prices = self.crawler._parse_size_map(data)
        assert len(prices) == 2
        sizes = {p.size for p in prices}
        assert "250" in sizes
        assert "260" in sizes

    def test_find_prices_in_nuxt_data(self):
        """__NUXT_DATA__ 역직렬화 결과에서 시세 탐색."""
        data = {
            "data": {
                "product": {"id": 12345, "name": "Test"},
                "sizes": [
                    {"size": "260", "buy_now_price": 130000, "sell_now_price": 110000},
                    {"size": "270", "buy_now_price": 150000, "sell_now_price": 130000},
                ],
            }
        }
        prices = self.crawler._find_prices_in_data(data, "12345")
        assert len(prices) == 2
        assert prices[0].size == "260"
        assert prices[1].buy_now_price == 150000

    def test_skip_empty_prices(self):
        items = [
            {"size": "250"},  # 가격 없음 → 무시
            {"size": "260", "buy_now_price": 130000},
        ]
        prices = self.crawler._parse_size_list(items)
        assert len(prices) == 1
        assert prices[0].size == "260"


# ─── 거래 내역 파싱 테스트 ───────────────────────────────

class TestTradeHistory:
    """거래 내역 파싱 테스트."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    def test_calculate_trade_stats(self):
        from datetime import timedelta
        now = datetime.now()
        items = [
            {"price": 150000, "date": (now - timedelta(days=1)).isoformat()},
            {"price": 148000, "date": (now - timedelta(days=2)).isoformat()},
            {"price": 145000, "date": (now - timedelta(days=5)).isoformat()},
            {"price": 142000, "date": (now - timedelta(days=10)).isoformat()},
            {"price": 140000, "date": (now - timedelta(days=15)).isoformat()},
            {"price": 138000, "date": (now - timedelta(days=20)).isoformat()},
        ]
        result = self.crawler._calculate_trade_stats(items, "12345")
        assert result["volume_7d"] == 3
        assert result["volume_30d"] == 6
        assert result["last_trade_date"] is not None

    def test_find_trades_in_data(self):
        from datetime import timedelta
        now = datetime.now()
        data = {
            "sales": [
                {"price": 150000, "created_at": (now - timedelta(days=1)).isoformat()},
                {"price": 148000, "created_at": (now - timedelta(days=3)).isoformat()},
            ]
        }
        result = self.crawler._find_trades_in_data(data, "12345")
        assert result is not None
        assert result["volume_7d"] == 2

    def test_empty_trades(self):
        result = self.crawler._calculate_trade_stats([], "12345")
        assert result["volume_7d"] == 0
        assert result["volume_30d"] == 0


# ─── 검색 결과 파싱 테스트 ──────────────────────────────

class TestSearchParsing:
    def setup_method(self):
        self.crawler = KreamCrawler()

    def test_extract_product_summary(self):
        item = {
            "id": 12345,
            "name": "Nike Dunk Low",
            "brand": {"name": "Nike"},
        }
        result = self.crawler._extract_product_summary(item)
        assert result is not None
        assert result["product_id"] == "12345"
        assert result["name"] == "Nike Dunk Low"
        assert result["brand"] == "Nike"

    def test_extract_product_summary_alt_keys(self):
        item = {
            "productId": "67890",
            "translated_name": "조던 1",
            "brandName": "Jordan",
        }
        result = self.crawler._extract_product_summary(item)
        assert result["product_id"] == "67890"
        assert result["name"] == "조던 1"

    def test_extract_no_id(self):
        item = {"name": "test"}
        result = self.crawler._extract_product_summary(item)
        assert result is None


# ─── deep_find 유틸리티 테스트 ───────────────────────────

class TestDeepFind:
    def test_deep_find_shallow(self):
        data = {"products": [1, 2, 3]}
        assert KreamCrawler._deep_find(data, "products") == [[1, 2, 3]]

    def test_deep_find_nested(self):
        data = {"a": {"b": {"products": [1, 2]}}}
        results = KreamCrawler._deep_find(data, "products")
        assert [1, 2] in results

    def test_deep_find_dict_with_keys(self):
        data = {
            "items": [
                {"id": 1, "name": "a"},
                {"id": 2, "name": "b"},
            ]
        }
        results = KreamCrawler._deep_find_dict(data, {"id", "name"})
        assert len(results) == 2


# ─── _to_int 유틸리티 테스트 ────────────────────────────

class TestToInt:
    def test_int(self):
        assert KreamCrawler._to_int(100) == 100

    def test_zero(self):
        assert KreamCrawler._to_int(0) is None

    def test_negative(self):
        assert KreamCrawler._to_int(-5) is None

    def test_float(self):
        assert KreamCrawler._to_int(99.9) == 99

    def test_string(self):
        assert KreamCrawler._to_int("132,000원") == 132000

    def test_none(self):
        assert KreamCrawler._to_int(None) is None

    def test_bool(self):
        assert KreamCrawler._to_int(True) is None


# ─── 통합 시나리오: __NUXT_DATA__ → 전체 데이터 추출 ─────

class TestEndToEnd:
    """실제 __NUXT_DATA__ 구조를 시뮬레이션한 통합 테스트."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    def test_full_nuxt_data_parsing(self):
        """Nuxt 페이지에서 상품+시세+거래내역 모두 추출하는 시나리오."""
        from datetime import timedelta

        now = datetime.now()

        # 실제 __NUXT_DATA__ 구조를 devalue 형식으로 시뮬레이션
        # (unflatten 후의 결과를 직접 사용)
        page_data = {
            "data": {
                "product": {
                    "id": 7890,
                    "name": "Nike Air Force 1 '07 Low White",
                    "style_code": "CW2288-111",
                    "brand": {"name": "Nike"},
                    "image_url": "https://example.com/image.jpg",
                },
                "sizes": [
                    {"size": "250", "buy_now_price": 109000, "sell_now_price": 89000},
                    {"size": "260", "buy_now_price": 115000, "sell_now_price": 95000},
                    {"size": "270", "buy_now_price": 120000, "sell_now_price": 100000},
                    {"size": "275", "buy_now_price": 125000, "sell_now_price": 105000},
                    {"size": "280", "buy_now_price": 130000, "sell_now_price": 110000},
                ],
                "sales": [
                    {"price": 115000, "created_at": (now - timedelta(days=1)).isoformat()},
                    {"price": 112000, "created_at": (now - timedelta(days=2)).isoformat()},
                    {"price": 110000, "created_at": (now - timedelta(days=5)).isoformat()},
                    {"price": 108000, "created_at": (now - timedelta(days=8)).isoformat()},
                    {"price": 105000, "created_at": (now - timedelta(days=15)).isoformat()},
                ],
            }
        }

        # 상품 정보 추출
        product = self.crawler._find_product_in_data(page_data, "7890")
        assert product is not None
        assert product.name == "Nike Air Force 1 '07 Low White"
        assert product.model_number == "CW2288-111"
        assert product.brand == "Nike"

        # 사이즈별 시세 추출
        prices = self.crawler._find_prices_in_data(page_data, "7890")
        assert len(prices) == 5
        assert prices[0].size == "250"
        assert prices[0].buy_now_price == 109000
        assert prices[0].sell_now_price == 89000
        assert prices[4].size == "280"
        assert prices[4].buy_now_price == 130000

        # 거래 내역 추출
        trades = self.crawler._find_trades_in_data(page_data, "7890")
        assert trades is not None
        assert trades["volume_7d"] == 3  # 1일, 2일, 5일 전
        assert trades["volume_30d"] == 5
        assert trades["last_trade_date"] is not None

    def test_nuxt_data_html_roundtrip(self):
        """devalue 형식 HTML → unflatten → 데이터 추출 전체 경로."""
        # devalue 직렬화된 JSON 배열
        nuxt_payload = [
            {"data": 1},                    # 0: root
            {"product": 2, "sizes": 5},      # 1: data
            {"id": 3, "name": 4, "style_code": 8},  # 2: product
            99999,                           # 3: id
            "Test Sneaker",                  # 4: name
            [6],                             # 5: sizes array
            {"size": 7, "buy_now_price": 9, "sell_now_price": 10},  # 6
            "270",                           # 7: size
            "AB1234-001",                    # 8: style_code
            200000,                          # 9: buy_now_price
            180000,                          # 10: sell_now_price
        ]
        html = (
            '<html><head></head><body>'
            f'<script id="__NUXT_DATA__" type="application/json" data-ssr="true">'
            f'{json.dumps(nuxt_payload)}'
            f'</script>'
            '</body></html>'
        )

        # __NUXT_DATA__ 추출 + unflatten
        data = KreamCrawler._extract_nuxt_data(html)
        assert data is not None

        # 상품 정보 추출
        product = self.crawler._find_product_in_data(data, "99999")
        assert product is not None
        assert product.name == "Test Sneaker"
        assert product.model_number == "AB1234-001"

        # 사이즈별 시세 추출
        prices = self.crawler._find_prices_in_data(data, "99999")
        assert len(prices) == 1
        assert prices[0].size == "270"
        assert prices[0].buy_now_price == 200000
        assert prices[0].sell_now_price == 180000


from datetime import datetime
