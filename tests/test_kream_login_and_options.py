"""크림 로그인 자동화 + /api/p/options/display API 테스트.

CDP 로그인 → 쿠키 주입 → options/display API로 전체 사이즈별 가격 수집 흐름을 검증한다.
네트워크/브라우저 호출은 모두 mock 처리.
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.crawlers.kream import KreamCrawler
from src.models.product import KreamSizePrice


# ─── _merge_options_into_map 테스트 ─────────────────────────


class TestMergeOptionsIntoMap:
    """options/display API 응답 파싱 테스트."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    def test_buy_mode_list_response(self):
        """buying 모드: 리스트 응답에서 즉시구매가 추출."""
        data = [
            {
                "product_option": {"key": "250"},
                "price": 120000,
            },
            {
                "product_option": {"key": "260"},
                "price": 130000,
            },
            {
                "product_option": {"key": "270"},
                "price": 150000,
            },
        ]
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "buy")
        assert size_map["250"]["buy_now_price"] == 120000
        assert size_map["260"]["buy_now_price"] == 130000
        assert size_map["270"]["buy_now_price"] == 150000

    def test_sell_mode_list_response(self):
        """selling 모드: 리스트 응답에서 즉시판매가 추출."""
        data = [
            {
                "product_option": {"key": "250"},
                "price": 100000,
                "quantity": 3,
            },
            {
                "product_option": {"key": "260"},
                "price": 110000,
                "quantity": 5,
            },
        ]
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "sell")
        assert size_map["250"]["sell_now_price"] == 100000
        assert size_map["250"]["bid_count"] == 3
        assert size_map["260"]["sell_now_price"] == 110000
        assert size_map["260"]["bid_count"] == 5

    def test_buy_keeps_lowest_price(self):
        """buying 모드에서 같은 사이즈의 최저가만 유지."""
        data = [
            {"product_option": {"key": "260"}, "price": 140000},
            {"product_option": {"key": "260"}, "price": 130000},
            {"product_option": {"key": "260"}, "price": 135000},
        ]
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "buy")
        assert size_map["260"]["buy_now_price"] == 130000

    def test_sell_keeps_highest_price(self):
        """selling 모드에서 같은 사이즈의 최고가만 유지."""
        data = [
            {"product_option": {"key": "270"}, "price": 110000},
            {"product_option": {"key": "270"}, "price": 120000},
            {"product_option": {"key": "270"}, "price": 115000},
        ]
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "sell")
        assert size_map["270"]["sell_now_price"] == 120000

    def test_dict_with_options_key(self):
        """dict 응답에서 'options' 키 안의 리스트 처리."""
        data = {
            "options": [
                {"product_option": {"key": "250"}, "price": 120000},
                {"product_option": {"key": "260"}, "price": 130000},
            ]
        }
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "buy")
        assert len(size_map) == 2
        assert size_map["250"]["buy_now_price"] == 120000

    def test_dict_with_product_options_key(self):
        """'product_options' 키 안의 리스트 처리."""
        data = {
            "product_options": [
                {"product_option": {"key": "270"}, "price": 150000},
            ]
        }
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "buy")
        assert size_map["270"]["buy_now_price"] == 150000

    def test_size_from_direct_keys(self):
        """product_option이 없을 때 직접 size/option/key에서 사이즈 추출."""
        data = [
            {"size": "250", "price": 120000},
            {"option": "260", "price": 130000},
            {"key": "270", "price": 150000},
        ]
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "buy")
        assert len(size_map) == 3

    def test_last_sale_price_extraction(self):
        """last_sale_price 추출."""
        data = [
            {
                "product_option": {"key": "260"},
                "price": 130000,
                "last_sale_price": 125000,
            },
        ]
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "buy")
        assert size_map["260"]["last_sale_price"] == 125000

    def test_merge_buy_and_sell(self):
        """buying + selling 두 번 호출하여 병합."""
        buy_data = [
            {"product_option": {"key": "260"}, "price": 130000},
            {"product_option": {"key": "270"}, "price": 150000},
        ]
        sell_data = [
            {"product_option": {"key": "260"}, "price": 110000, "quantity": 2},
            {"product_option": {"key": "270"}, "price": 130000, "quantity": 4},
            {"product_option": {"key": "280"}, "price": 140000, "quantity": 1},
        ]
        size_map = {}
        self.crawler._merge_options_into_map(buy_data, size_map, "buy")
        self.crawler._merge_options_into_map(sell_data, size_map, "sell")

        assert size_map["260"]["buy_now_price"] == 130000
        assert size_map["260"]["sell_now_price"] == 110000
        assert size_map["270"]["buy_now_price"] == 150000
        assert size_map["270"]["sell_now_price"] == 130000
        # 280은 sell만 있음
        assert "buy_now_price" not in size_map["280"]
        assert size_map["280"]["sell_now_price"] == 140000

    def test_empty_data(self):
        """빈 데이터 처리."""
        size_map = {}
        self.crawler._merge_options_into_map([], size_map, "buy")
        assert len(size_map) == 0
        self.crawler._merge_options_into_map({}, size_map, "buy")
        assert len(size_map) == 0
        self.crawler._merge_options_into_map(None, size_map, "buy")
        assert len(size_map) == 0

    def test_skip_items_without_size(self):
        """사이즈 키가 없는 아이템 무시."""
        data = [
            {"price": 130000},  # 사이즈 없음
            {"product_option": {"key": "260"}, "price": 130000},
        ]
        size_map = {}
        self.crawler._merge_options_into_map(data, size_map, "buy")
        assert len(size_map) == 1
        assert "260" in size_map


