"""Mock 기반 파이프라인 통합 테스트.

실제 API 호출 없이 전체 데이터 흐름 검증:
무신사 응답 → 크림 시세 → 수익 계산 → 알림 발송 여부
"""

from src.matcher import model_numbers_match
from src.models.product import (
    AutoScanOpportunity,
    KreamProduct,
    KreamSizePrice,
    Signal,
)
from src.profit_calculator import (
    analyze_auto_scan_opportunity,
    determine_signal,
)


def _make_kream(product_id, model, sell_price, volume_7d=15, last_sale=None):
    """테스트용 크림 상품 생성."""
    return KreamProduct(
        product_id=product_id,
        name=f"크림 {model}",
        model_number=model,
        brand="Nike",
        volume_7d=volume_7d,
        size_prices=[
            KreamSizePrice(
                size="270",
                sell_now_price=sell_price,
                last_sale_price=last_sale or sell_price,
            ),
        ],
    )


class TestPipelineScenarios:
    """시나리오 A~E: 파이프라인 전 구간 통합 검증."""

    def test_scenario_a_profitable_sends_alert(self):
        """정상 수익 상품 → BUY 이상 → 알림 발송 대상."""
        kream = _make_kream("K1", "DQ8423-100", 150_000, volume_7d=10)
        musinsa_sizes = {"270": (80_000, True)}

        opp = analyze_auto_scan_opportunity(
            kream_product=kream,
            musinsa_sizes=musinsa_sizes,
            musinsa_url="https://musinsa.com/1",
            musinsa_name="나이키 덩크 로우",
        )

        assert opp is not None
        assert opp.best_confirmed_profit > 0
        signal = determine_signal(opp.best_confirmed_profit, opp.volume_7d)
        assert signal in (Signal.STRONG_BUY, Signal.BUY)

    def test_scenario_b_low_profit_blocks_alert(self):
        """수익 부족 → WATCH/NOT_RECOMMENDED → 알림 차단."""
        # 크림 85,000 - 무신사 80,000 = 매우 낮은 마진 (수수료 빼면 마이너스)
        kream = _make_kream("K2", "AB1234-001", 85_000, volume_7d=10)
        musinsa_sizes = {"270": (80_000, True)}

        opp = analyze_auto_scan_opportunity(
            kream_product=kream,
            musinsa_sizes=musinsa_sizes,
            musinsa_url="",
            musinsa_name="",
        )

        if opp is not None:
            signal = determine_signal(opp.best_confirmed_profit, opp.volume_7d)
            assert signal not in (Signal.STRONG_BUY, Signal.BUY)

    def test_scenario_c_offline_model_mismatch(self):
        """모델번호 불일치 → 매칭 실패로 처리."""
        assert model_numbers_match("DQ8423-100", "DQ8423-200") is False
        assert model_numbers_match("25-002", "CU9225-002") is False

    def test_scenario_d_zero_volume_blocks(self):
        """거래량 0건 → NOT_RECOMMENDED → 알림 차단."""
        signal = determine_signal(50_000, 0)
        assert signal == Signal.NOT_RECOMMENDED
        assert signal not in (Signal.STRONG_BUY, Signal.BUY)

    def test_scenario_e_no_sizes_no_opportunity(self):
        """크림 사이즈 가격 없음 → 수익분석 불가."""
        kream = KreamProduct(
            product_id="K3",
            name="빈상품",
            model_number="XX0000-000",
            brand="Nike",
            size_prices=[],
        )
        opp = analyze_auto_scan_opportunity(
            kream_product=kream,
            musinsa_sizes={"270": (80_000, True)},
            musinsa_url="",
            musinsa_name="",
        )
        assert opp is None


class TestPipelineProfitCalculation:
    """수익 계산 정확성 E2E 검증."""

    def test_exact_profit_calculation(self):
        """수수료 계산 후 순수익이 정확한지 검증.

        크림 판매가 150,000 → 수수료 = ceil((2500 + 9000) * 1.1) = 12,650
        총수수료 = 12,650 + 3,000 = 15,650
        순수익 = 150,000 - 80,000 - 15,650 = 54,350
        """
        kream = _make_kream("K4", "TEST-001", 150_000, volume_7d=10)
        musinsa_sizes = {"270": (80_000, True)}

        opp = analyze_auto_scan_opportunity(
            kream_product=kream,
            musinsa_sizes=musinsa_sizes,
            musinsa_url="",
            musinsa_name="",
        )

        assert opp is not None
        assert opp.best_confirmed_profit == 54_350

    def test_negative_profit_still_returns_opportunity(self):
        """마이너스 수익도 opportunity 객체는 반환됨 (필터링은 호출자 책임)."""
        kream = _make_kream("K5", "TEST-002", 85_000, volume_7d=10)
        musinsa_sizes = {"270": (80_000, True)}

        opp = analyze_auto_scan_opportunity(
            kream_product=kream,
            musinsa_sizes=musinsa_sizes,
            musinsa_url="",
            musinsa_name="",
        )

        # 크림 85,000 - 무신사 80,000 - 수수료 ≈ -3,610 (마이너스)
        assert opp is not None
        assert opp.best_confirmed_profit < 0

    def test_empty_musinsa_sizes_no_opportunity(self):
        """무신사 사이즈 없음 → opportunity None."""
        kream = _make_kream("K6", "TEST-003", 150_000)
        opp = analyze_auto_scan_opportunity(
            kream_product=kream,
            musinsa_sizes={},
            musinsa_url="",
            musinsa_name="",
        )
        assert opp is None

    def test_size_mismatch_no_opportunity(self):
        """크림 270 vs 무신사 280 → 사이즈 미매칭 → 수익분석 불가."""
        kream = _make_kream("K7", "TEST-004", 150_000)
        musinsa_sizes = {"280": (80_000, True)}

        opp = analyze_auto_scan_opportunity(
            kream_product=kream,
            musinsa_sizes=musinsa_sizes,
            musinsa_url="",
            musinsa_name="",
        )
        assert opp is None
