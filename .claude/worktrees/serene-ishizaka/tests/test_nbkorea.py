"""NB Korea 크롤러 단위 테스트."""

from __future__ import annotations

import pytest

from src.crawlers.nbkorea import (
    NbKoreaCrawler,
    _normalize_nb_model,
    _parse_category_mapping,
    _parse_prod_opt,
)


# ===== _parse_prod_opt =====

class TestParseProdOpt:
    def test_basic(self):
        opt = [
            {
                "DispStyleName": "M2002RXD",
                "SizeName": "250",
                "Price": 189000,
                "NorPrice": 199000,
                "Qty": 10,
                "StyleCode": "NBP7GS114F",
                "ColCode": "85",
            },
            {
                "DispStyleName": "M2002RXD",
                "SizeName": "260",
                "Price": 189000,
                "NorPrice": 199000,
                "Qty": 0,
                "StyleCode": "NBP7GS114F",
                "ColCode": "85",
            },
        ]
        rows = _parse_prod_opt(opt)
        assert len(rows) == 2
        assert rows[0]["size"] == "250"
        assert rows[0]["in_stock"] is True
        assert rows[0]["price"] == 189000
        assert rows[0]["original_price"] == 199000
        assert rows[0]["model_number"] == "M2002RXD"
        assert rows[1]["size"] == "260"
        assert rows[1]["in_stock"] is False

    def test_empty(self):
        assert _parse_prod_opt([]) == []

    def test_missing_size_name(self):
        """SizeName 없는 항목은 스킵."""
        opt = [{"DispStyleName": "M2002RXD", "SizeName": "", "Price": 100000, "Qty": 5}]
        assert _parse_prod_opt(opt) == []

    def test_same_price(self):
        """Price == NorPrice -> original_price == price, 할인 없음."""
        opt = [
            {
                "DispStyleName": "ML860XA",
                "SizeName": "270",
                "Price": 139000,
                "NorPrice": 139000,
                "Qty": 3,
                "StyleCode": "NBPDGS101F",
                "ColCode": "19",
            },
        ]
        rows = _parse_prod_opt(opt)
        assert len(rows) == 1
        assert rows[0]["price"] == 139000
        assert rows[0]["original_price"] == 139000

    def test_nor_price_zero_fallback(self):
        """NorPrice가 0이면 Price로 대체."""
        opt = [
            {
                "DispStyleName": "U2000ETC",
                "SizeName": "280",
                "Price": 159000,
                "NorPrice": 0,
                "Qty": 1,
            },
        ]
        rows = _parse_prod_opt(opt)
        assert rows[0]["original_price"] == 159000


# ===== _parse_category_mapping =====

class TestParseCategoryMapping:
    def test_extract_mapping(self):
        html = '''
        <div class="product_list">
          <li data-style="NBP7GS114F" data-col="85" data-display-name="U20024VT">
            <a href="/product/productDetail.action?styleCode=NBP7GS114F&colCode=85">product1</a>
          </li>
          <li data-style="NBPDGS101F" data-col="19" data-display-name="M2002RXD">
            <a href="/product/productDetail.action?styleCode=NBPDGS101F&colCode=19">product2</a>
          </li>
        </div>
        '''
        mapping = _parse_category_mapping(html)
        assert "U20024VT" in mapping
        assert "M2002RXD" in mapping
        assert mapping["U20024VT"] == [("NBP7GS114F", "85")]
        assert mapping["M2002RXD"] == [("NBPDGS101F", "19")]

    def test_empty_html(self):
        assert _parse_category_mapping("") == {}

    def test_duplicate_dedup(self):
        """같은 display_name, 같은 style/col 쌍은 중복 추가 안 함."""
        html = '''
        <li data-style="NBP7GS114F" data-col="85" data-display-name="U20024VT">a</li>
        <li data-style="NBP7GS114F" data-col="85" data-display-name="U20024VT">b</li>
        '''
        mapping = _parse_category_mapping(html)
        assert len(mapping["U20024VT"]) == 1

    def test_multiple_colors_same_model(self):
        """같은 모델에 다른 컬러 코드."""
        html = '''
        <li data-style="NBP7GS114F" data-col="85" data-display-name="U20024VT">a</li>
        <li data-style="NBP7GS114F" data-col="19" data-display-name="U20024VT">b</li>
        '''
        mapping = _parse_category_mapping(html)
        assert len(mapping["U20024VT"]) == 2

    def test_alt_attribute_order(self):
        """data-display-name이 data-style보다 앞에 올 때."""
        html = '<li data-display-name="ML860XA" data-style="NBPDFF003Z" data-col="22">x</li>'
        mapping = _parse_category_mapping(html)
        assert "ML860XA" in mapping
        assert mapping["ML860XA"] == [("NBPDFF003Z", "22")]


# ===== _normalize_nb_model =====

class TestNormalizeModel:
    def test_uppercase(self):
        assert _normalize_nb_model("m2002rxd") == "M2002RXD"

    def test_strip(self):
        assert _normalize_nb_model(" M2002RXD ") == "M2002RXD"

    def test_remove_hyphen(self):
        assert _normalize_nb_model("M2002-RXD") == "M2002RXD"

    def test_remove_space(self):
        assert _normalize_nb_model("M2002 RXD") == "M2002RXD"


# ===== 레지스트리 등록 =====

