"""musinsa_httpx 크롤러 단위 테스트 — HTML 파싱 순수 함수."""

from src.crawlers.musinsa_httpx import MusinsaHttpxCrawler


class TestExtractModelFromHtml:
    """_extract_model_from_html 5단계 폴백 테스트."""

    def setup_method(self):
        self.crawler = MusinsaHttpxCrawler()

    def test_extract_model_table(self):
        """품번 테이블에서 추출."""
        html = '<div>품번: DQ8423-100</div>'
        assert self.crawler._extract_model_from_html(html) == "DQ8423-100"

    def test_extract_model_name_pattern(self):
        """og:title에서 / 뒤 모델번호 추출."""
        html = (
            '<html><head>'
            '<meta property="og:title" content="나이키 덩크 로우 / DQ8423-100">'
            '</head><body></body></html>'
        )
        result = self.crawler._extract_model_from_html(html)
        assert "DQ8423" in result

    def test_extract_model_nb_pattern(self):
        """상품명에서 NB 패턴 모델번호 추출."""
        html = (
            '<span class="GoodsName">뉴발란스 530 U7408PL 화이트</span>'
        )
        result = self.crawler._extract_model_from_html(html)
        assert "U7408" in result.upper()

    def test_extract_model_sku_fallback(self):
        """table에 무신사 SKU + 이름에 실품번 → 이름 우선."""
        html = (
            '<div>품번: NBPDGS111G_15</div>'
            '<span class="GoodsName">뉴발란스 U7408PL 그레이</span>'
        )
        result = self.crawler._extract_model_from_html(html)
        # 무신사 SKU(언더스코어)보다 이름의 실품번이 우선
        assert "U7408" in result.upper()

    def test_extract_model_empty(self):
        """모델번호 없는 HTML → 빈 문자열."""
        html = '<html><body><p>일반 텍스트만 있는 페이지</p></body></html>'
        result = self.crawler._extract_model_from_html(html)
        assert result == ""


class TestIsMusinsaSku:
    """_is_musinsa_sku SKU 판별 테스트."""

    def test_is_musinsa_sku_true(self):
        """무신사 SKU 패턴 → True."""
        # 언더스코어 포함
        assert MusinsaHttpxCrawler._is_musinsa_sku("NBPDGS111G_15") is True
        # 긴 알파벳 접두어 (8자+, 하이픈 없음)
        assert MusinsaHttpxCrawler._is_musinsa_sku("ABCDEFGH") is True

    def test_is_musinsa_sku_false(self):
        """실제 모델번호 → False."""
        assert MusinsaHttpxCrawler._is_musinsa_sku("DQ8423-100") is False
        assert MusinsaHttpxCrawler._is_musinsa_sku("U7408PL") is False
        assert MusinsaHttpxCrawler._is_musinsa_sku("CW2288-111") is False
        assert MusinsaHttpxCrawler._is_musinsa_sku("") is False


class TestIsOfflineOrUpcoming:
    """_is_offline_or_upcoming 오프라인/발매예정 체크."""

    def setup_method(self):
        self.crawler = MusinsaHttpxCrawler()

    def test_is_offline_or_upcoming(self):
        """판매예정, 오프라인전용, 정상 케이스."""
        # 판매예정
        assert self.crawler._is_offline_or_upcoming('<div>판매예정 상품</div>') is True
        # 출시예정
        assert self.crawler._is_offline_or_upcoming('<div>출시예정 상품</div>') is True
        # 오프라인전용 + 구매버튼 없음
        assert self.crawler._is_offline_or_upcoming(
            '<div>오프라인 전용 상품</div><div>매장방문</div>'
        ) is True
        # 오프라인전용이지만 구매버튼 있음 → False
        assert self.crawler._is_offline_or_upcoming(
            '<div>오프라인 전용 상품</div><button>구매하기</button>'
        ) is False
        # 정상 상품
        assert self.crawler._is_offline_or_upcoming(
            '<div>일반 상품</div><button>구매하기</button>'
        ) is False


class TestIsOfflineApiGoods:
    """_is_offline_api_goods — API 경로 오프라인 전용 판별 (pid 6046587 회귀)."""

    def test_isOfflineGoods_true(self):
        # 실제 2026-04-18 알림 누수 케이스: pid 6046587 (아식스 1203A837-020)
        data = {
            "goodsNo": "6046587",
            "isOfflineGoods": True,
            "mdOpinion": "<p>해당 상품은 무신사 스토어 성수@대림창고에서 구매 가능한 상품입니다.</p>",
        }
        assert MusinsaHttpxCrawler._is_offline_api_goods(data) is True

    def test_offline_store_banner(self):
        data = {
            "goodsNo": "X",
            "isOfflineGoods": False,
            "goodsDetailBanner": {
                "offlineStoreBanner": {
                    "eventBannerKind": "OFFLINESTORE",
                    "name": "무신사 스토어@대림창고",
                }
            },
        }
        assert MusinsaHttpxCrawler._is_offline_api_goods(data) is True

    def test_online_normal(self):
        data = {
            "goodsNo": "4216277",
            "isOfflineGoods": False,
            "goodsDetailBanner": {"offlineStoreBanner": None},
        }
        assert MusinsaHttpxCrawler._is_offline_api_goods(data) is False

    def test_minimal_dict(self):
        assert MusinsaHttpxCrawler._is_offline_api_goods({}) is False


class TestExtractPrices:
    """_extract_prices_from_html 가격 추출 테스트."""

    def setup_method(self):
        self.crawler = MusinsaHttpxCrawler()

    def test_extract_prices(self):
        """JSON-LD + PriceWrap에서 가격 추출."""
        html = '''
        <script type="application/ld+json">{"price": "89000"}</script>
        <div class="PriceWrap">
            <span>119,000원</span>
            <span>89,000원</span>
        </div>
        '''
        original, sale, discount_type, discount_rate = (
            self.crawler._extract_prices_from_html(html)
        )
        assert original == 119000
        assert sale == 89000
        assert discount_type == "할인"
        assert 0.2 < discount_rate < 0.3  # ~0.252
