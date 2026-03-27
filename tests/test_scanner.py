"""스캐너 기본 테스트."""

from src.scanner import ScanResult
from src.models.product import ProfitOpportunity, KreamProduct, Signal


class TestScanResult:
    def test_initial_state(self):
        result = ScanResult()
        assert result.scanned_products == 0
        assert result.matched_products == 0
        assert result.opportunities == []
        assert result.price_changes == []
        assert result.errors == []
        assert result.profitable_count == 0

    def test_profitable_count(self):
        result = ScanResult()

        op1 = ProfitOpportunity(
            kream_product=KreamProduct(product_id="1", name="A", model_number="X"),
            retail_products=[], size_profits=[],
            best_profit=35000, signal=Signal.STRONG_BUY,
        )
        op2 = ProfitOpportunity(
            kream_product=KreamProduct(product_id="2", name="B", model_number="Y"),
            retail_products=[], size_profits=[],
            best_profit=20000, signal=Signal.BUY,
        )
        op3 = ProfitOpportunity(
            kream_product=KreamProduct(product_id="3", name="C", model_number="Z"),
            retail_products=[], size_profits=[],
            best_profit=6000, signal=Signal.WATCH,
        )

        result.opportunities = [op1, op2, op3]
        assert result.profitable_count == 2  # STRONG_BUY + BUY만
