"""ABC마트/a-rt.com 크롤러 단위 테스트 — JSON API 파싱 순수 함수 + 멀티채널."""

from src.crawlers.abcmart import (
    AbcMartCrawler,
    ArtCrawler,
    ArtChannelConfig,
    GRANDSTAGE_CONFIG,
    ONTHESPOT_CONFIG,
    _build_model_number,
    _parse_option_inline,
)


class TestBuildModelNumber:
    """모델번호 조합 테스트."""

    def test_style_with_3digit_color(self):
        """STYLE_INFO + 3자리 COLOR_ID -> 하이픈 조합."""
        assert _build_model_number("IB7746", "001") == "IB7746-001"

    def test_style_with_rgb_color(self):
        """COLOR_ID가 RGB 값이면 style_info만 반환."""
        assert _build_model_number("IB7746", "FF0000") == "IB7746"

    def test_style_only(self):
        """COLOR_ID 없으면 style_info만 반환."""
        assert _build_model_number("DQ8423", "") == "DQ8423"

    def test_empty_style(self):
        """style_info가 빈 문자열이면 빈 문자열."""
        assert _build_model_number("", "001") == ""

    def test_color_id_non_numeric(self):
        """COLOR_ID가 숫자 3자리가 아니면 style_info만."""
        assert _build_model_number("AB1234", "AB") == "AB1234"


class TestParseOptionInline:
    """PRDT_OPTION_INLINE 파싱 테스트."""

    def test_normal_inline(self):
        """정상 인라인 파싱."""
        inline = "240,168,10001/245,59,10001/250,0,10001/"
        sizes = _parse_option_inline(inline)
        assert len(sizes) == 3
        assert sizes[0] == {"size": "240", "stock": 168, "in_stock": True}
        assert sizes[1] == {"size": "245", "stock": 59, "in_stock": True}
        assert sizes[2] == {"size": "250", "stock": 0, "in_stock": False}

    def test_empty_inline(self):
        """빈 문자열 -> 빈 리스트."""
        assert _parse_option_inline("") == []

    def test_none_inline(self):
        """None -> 빈 리스트."""
        assert _parse_option_inline(None) == []

    def test_single_size(self):
        """사이즈 1개."""
        sizes = _parse_option_inline("270,5,10002")
        assert len(sizes) == 1
        assert sizes[0]["size"] == "270"
        assert sizes[0]["stock"] == 5
        assert sizes[0]["in_stock"] is True

    def test_trailing_slash(self):
        """끝에 슬래시만 있으면 무시."""
        sizes = _parse_option_inline("/")
        assert sizes == []


class TestAbcMartCrawlerInit:
    """크롤러 초기화 테스트 (하위호환)."""

    def test_init(self):
        """AbcMartCrawler alias로 인스턴스 생성."""
        crawler = AbcMartCrawler(GRANDSTAGE_CONFIG)
        assert crawler._client is None
        assert crawler._config.channel_id == "10002"

    def test_alias_is_art_crawler(self):
        """AbcMartCrawler는 ArtCrawler의 alias."""
        assert AbcMartCrawler is ArtCrawler


class TestArtChannelConfig:
    """ArtChannelConfig 및 채널 설정 검증."""

    def test_grandstage_config(self):
        from src.crawlers.abcmart import grandstage_crawler
        assert grandstage_crawler._config.channel_id == "10002"
        assert "result-total" in grandstage_crawler._config.search_path
        assert grandstage_crawler._config.source_key == "grandstage"
        assert grandstage_crawler._config.label == "그랜드스테이지"

    def test_onthespot_config(self):
        from src.crawlers.abcmart import onthespot_crawler
        assert onthespot_crawler._config.channel_id == "10003"
        assert "result-total" not in onthespot_crawler._config.search_path
        assert onthespot_crawler._config.accept_header == "application/json"
        assert onthespot_crawler._config.source_key == "onthespot"
        assert onthespot_crawler._config.label == "온더스팟"

    def test_config_is_frozen(self):
        """Config는 불변."""
        import dataclasses
        assert dataclasses.is_dataclass(ArtChannelConfig)
        try:
            GRANDSTAGE_CONFIG.channel_id = "99999"
            assert False, "Should raise FrozenInstanceError"
        except (AttributeError, dataclasses.FrozenInstanceError):
            pass

    def test_default_accept_header(self):
        """기본 accept_header 확인."""
        assert GRANDSTAGE_CONFIG.accept_header == "application/json, text/plain, */*"


class TestArtRegistryRegistration:
    """레지스트리 등록 확인."""

    def test_grandstage_registered(self):
        from src.crawlers.registry import RETAIL_CRAWLERS
        assert "grandstage" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["grandstage"]["label"] == "그랜드스테이지"

    def test_onthespot_registered(self):
        from src.crawlers.registry import RETAIL_CRAWLERS
        assert "onthespot" in RETAIL_CRAWLERS
        assert RETAIL_CRAWLERS["onthespot"]["label"] == "온더스팟"

    def test_crawlers_are_distinct_instances(self):
        from src.crawlers.abcmart import grandstage_crawler, onthespot_crawler
        assert grandstage_crawler is not onthespot_crawler


class TestArtCrawlerProductPageUrl:
    """URL 생성 검증."""

    def test_grandstage_url(self):
        from src.crawlers.abcmart import grandstage_crawler
        url = grandstage_crawler._product_page_url("12345")
        assert url == "https://grandstage.a-rt.com/product/new?prdtNo=12345"

    def test_onthespot_url(self):
        from src.crawlers.abcmart import onthespot_crawler
        url = onthespot_crawler._product_page_url("67890")
        assert url == "https://www.onthespot.co.kr/product/new?prdtNo=67890"
