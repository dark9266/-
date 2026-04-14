"""디스코드 Embed 포매터 테스트."""

from datetime import datetime

import discord

from src.discord_bot.formatter import (
    format_daily_report,
    format_help,
    format_price_change_alert,
    format_product_detail,
    format_profit_alert,
    format_status,
)
from src.models.product import (
    KreamProduct,
    KreamSizePrice,
    ProfitOpportunity,
    RetailProduct,
    RetailSizeInfo,
    Signal,
    SizeProfitResult,
)
from src.price_tracker import PriceChange


def _make_opportunity() -> ProfitOpportunity:
    kream = KreamProduct(
        product_id="12345",
        name="나이키 덩크 로우 레트로 화이트 블랙",
        model_number="DQ8423-100",
        brand="Nike",
        image_url="https://example.com/img.jpg",
        url="https://kream.co.kr/products/12345",
        volume_7d=15,
        volume_30d=50,
        last_trade_date=datetime(2026, 3, 20),
        price_trend="상승",
        size_prices=[
            KreamSizePrice(size="260", sell_now_price=130000, bid_count=5),
            KreamSizePrice(size="270", sell_now_price=120000, bid_count=3),
        ],
    )
    retail = RetailProduct(
        source="musinsa",
        product_id="m123",
        name="나이키 덩크 로우",
        model_number="DQ8423-100",
        brand="Nike",
        url="https://www.musinsa.com/products/m123",
        sizes=[
            RetailSizeInfo(size="260", price=80000, original_price=110000,
                           discount_type="회원등급", discount_rate=0.27),
            RetailSizeInfo(size="270", price=85000, original_price=110000,
                           discount_type="회원등급", discount_rate=0.23),
        ],
    )
    size_profits = [
        SizeProfitResult(
            size="260", retail_price=80000, kream_sell_price=130000,
            sell_fee=8580, inspection_fee=2500, kream_shipping_fee=3500,
            seller_shipping_fee=3000, total_cost=97580,
            net_profit=32420, roi=40.5, signal=Signal.STRONG_BUY,
            in_stock=True, bid_count=5,
        ),
        SizeProfitResult(
            size="270", retail_price=85000, kream_sell_price=120000,
            sell_fee=7920, inspection_fee=2500, kream_shipping_fee=3500,
            seller_shipping_fee=3000, total_cost=101920,
            net_profit=18080, roi=21.3, signal=Signal.BUY,
            in_stock=True, bid_count=3,
        ),
    ]
    return ProfitOpportunity(
        kream_product=kream,
        retail_products=[retail],
        size_profits=size_profits,
        best_profit=32420,
        best_roi=40.5,
        signal=Signal.STRONG_BUY,
        detected_at=datetime(2026, 3, 24, 10, 30),
    )


class TestFormatProfitAlert:
    def test_creates_embed(self):
        op = _make_opportunity()
        embed = format_profit_alert(op)

        assert isinstance(embed, discord.Embed)
        assert "강력매수" in embed.title
        assert "DQ8423-100" in embed.fields[0].value
        assert "32,420" in embed.fields[0].value

    def test_has_all_required_fields(self):
        op = _make_opportunity()
        embed = format_profit_alert(op)

        field_names = [f.name for f in embed.fields]
        assert "상품 정보" in field_names
        assert "구매처" in field_names
        assert "사이즈별 수익" in field_names
        assert "거래 활성도" in field_names
        assert "수수료 상세 (최고수익 기준)" in field_names

    def test_signal_color(self):
        op = _make_opportunity()
        embed = format_profit_alert(op)
        assert embed.color.value == 0xFF0000  # STRONG_BUY = 빨강


class TestFormatPriceChangeAlert:
    def test_creates_embed(self):
        changes = [
            PriceChange(
                product_name="나이키 덩크", model_number="DQ8423-100",
                kream_product_id="12345", change_source="kream",
                size="260", old_price=130000, new_price=140000,
                price_diff=10000, old_profit=20000, new_profit=30000,
                profit_diff=10000, old_signal=Signal.BUY,
                new_signal=Signal.STRONG_BUY,
                detected_at=datetime(2026, 3, 24, 11, 0),
            ),
        ]
        embed = format_price_change_alert(changes)

        assert isinstance(embed, discord.Embed)
        assert "가격 변동" in embed.title
        assert "260" in embed.fields[1].value

    def test_empty_changes(self):
        embed = format_price_change_alert([])
        assert "없음" in embed.title


class TestFormatDailyReport:
    def test_creates_report(self):
        op = _make_opportunity()
        embed = format_daily_report(
            scan_count=48,
            product_count=350,
            opportunity_count=12,
            top_products=[op],
        )

        assert isinstance(embed, discord.Embed)
        assert "일일 리포트" in embed.title
        assert "48" in embed.fields[0].value

    def test_no_opportunities(self):
        embed = format_daily_report(0, 0, 0, [])
        assert "없습니다" in embed.fields[1].value


class TestFormatHelp:
    def test_has_all_commands(self):
        embed = format_help()
        text = embed.fields[0].value

        assert "!스캔" in text
        assert "!크림" in text
        assert "!비교" in text
        assert "!무신사" in text
        assert "!키워드" in text
        assert "!설정" in text
        assert "!리포트" in text
        assert "!상태" in text
        assert "!도움" in text


class TestFormatStatus:
    def test_creates_status(self):
        embed = format_status(
            is_kream_active=True,
            is_musinsa_logged_in=False,
            keyword_count=5,
            db_product_count=120,
            uptime="2:30:15",
        )

        assert isinstance(embed, discord.Embed)
        assert "🟢" in embed.fields[0].value  # kream active
        assert "🔴" in embed.fields[0].value  # musinsa not logged in
        assert "크림 API" in embed.fields[0].value
        assert "Chrome" not in embed.fields[0].value
