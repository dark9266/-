"""SDUI 가격 병합 회귀 테스트.

SDUI parameters.price 는 "구매입찰 추천 시작가"이지 실제 시세가 아님.
pinia(NUXT) sell_now/buy_now 가 항상 우선해야 하며, SDUI 는 사이즈
목록 확장 용도로만 사용(가격은 None 으로 클리어).

2026-04-16 사고: SDUI 가격이 pinia 를 덮어써 12건 중 11건 허위 알림.
"""

import pytest

from src.models.product import KreamSizePrice


def _make_sp(size: str, sell=None, buy=None, last=None):
    return KreamSizePrice(
        size=size,
        sell_now_price=sell,
        buy_now_price=buy,
        last_sale_price=last,
    )


def _merge_pinia_priority(pinia: list[KreamSizePrice], sdui: list[KreamSizePrice]):
    """kream.py get_full_product_info 의 병합 로직 재현 (테스트용)."""
    if not sdui:
        return pinia

    if len(sdui) > len(pinia):
        pinia_map = {p.size: p for p in pinia}
        api_map = {p.size: p for p in sdui}
        merged: list[KreamSizePrice] = []
        for ap in sdui:
            pp = pinia_map.get(ap.size)
            if pp:
                if not pp.last_sale_price and ap.last_sale_price:
                    pp.last_sale_price = ap.last_sale_price
                    pp.last_sale_date = ap.last_sale_date
                merged.append(pp)
            else:
                ap.sell_now_price = None
                ap.buy_now_price = None
                merged.append(ap)
        for pp in pinia:
            if pp.size not in api_map:
                merged.append(pp)
        merged.sort(key=lambda p: int(p.size) if p.size.isdigit() else 0)
        return merged
    elif not pinia:
        for ap in sdui:
            ap.sell_now_price = None
            ap.buy_now_price = None
        return sdui
    return pinia


class TestPiniaPriority:
    """pinia 가격이 SDUI 보다 항상 우선하는지 검증."""

    def test_pinia_sell_now_preserved(self):
        """SDUI 의 부풀린 sell_now 가 pinia 값을 덮어쓰면 안 됨."""
        pinia = [_make_sp("220", sell=110_000), _make_sp("225", sell=125_000)]
        sdui = [
            _make_sp("220", sell=199_000),
            _make_sp("225", sell=169_000),
            _make_sp("230", sell=138_000),  # pinia 에 없음
        ]
        result = _merge_pinia_priority(pinia, sdui)
        by_size = {p.size: p for p in result}

        assert by_size["220"].sell_now_price == 110_000
        assert by_size["225"].sell_now_price == 125_000
        assert by_size["230"].sell_now_price is None

    def test_pinia_buy_now_preserved(self):
        pinia = [_make_sp("245", buy=109_000)]
        sdui = [_make_sp("245", buy=200_000), _make_sp("250", buy=180_000)]
        result = _merge_pinia_priority(pinia, sdui)
        by_size = {p.size: p for p in result}

        assert by_size["245"].buy_now_price == 109_000
        assert by_size["250"].buy_now_price is None

    def test_sdui_only_sizes_get_none_price(self):
        """SDUI 에만 있는 사이즈의 가격은 None 이어야 함."""
        pinia = [_make_sp("220", sell=110_000)]
        sdui = [
            _make_sp("220", sell=199_000),
            _make_sp("260", sell=145_000),
            _make_sp("265", sell=169_000),
        ]
        result = _merge_pinia_priority(pinia, sdui)
        for p in result:
            if p.size in ("260", "265"):
                assert p.sell_now_price is None, f"{p.size} should have None sell_now"

    def test_pinia_zero_not_overwritten(self):
        """pinia sell_now=None 인 사이즈도 SDUI 로 덮어쓰면 안 됨."""
        pinia = [_make_sp("230", sell=None)]
        sdui = [_make_sp("230", sell=138_000), _make_sp("235", sell=119_000)]
        result = _merge_pinia_priority(pinia, sdui)
        by_size = {p.size: p for p in result}

        assert by_size["230"].sell_now_price is None
        assert by_size["235"].sell_now_price is None

    def test_last_sale_price_supplemented(self):
        """pinia 에 last_sale 없고 SDUI 에 있으면 보충."""
        pinia = [_make_sp("220", sell=110_000, last=None)]
        sdui = [_make_sp("220", sell=199_000, last=105_000), _make_sp("225", sell=169_000)]
        result = _merge_pinia_priority(pinia, sdui)
        by_size = {p.size: p for p in result}

        assert by_size["220"].sell_now_price == 110_000
        assert by_size["220"].last_sale_price == 105_000

    def test_empty_pinia_clears_sdui_prices(self):
        """pinia 0건 → SDUI 사이즈 목록만 사용, 가격 전부 None."""
        pinia: list[KreamSizePrice] = []
        sdui = [_make_sp("220", sell=199_000), _make_sp("225", sell=169_000)]
        result = _merge_pinia_priority(pinia, sdui)

        for p in result:
            assert p.sell_now_price is None, f"{p.size} should be None"
            assert p.buy_now_price is None

    def test_pinia_only_sizes_preserved(self):
        """pinia 에만 있는 사이즈는 결과에 보존."""
        pinia = [_make_sp("220", sell=110_000), _make_sp("999", sell=50_000)]
        sdui = [_make_sp("220", sell=199_000), _make_sp("225", sell=169_000)]
        result = _merge_pinia_priority(pinia, sdui)
        sizes = {p.size for p in result}

        assert "999" in sizes
        assert {p.size: p for p in result}["999"].sell_now_price == 50_000


class TestExtractBestSellNow:
    """kream_delta_client._extract_best_sell_now 가 SDUI price 를 무시하는지."""

    def test_sdui_parameters_price_ignored(self):
        from src.crawlers.kream_delta_client import _extract_best_sell_now

        data = {
            "content": {
                "items": [
                    {
                        "action": {
                            "parameters": {
                                "option_key": ["220"],
                                "price": ["199000"],
                            }
                        }
                    }
                ]
            }
        }
        assert _extract_best_sell_now(data) is None

    def test_legacy_sell_now_price_used(self):
        from src.crawlers.kream_delta_client import _extract_best_sell_now

        data = {"sell_now_price": 110_000}
        assert _extract_best_sell_now(data) == 110_000

    def test_nested_sellNowPrice_used(self):
        from src.crawlers.kream_delta_client import _extract_best_sell_now

        data = {"items": [{"sellNowPrice": 125_000}, {"sellNowPrice": 130_000}]}
        assert _extract_best_sell_now(data) == 130_000
