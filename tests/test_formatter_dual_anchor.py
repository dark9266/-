"""Phase 1 — format_profit_alert dual-anchor embed 검증."""

from datetime import datetime

from src.discord_bot.formatter import format_profit_alert
from src.models.product import (
    KreamProduct,
    KreamSizePrice,
    ProfitOpportunity,
    RetailProduct,
    RetailSizeInfo,
    Signal,
    SizeProfitResult,
)


def _make_opportunity(*, last_sale_price: int = 105000) -> ProfitOpportunity:
    """체결가 105k (sell_now 102k 보다 +3k) 인 표준 케이스."""
    sp = SizeProfitResult(
        size="270",
        retail_price=80000,
        kream_sell_price=102000,
        sell_fee=9482,
        inspection_fee=0,
        kream_shipping_fee=0,
        seller_shipping_fee=3000,
        total_cost=92482,
        net_profit=9518,
        roi=11.9,
        signal=Signal.WATCH,
        kream_last_sale_price=last_sale_price,
        kream_buy_now_price=110000,
        net_profit_last_sale=12260 if last_sale_price > 0 else 0,
        roi_last_sale=15.3 if last_sale_price > 0 else 0.0,
    )
    return ProfitOpportunity(
        kream_product=KreamProduct(
            product_id="123",
            name="Test Shoe",
            model_number="TEST-001",
            volume_7d=10,
            size_prices=[KreamSizePrice(size="270", sell_now_price=102000)],
        ),
        retail_products=[
            RetailProduct(
                source="test",
                product_id="r1",
                name="Test",
                model_number="TEST-001",
                url="https://test.example.com/p/1",
                sizes=[RetailSizeInfo(size="270", price=80000, in_stock=True)],
            ),
        ],
        size_profits=[sp],
        best_profit=9518,
        best_roi=11.9,
        signal=Signal.WATCH,
        detected_at=datetime(2026, 5, 1, 13, 0, 0),
    )


def test_embed_has_dual_anchor_field():
    """체결가 등록 시 마진 정보가 embed 필드에 포함."""
    op = _make_opportunity()
    embed = format_profit_alert(op)

    field_names = [f.name for f in embed.fields]
    assert any("체결가" in n for n in field_names), f"체결가 필드 없음: {field_names}"


def test_embed_dual_anchor_value_format():
    """체결가 라인에 마진 + 가격 둘 다 포함."""
    op = _make_opportunity()
    embed = format_profit_alert(op)

    dual_field = next((f for f in embed.fields if "체결가" in f.name), None)
    assert dual_field is not None
    # 사이즈 + 마진 + 체결가 모두 표시
    assert "270" in dual_field.value
    assert "12,260" in dual_field.value or "+12,260" in dual_field.value
    assert "105,000" in dual_field.value


def test_embed_no_last_sale_shows_na():
    """체결가 0 인 사이즈는 'N/A' 표시."""
    op = _make_opportunity(last_sale_price=0)
    embed = format_profit_alert(op)

    dual_field = next((f for f in embed.fields if "체결가" in f.name), None)
    assert dual_field is not None
    assert "N/A" in dual_field.value