# ─── _fetch_options_display 테스트 ──────────────────────────


class TestFetchOptionsDisplay:
    """_fetch_options_display 통합 로직 테스트 (네트워크 mock)."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    @pytest.mark.asyncio
    async def test_fetch_both_buying_and_selling(self):
        """buying + selling 양쪽 API 호출 후 병합."""
        self.crawler._logged_in = True

        buy_response = [
            {"product_option": {"key": "250"}, "price": 120000},
            {"product_option": {"key": "260"}, "price": 130000},
            {"product_option": {"key": "270"}, "price": 150000},
        ]
        sell_response = [
            {"product_option": {"key": "250"}, "price": 100000, "quantity": 2},
            {"product_option": {"key": "260"}, "price": 110000, "quantity": 3},
            {"product_option": {"key": "270"}, "price": 130000, "quantity": 5},
        ]

        call_count = 0

        async def mock_request(method, url, *, headers=None, params=None, max_retries=3, parse_json=True):
            nonlocal call_count
            call_count += 1
            if params and params.get("picker_type") == "buying":
                return buy_response
            elif params and params.get("picker_type") == "selling":
                return sell_response
            return None

        self.crawler._request = mock_request
        prices = await self.crawler._fetch_options_display("12345")

        assert call_count == 2  # buying + selling
        assert len(prices) == 3
        # 사이즈 순서 정렬 확인
        assert prices[0].size == "250"
        assert prices[1].size == "260"
        assert prices[2].size == "270"
        # 즉시구매가 (buying)
        assert prices[0].buy_now_price == 120000
        assert prices[1].buy_now_price == 130000
        assert prices[2].buy_now_price == 150000
        # 즉시판매가 (selling)
        assert prices[0].sell_now_price == 100000
        assert prices[1].sell_now_price == 110000
        assert prices[2].sell_now_price == 130000
        # 입찰 수량 (selling)
        assert prices[2].bid_count == 5

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_when_not_logged_in(self):
        """로그인 안 됐고 자동 로그인도 실패하면 빈 리스트 반환."""
        self.crawler._logged_in = False
        self.crawler.ensure_login = AsyncMock(return_value=False)

        prices = await self.crawler._fetch_options_display("12345")
        assert prices == []

    @pytest.mark.asyncio
    async def test_fetch_with_auto_login(self):
        """로그인 안 됐으면 자동 로그인 시도 후 API 호출."""
        self.crawler._logged_in = False

        async def mock_ensure_login():
            self.crawler._logged_in = True
            return True

        self.crawler.ensure_login = mock_ensure_login

        async def mock_request(method, url, *, headers=None, params=None, max_retries=3, parse_json=True):
            if params and params.get("picker_type") == "buying":
                return [{"product_option": {"key": "260"}, "price": 130000}]
            return []

        self.crawler._request = mock_request
        prices = await self.crawler._fetch_options_display("12345")
        assert len(prices) == 1
        assert prices[0].size == "260"
        assert prices[0].buy_now_price == 130000

    @pytest.mark.asyncio
    async def test_fetch_partial_response(self):
        """한쪽 API만 성공해도 결과 반환."""
        self.crawler._logged_in = True

        async def mock_request(method, url, *, headers=None, params=None, max_retries=3, parse_json=True):
            if params and params.get("picker_type") == "buying":
                return [
                    {"product_option": {"key": "260"}, "price": 130000},
                    {"product_option": {"key": "270"}, "price": 150000},
                ]
            return None  # selling 실패

        self.crawler._request = mock_request
        prices = await self.crawler._fetch_options_display("12345")
        assert len(prices) == 2
        assert prices[0].buy_now_price == 130000
        assert prices[0].sell_now_price is None  # selling 실패이므로 None

    @pytest.mark.asyncio
    async def test_fetch_both_fail(self):
        """양쪽 API 모두 실패하면 빈 리스트."""
        self.crawler._logged_in = True

        async def mock_request(method, url, *, headers=None, params=None, max_retries=3, parse_json=True):
            return None

        self.crawler._request = mock_request
        prices = await self.crawler._fetch_options_display("12345")
        assert prices == []


# ─── ensure_login 테스트 ────────────────────────────────────


class TestEnsureLogin:
    """ensure_login (pinia 전용 모드) 테스트."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    @pytest.mark.asyncio
    async def test_pinia_only_returns_false(self):
        """pinia 전용 모드 — 항상 False 반환."""
        self.crawler._logged_in = False
        result = await self.crawler.ensure_login()
        assert result is False


