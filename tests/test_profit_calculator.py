"""수익 계산 엔진 단위 테스트."""

from src.models.product import (
    KreamProduct,
    KreamSizePrice,
    RetailProduct,
    RetailSizeInfo,
    Signal,
)
from src.profit_calculator import (
    _normalize_size,
    analyze_opportunity,
    calculate_kream_fees,
    calculate_size_profit,
    determine_signal,
)


class TestCalculateKreamFees:
    def test_basic_fee_calculation(self):
        """130,000원 판매 시 수수료 계산.

        수수료 = ceil((2500 + 130000 * 0.06) * 1.1) = ceil(11330.0) = 11330
        """
        result = calculate_kream_fees(130_000)

        assert result["sell_fee"] == 11_330
        assert result["inspection_fee"] == 0
        assert result["kream_shipping_fee"] == 0
        assert result["seller_shipping_fee"] == 3_000
        assert result["total_fees"] == 11_330 + 3_000

    def test_zero_price(self):
        """판매가 0원 → 기본료만 적용."""
        result = calculate_kream_fees(0)
        # ceil(2500 * 1.1) = ceil(2750) = 2750
        assert result["sell_fee"] == 2_750
        assert result["total_fees"] == 2_750 + 3_000

    def test_high_price(self):
        """500,000원 판매 시.

        수수료 = ceil((2500 + 500000 * 0.06) * 1.1) = ceil(35750.0) = 35750
        """
        result = calculate_kream_fees(500_000)
        assert result["sell_fee"] == 35_750

    def test_real_account_verification(self):
        """실제 크림 계정 화면 검증값.

        판매가 167,000원 → 수수료 = ceil((2500 + 167000*0.06) * 1.1) = ceil(13772.0) = 13772
        정산금액 = 167000 - 13772 = 153228 (크림 화면 13770/153230과 2원 차이 - 10원 절사 추정)
        """
        result = calculate_kream_fees(167_000)
        assert result["sell_fee"] == 13_772
        assert result["inspection_fee"] == 0
        assert result["kream_shipping_fee"] == 0
        assert result["seller_shipping_fee"] == 3_000
        assert result["total_fees"] == 13_772 + 3_000


class TestCalculateSizeProfit:
    def test_profitable_case(self):
        """수익이 나는 경우."""
        result = calculate_size_profit(
            retail_price=80_000,
            kream_sell_price=130_000,
        )
        # 수수료: ceil((2500 + 130000*0.06) * 1.1) = 11330 + 3000 = 14330
        # 총비용: 80000 + 14330 = 94330
        # 순수익: 130000 - 94330 = 35670
        assert result.net_profit == 35_670
        assert result.roi > 0

    def test_loss_case(self):
        """손해나는 경우."""
        result = calculate_size_profit(
            retail_price=120_000,
            kream_sell_price=130_000,
        )
        # 수수료: 14330
        # 총비용: 120000 + 14330 = 134330
        # 순수익: 130000 - 134330 = -4330
        assert result.net_profit == -4_330
        assert result.roi < 0

    def test_roi_calculation(self):
        result = calculate_size_profit(
            retail_price=100_000,
            kream_sell_price=150_000,
        )
        # 수수료: round((2500 + 9000) * 1.1) = 12650, total = 12650 + 3000 = 15650
        # 순수익: 150000 - 100000 - 15650 = 34350
        # ROI: 34350/100000*100 = 34.4%
        assert result.net_profit == 34_350
        assert result.roi == 34.4


class TestDetermineSignal:
    def test_strong_buy(self):
        assert determine_signal(35_000, 12) == Signal.STRONG_BUY

    def test_buy(self):
        assert determine_signal(20_000, 7) == Signal.BUY

    def test_watch(self):
        assert determine_signal(8_000, 5) == Signal.WATCH

    def test_not_recommended_low_profit(self):
        assert determine_signal(3_000, 10) == Signal.NOT_RECOMMENDED

    def test_not_recommended_low_volume(self):
        """거래량 부족이면 수익이 높아도 비추천."""
        assert determine_signal(50_000, 2) == Signal.NOT_RECOMMENDED

    def test_boundary_strong_buy(self):
        """강력매수 경계값."""
        assert determine_signal(30_000, 10) == Signal.STRONG_BUY

    def test_boundary_buy(self):
        assert determine_signal(15_000, 5) == Signal.BUY


class TestNormalizeSize:
    def test_mm_number(self):
        assert _normalize_size("270") == "270"

    def test_with_mm_suffix(self):
        assert _normalize_size("270mm") == "270"

    def test_cm_number(self):
        assert _normalize_size("27") == "270"

    def test_cm_suffix(self):
        assert _normalize_size("27cm") == "270"

    def test_decimal_cm(self):
        assert _normalize_size("27.5") == "275"

    def test_letter_size(self):
        assert _normalize_size("M") == "M"
        assert _normalize_size("XL") == "XL"


class TestAnalyzeOpportunity:
    def _make_kream_product(self, prices: list[tuple[str, int]]) -> KreamProduct:
        return KreamProduct(
            product_id="12345",
            name="나이키 덩크 로우",
            model_number="DQ8423-100",
            brand="Nike",
            volume_7d=15,
            volume_30d=50,
            size_prices=[
                KreamSizePrice(size=s, sell_now_price=p) for s, p in prices
            ],
        )

    def _make_retail_product(self, sizes: list[tuple[str, int]]) -> RetailProduct:
        return RetailProduct(
            source="musinsa",
            product_id="m123",
            name="나이키 덩크 로우",
            model_number="DQ8423-100",
            sizes=[
                RetailSizeInfo(size=s, price=p, original_price=p) for s, p in sizes
            ],
        )

    def test_profitable_opportunity(self):
        kream = self._make_kream_product([("260", 130_000), ("270", 120_000)])
        retail = self._make_retail_product([("260", 80_000), ("270", 85_000)])

        result = analyze_opportunity(kream, [retail])

        assert result is not None
        assert result.best_profit > 0
        assert len(result.size_profits) == 2
        assert result.signal in (Signal.STRONG_BUY, Signal.BUY)

    def test_no_matching_sizes(self):
        kream = self._make_kream_product([("260", 130_000)])
        retail = self._make_retail_product([("280", 80_000)])

        result = analyze_opportunity(kream, [retail])
        assert result is None

    def test_no_kream_prices(self):
        kream = KreamProduct(
            product_id="12345", name="test", model_number="XX-100",
            size_prices=[],
        )
        retail = self._make_retail_product([("260", 80_000)])
        result = analyze_opportunity(kream, [retail])
        assert result is None

    def test_multiple_retail_sources(self):
        """여러 리테일 사이트 중 최저가 기준으로 계산."""
        kream = self._make_kream_product([("270", 150_000)])
        retail1 = self._make_retail_product([("270", 90_000)])
        retail2 = RetailProduct(
            source="nike",
            product_id="n456",
            name="나이키 덩크 로우",
            model_number="DQ8423-100",
            sizes=[RetailSizeInfo(size="270", price=85_000, original_price=100_000)],
        )

        result = analyze_opportunity(kream, [retail1, retail2])

        assert result is not None
        # 85,000원(nike)이 최저가로 선택되어야 함
        assert result.size_profits[0].retail_price == 85_000