class TestRegistration:
    def test_nbkorea_registered(self):
        from src.crawlers import nbkorea as nbkorea_mod
        from src.crawlers.registry import RETAIL_CRAWLERS, register
        register("nbkorea", nbkorea_mod.nbkorea_crawler, "뉴발란스")
        assert "nbkorea" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["nbkorea"]["label"] == "뉴발란스"


# ===== search_products / get_product_detail 모킹 테스트 =====

class TestSearchProductsMocked:
    @pytest.mark.asyncio
    async def test_search_found(self, monkeypatch):
        """매핑 캐시 히트 + opt API 정상 -> 결과 반환."""
        crawler = NbKoreaCrawler()

        # 매핑 캐시 모킹
        async def mock_mapping():
            return {"M2002RXD": [("NBPDGS101F", "19")]}

        monkeypatch.setattr(crawler, "_get_mapping", mock_mapping)

        # opt API 모킹
        async def mock_opt(style_code, col_code):
            return {
                "prodOpt": [
                    {
                        "StyleCode": "NBPDGS101F",
                        "DispStyleName": "M2002RXD",
                        "SizeName": "250",
                        "Price": 189000,
                        "NorPrice": 199000,
                        "Qty": 5,
                        "ColCode": "19",
                    },
                    {
                        "StyleCode": "NBPDGS101F",
                        "DispStyleName": "M2002RXD",
                        "SizeName": "260",
                        "Price": 189000,
                        "NorPrice": 199000,
                        "Qty": 0,
                        "ColCode": "19",
                    },
                ],
            }

        monkeypatch.setattr(crawler, "_fetch_opt_info", mock_opt)

        results = await crawler.search_products("M2002RXD")
        assert len(results) == 1
        assert results[0]["model_number"] == "M2002RXD"
        assert results[0]["product_id"] == "NBPDGS101F_19"
        assert results[0]["price"] == 189000
        # sizes 포함
        assert len(results[0]["sizes"]) == 2
        assert results[0]["sizes"][0]["in_stock"] is True
        assert results[0]["sizes"][1]["in_stock"] is False

    @pytest.mark.asyncio
    async def test_search_no_match(self, monkeypatch):
        """매핑 캐시 미스 -> 빈 리스트."""
        crawler = NbKoreaCrawler()

        async def mock_mapping():
            return {"OTHERMODEL": [("XXX", "01")]}

        monkeypatch.setattr(crawler, "_get_mapping", mock_mapping)

        results = await crawler.search_products("M2002RXD")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self, monkeypatch):
        crawler = NbKoreaCrawler()
        results = await crawler.search_products("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_short_keyword(self, monkeypatch):
        crawler = NbKoreaCrawler()
        results = await crawler.search_products("AB")
        assert results == []


class TestGetProductDetailMocked:
    @pytest.mark.asyncio
    async def test_detail_success(self, monkeypatch):
        """정상 조회 -> RetailProduct 반환."""
        crawler = NbKoreaCrawler()

        async def mock_status(style_code, col_code):
            return {"soldOutYn": "N", "comingSoonYn": "N"}

        async def mock_opt(style_code, col_code):
            return {
                "prodOpt": [
                    {
                        "StyleCode": "NBPDGS101F",
                        "DispStyleName": "M2002RXD",
                        "SizeName": "250",
                        "Price": 179000,
                        "NorPrice": 199000,
                        "Qty": 3,
                        "ColCode": "19",
                    },
                    {
                        "StyleCode": "NBPDGS101F",
                        "DispStyleName": "M2002RXD",
                        "SizeName": "260",
                        "Price": 179000,
                        "NorPrice": 199000,
                        "Qty": 0,
                        "ColCode": "19",
                    },
                ],
            }

        monkeypatch.setattr(crawler, "_fetch_opt_status", mock_status)
        monkeypatch.setattr(crawler, "_fetch_opt_info", mock_opt)

        product = await crawler.get_product_detail("NBPDGS101F_19")
        assert product is not None
        assert product.source == "nbkorea"
        assert product.model_number == "M2002RXD"
        assert product.brand == "New Balance"
        assert len(product.sizes) == 1  # Qty=0 필터됨
        assert product.sizes[0].size == "250"
        assert product.sizes[0].price == 179000
        assert product.sizes[0].discount_rate > 0

    @pytest.mark.asyncio
    async def test_detail_sold_out(self, monkeypatch):
        """품절 -> None."""
        crawler = NbKoreaCrawler()

        async def mock_status(style_code, col_code):
            return {"soldOutYn": "Y", "comingSoonYn": "N"}

        monkeypatch.setattr(crawler, "_fetch_opt_status", mock_status)

        result = await crawler.get_product_detail("NBPDGS101F_19")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_coming_soon(self, monkeypatch):
        """발매예정 -> None."""
        crawler = NbKoreaCrawler()

        async def mock_status(style_code, col_code):
            return {"soldOutYn": "N", "comingSoonYn": "Y"}

        monkeypatch.setattr(crawler, "_fetch_opt_status", mock_status)

        result = await crawler.get_product_detail("NBPDGS101F_19")
        assert result is None

    @pytest.mark.asyncio
    async def test_detail_invalid_id(self, monkeypatch):
        """잘못된 product_id -> None."""
        crawler = NbKoreaCrawler()
        result = await crawler.get_product_detail("INVALID")
        assert result is None
