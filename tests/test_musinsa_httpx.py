"""musinsa_httpx 크롤러 단위 테스트 — HTML 파싱 순수 함수."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

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


# ─── 1c-3: 재고 증거 배선 — 조회실패(UNKNOWN) vs 품절(OUT_OF_STOCK) 구분 ───


@asynccontextmanager
async def _noop_acquire():
    yield


class _FakeResp:
    """httpx.Response 흉내 — status_code + json() 만 필요."""

    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _goods_data(product_id: str = "9001") -> dict:
    """`_fetch_goods_detail_api` 성공 응답 모사."""
    return {
        "goodsNo": product_id,
        "goodsNm": "나이키 덩크 로우 화이트 / DQ8423-100",
        "brandInfo": {"brandName": "나이키"},
        "styleNo": "DQ8423-100",
        "thumbnailImageUrl": "https://img.example/x.jpg",
        "goodsPrice": {"salePrice": 139000, "normalPrice": 159000, "discountRate": 12.6},
        "isOutOfStock": False,
    }


def _options_data(activated: dict[int, bool] | None = None) -> dict:
    """단일 사이즈 옵션(270/280) options API 응답 모사.

    activated: {optionValueNo: activated} — None 이면 전부 활성.
    """
    activated = activated or {501: True, 502: True}
    option_items = [
        {"no": 1, "activated": activated.get(501, True), "optionValueNos": [501]},
        {"no": 2, "activated": activated.get(502, True), "optionValueNos": [502]},
    ]
    return {
        "data": {
            "basic": [
                {
                    "name": "사이즈",
                    "standardOptionNo": 3,
                    "no": 10,
                    "optionValues": [
                        {"no": 501, "name": "270"},
                        {"no": 502, "name": "280"},
                    ],
                }
            ],
            "optionItems": option_items,
        }
    }


class TestFetchInventoriesApiRetry:
    """`_fetch_inventories_api` — 1회 재시도 + 실패 사유 반환 (검증 기준 6)."""

    def setup_method(self):
        self.crawler = MusinsaHttpxCrawler()
        self.crawler._rate_limiter = AsyncMock()
        self.crawler._rate_limiter.acquire = _noop_acquire

    async def test_retry_success_after_first_failure(self):
        """400 후 200 → 성공 처리 + 호출 2회."""
        fake_client = AsyncMock()
        success_payload = {
            "data": [{"outOfStock": False, "relatedOption": {"optionValueNo": 501}}]
        }
        fake_client.post.side_effect = [
            _FakeResp(400),
            _FakeResp(200, success_payload),
        ]
        with patch.object(self.crawler, "connect", AsyncMock(return_value=fake_client)):
            data, reason = await self.crawler._fetch_inventories_api("9001", [501, 502])

        assert fake_client.post.call_count == 2
        assert reason == ""
        assert data == [{"outOfStock": False, "relatedOption": {"optionValueNo": 501}}]

    async def test_retry_exhausted_failure_confirmed(self):
        """400+400 → 실패 확정 + 호출 2회(1회만 재시도)."""
        fake_client = AsyncMock()
        fake_client.post.side_effect = [_FakeResp(400), _FakeResp(400)]
        with patch.object(self.crawler, "connect", AsyncMock(return_value=fake_client)):
            data, reason = await self.crawler._fetch_inventories_api("9001", [501, 502])

        assert fake_client.post.call_count == 2
        assert data is None
        assert reason == "api_400"


class TestGetProductDetailStockEvidence:
    """`get_product_detail` — source_stock 증거 상태 결정 (검증 기준 1,2,3,4,5,7)."""

    def setup_method(self):
        self.crawler = MusinsaHttpxCrawler()
        self.crawler._rate_limiter = AsyncMock()
        self.crawler._rate_limiter.acquire = _noop_acquire

    async def test_jr2660_regression_inventory_400_unknown_not_promoted(self):
        """JR2660 재현: inventory API 400 → in_stock 사이즈 0개 AND stock_state=UNKNOWN.

        전 사이즈 IN_STOCK 승격이 아님을 명시 assert (2026-04-17 보수 수정 회귀 고정).
        """
        with (
            patch.object(
                self.crawler, "_fetch_goods_detail_api", AsyncMock(return_value=_goods_data())
            ),
            patch.object(
                self.crawler, "_fetch_options_api", AsyncMock(return_value=_options_data())
            ),
            patch.object(
                self.crawler, "_fetch_inventories_api", AsyncMock(return_value=(None, "api_400"))
            ),
        ):
            product = await self.crawler.get_product_detail("9001")

        assert product is not None
        assert product.sizes == []  # 전 사이즈 IN_STOCK 승격 아님
        assert product.stock_state == "UNKNOWN"
        assert product.stock_reason == "api_400"

    async def test_activated_true_inventory_fail_not_promoted(self):
        """activated=true 사이즈도 inventory 조회 실패 시 승격 안 됨 (UNKNOWN)."""
        with (
            patch.object(
                self.crawler, "_fetch_goods_detail_api", AsyncMock(return_value=_goods_data())
            ),
            patch.object(
                self.crawler,
                "_fetch_options_api",
                AsyncMock(return_value=_options_data({501: True, 502: True})),
            ),
            patch.object(
                self.crawler, "_fetch_inventories_api", AsyncMock(return_value=(None, "timeout"))
            ),
        ):
            product = await self.crawler.get_product_detail("9001")

        assert product is not None
        assert product.sizes == []
        assert product.stock_state == "UNKNOWN"
        assert product.stock_reason == "timeout"

    async def test_activated_false_excluded_negative_evidence(self):
        """activated=false → 해당 사이즈 제외 (음성 증거 유지, inventory 성공 케이스)."""
        with (
            patch.object(
                self.crawler, "_fetch_goods_detail_api", AsyncMock(return_value=_goods_data())
            ),
            patch.object(
                self.crawler,
                "_fetch_options_api",
                AsyncMock(return_value=_options_data({501: True, 502: False})),
            ),
            patch.object(
                self.crawler,
                "_fetch_inventories_api",
                AsyncMock(
                    return_value=(
                        [
                            {"outOfStock": False, "relatedOption": {"optionValueNo": 501}},
                            {"outOfStock": False, "relatedOption": {"optionValueNo": 502}},
                        ],
                        "",
                    )
                ),
            ),
        ):
            product = await self.crawler.get_product_detail("9001")

        assert product is not None
        sizes = [s.size for s in product.sizes]
        assert sizes == ["270"]  # 502(activated=false)는 제외
        assert product.stock_state == "IN_STOCK"

    async def test_inventory_success_partial_stock_in_stock(self):
        """inventory 성공 + 일부 재고 → stock_state=IN_STOCK + 정확한 사이즈만."""
        with (
            patch.object(
                self.crawler, "_fetch_goods_detail_api", AsyncMock(return_value=_goods_data())
            ),
            patch.object(
                self.crawler, "_fetch_options_api", AsyncMock(return_value=_options_data())
            ),
            patch.object(
                self.crawler,
                "_fetch_inventories_api",
                AsyncMock(
                    return_value=(
                        [
                            {"outOfStock": False, "relatedOption": {"optionValueNo": 501}},
                            {"outOfStock": True, "relatedOption": {"optionValueNo": 502}},
                        ],
                        "",
                    )
                ),
            ),
        ):
            product = await self.crawler.get_product_detail("9001")

        assert product is not None
        sizes = [s.size for s in product.sizes]
        assert sizes == ["270"]
        assert product.stock_state == "IN_STOCK"
        assert product.stock_reason == ""

    async def test_inventory_success_all_out_of_stock(self):
        """inventory 성공 + 전부 outOfStock → stock_state=OUT_OF_STOCK."""
        with (
            patch.object(
                self.crawler, "_fetch_goods_detail_api", AsyncMock(return_value=_goods_data())
            ),
            patch.object(
                self.crawler, "_fetch_options_api", AsyncMock(return_value=_options_data())
            ),
            patch.object(
                self.crawler,
                "_fetch_inventories_api",
                AsyncMock(
                    return_value=(
                        [
                            {"outOfStock": True, "relatedOption": {"optionValueNo": 501}},
                            {"outOfStock": True, "relatedOption": {"optionValueNo": 502}},
                        ],
                        "",
                    )
                ),
            ),
        ):
            product = await self.crawler.get_product_detail("9001")

        assert product is not None
        assert product.sizes == []
        assert product.stock_state == "OUT_OF_STOCK"

    async def test_options_api_fail_reason(self):
        """options API 실패 → stock_reason=='options_api_fail'."""
        with (
            patch.object(
                self.crawler, "_fetch_goods_detail_api", AsyncMock(return_value=_goods_data())
            ),
            patch.object(self.crawler, "_fetch_options_api", AsyncMock(return_value=None)),
        ):
            product = await self.crawler.get_product_detail("9001")

        assert product is not None
        assert product.sizes == []
        assert product.stock_state == "UNKNOWN"
        assert product.stock_reason == "options_api_fail"