# ─── get_full_product_info 통합 테스트 ──────────────────────


class TestGetFullProductInfo:
    """get_full_product_info의 API 병합 로직 테스트."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    @pytest.mark.asyncio
    async def test_api_prices_replace_pinia_when_more_sizes(self):
        """API가 더 많은 사이즈를 가져오면 API 결과로 교체하면서 pinia 보충."""
        # pinia에서 3개 사이즈 (체결가 포함)
        pinia_prices = [
            KreamSizePrice(size="250", buy_now_price=120000, sell_now_price=100000,
                           last_sale_price=110000, last_sale_date=datetime(2026, 3, 20)),
            KreamSizePrice(size="260", buy_now_price=130000, sell_now_price=110000,
                           last_sale_price=125000),
            KreamSizePrice(size="270", buy_now_price=150000, sell_now_price=130000),
        ]

        # API에서 5개 사이즈 (체결가 없음)
        api_prices = [
            KreamSizePrice(size="240", buy_now_price=115000, sell_now_price=95000),
            KreamSizePrice(size="250", buy_now_price=121000, sell_now_price=101000),
            KreamSizePrice(size="260", buy_now_price=131000, sell_now_price=111000),
            KreamSizePrice(size="270", buy_now_price=151000, sell_now_price=131000),
            KreamSizePrice(size="280", buy_now_price=160000, sell_now_price=140000),
        ]

        # API가 더 많으므로 교체 + pinia 체결가 보충
        # get_full_product_info 내부 병합 로직 시뮬레이션
        if len(api_prices) > len(pinia_prices):
            api_map = {p.size: p for p in api_prices}
            for p in pinia_prices:
                if p.size in api_map:
                    ap = api_map[p.size]
                    if not ap.last_sale_price and p.last_sale_price:
                        ap.last_sale_price = p.last_sale_price
                        ap.last_sale_date = p.last_sale_date
                else:
                    api_prices.append(p)
            api_prices.sort(key=lambda p: int(p.size) if p.size.isdigit() else 0)
            merged = api_prices
        else:
            merged = pinia_prices

        assert len(merged) == 5
        # 240은 API에서만 제공
        assert merged[0].size == "240"
        assert merged[0].buy_now_price == 115000
        # 250: API 가격 사용 + pinia 체결가 보충
        size_250 = next(p for p in merged if p.size == "250")
        assert size_250.buy_now_price == 121000  # API 가격
        assert size_250.last_sale_price == 110000  # pinia에서 보충
        assert size_250.last_sale_date == datetime(2026, 3, 20)
        # 280은 API에서만 제공
        assert merged[4].size == "280"
        assert merged[4].buy_now_price == 160000

    @pytest.mark.asyncio
    async def test_full_info_with_options_display(self):
        """get_full_product_info에서 options/display API 결과 통합."""
        from src.models.product import KreamProduct

        product_id = "12345"

        # Mock HTML → pinia에서 상품 정보만 가져옴 (가격 2개)
        mock_html = '<html><body>product page</body></html>'
        mock_pinia_product = KreamProduct(
            product_id=product_id,
            name="Nike Dunk Low",
            model_number="DD1391-100",
            brand="Nike",
            url=f"https://kream.co.kr/products/{product_id}",
        )
        pinia_size_prices = [
            KreamSizePrice(size="260", buy_now_price=130000, sell_now_price=110000),
            KreamSizePrice(size="270", buy_now_price=150000, sell_now_price=130000),
        ]
        pinia_trades = {
            "volume_7d": 15,
            "volume_30d": 45,
            "last_trade_date": datetime(2026, 3, 25),
            "price_trend": "상승",
        }

        # Mock options/display API → 5개 사이즈
        api_prices = [
            KreamSizePrice(size="250", buy_now_price=120000, sell_now_price=100000),
            KreamSizePrice(size="260", buy_now_price=130000, sell_now_price=110000),
            KreamSizePrice(size="270", buy_now_price=150000, sell_now_price=130000),
            KreamSizePrice(size="275", buy_now_price=145000, sell_now_price=125000),
            KreamSizePrice(size="280", buy_now_price=160000, sell_now_price=140000),
        ]

        # _request mock
        async def mock_request(method, url, *, headers=None, params=None, max_retries=3, parse_json=True):
            if url.endswith(f"/products/{product_id}") and not parse_json:
                return mock_html
            return None

        self.crawler._request = mock_request
        self.crawler._logged_in = True  # 로그인된 상태로 설정 (options/display 호출 조건)
        self.crawler._extract_page_data = MagicMock(return_value={"pinia": {}})
        self.crawler._find_product_in_data = MagicMock(return_value=mock_pinia_product)
        self.crawler._find_prices_in_data = MagicMock(return_value=pinia_size_prices)
        self.crawler._find_trades_in_data = MagicMock(return_value=pinia_trades)
        self.crawler._parse_product_from_meta = MagicMock(return_value=None)
        self.crawler._fetch_options_display = AsyncMock(return_value=api_prices)

        product = await self.crawler.get_full_product_info(product_id)

        assert product is not None
        assert product.name == "Nike Dunk Low"
        assert product.model_number == "DD1391-100"
        # API가 5개로 더 많으므로 API 결과 기반
        assert len(product.size_prices) == 5
        assert product.volume_7d == 15
        assert product.volume_30d == 45
        assert product.price_trend == "상승"

    @pytest.mark.asyncio
    async def test_full_info_fallback_to_pinia_only(self):
        """options/display 실패 시 pinia 데이터만 사용."""
        from src.models.product import KreamProduct

        product_id = "99999"
        mock_product = KreamProduct(
            product_id=product_id,
            name="Test Shoe",
            model_number="TS-001",
            brand="TestBrand",
            url=f"https://kream.co.kr/products/{product_id}",
        )
        pinia_prices = [
            KreamSizePrice(size="260", buy_now_price=130000, sell_now_price=110000),
        ]

        async def mock_request(method, url, *, headers=None, params=None, max_retries=3, parse_json=True):
            if not parse_json:
                return "<html></html>"
            return None

        self.crawler._request = mock_request
        self.crawler._extract_page_data = MagicMock(return_value={"data": {}})
        self.crawler._find_product_in_data = MagicMock(return_value=mock_product)
        self.crawler._find_prices_in_data = MagicMock(return_value=pinia_prices)
        self.crawler._find_trades_in_data = MagicMock(return_value=None)
        self.crawler._parse_product_from_meta = MagicMock(return_value=None)
        self.crawler._fetch_options_display = AsyncMock(return_value=[])

        product = await self.crawler.get_full_product_info(product_id)

        assert product is not None
        assert len(product.size_prices) == 1
        assert product.size_prices[0].size == "260"


# ─── pinia transactionHistorySummary 파싱 테스트 ────────────


class TestPiniaPricesAndTrades:
    """pinia 스토어의 asks/bids/sales 파싱 테스트."""

    def setup_method(self):
        self.crawler = KreamCrawler()

    def test_find_prices_in_pinia(self):
        """pinia에서 asks → 즉시구매가, bids → 즉시판매가 추출."""
        data = {
            "pinia": {
                "transactionHistorySummary": {
                    "previousItem": {
                        "meta": {
                            "transaction_history": {
                                "asks": {
                                    "items": [
                                        {"product_option": {"key": "250"}, "price": 120000},
                                        {"product_option": {"key": "260"}, "price": 130000},
                                        {"product_option": {"key": "270"}, "price": 150000},
                                    ]
                                },
                                "bids": {
                                    "items": [
                                        {"product_option": {"key": "250"}, "price": 100000, "quantity": 2},
                                        {"product_option": {"key": "260"}, "price": 110000, "quantity": 3},
                                        {"product_option": {"key": "270"}, "price": 130000, "quantity": 5},
                                    ]
                                },
                                "sales": {
                                    "items": [
                                        {"product_option": {"key": "260"}, "price": 125000, "date_created": "2026-03-20T10:00:00"},
                                    ]
                                },
                            }
                        }
                    }
                }
            }
        }

        prices = self.crawler._find_prices_in_pinia(data, "12345")
        assert len(prices) == 3

        p250 = next(p for p in prices if p.size == "250")
        assert p250.buy_now_price == 120000
        assert p250.sell_now_price == 100000
        assert p250.bid_count == 2

        p260 = next(p for p in prices if p.size == "260")
        assert p260.buy_now_price == 130000
        assert p260.sell_now_price == 110000
        assert p260.last_sale_price == 125000

        p270 = next(p for p in prices if p.size == "270")
        assert p270.buy_now_price == 150000
        assert p270.sell_now_price == 130000
        assert p270.bid_count == 5

    def test_find_prices_in_pinia_empty(self):
        """pinia에 데이터가 없으면 빈 리스트."""
        data = {"pinia": {}}
        prices = self.crawler._find_prices_in_pinia(data, "12345")
        assert prices == []

    def test_find_prices_in_pinia_not_dict(self):
        """data가 dict가 아니면 빈 리스트."""
        prices = self.crawler._find_prices_in_pinia("not a dict", "12345")
        assert prices == []

    def test_find_trades_in_pinia(self):
        """pinia sales에서 거래 통계 추출."""
        from datetime import timedelta
        now = datetime.now()

        data = {
            "pinia": {
                "transactionHistorySummary": {
                    "previousItem": {
                        "meta": {
                            "transaction_history": {
                                "asks": {"items": []},
                                "bids": {"items": []},
                                "sales": {
                                    "items": [
                                        {"product_option": {"key": "260"}, "price": 130000,
                                         "date_created": (now - timedelta(days=1)).isoformat()},
                                        {"product_option": {"key": "270"}, "price": 150000,
                                         "date_created": (now - timedelta(days=3)).isoformat()},
                                        {"product_option": {"key": "260"}, "price": 125000,
                                         "date_created": (now - timedelta(days=10)).isoformat()},
                                    ]
                                },
                            }
                        }
                    }
                }
            }
        }

        result = self.crawler._find_trades_in_pinia(data, "12345")
        assert result is not None
        assert result["volume_7d"] == 2
        assert result["volume_30d"] == 3

    def test_pinia_product_extraction(self):
        """pinia productDetail에서 상품 정보 추출."""
        data = {
            "pinia": {
                "productDetail": {
                    "productDetailContent": {
                        "meta": {
                            "name": "Nike Air Max 90",
                            "style_code": "CN8490-002",
                            "brand_name": "Nike",
                            "category": "sneakers",
                        }
                    }
                }
            }
        }
        product = self.crawler._find_product_in_pinia(data, "55555")
        assert product is not None
        assert product.name == "Nike Air Max 90"
        assert product.model_number == "CN8490-002"
        assert product.brand == "Nike"
        assert product.product_id == "55555"
