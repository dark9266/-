"""스캔 오케스트레이터.

전체 파이프라인을 조율한다:
무신사 검색 → 모델번호 매칭 → 크림 시세 수집 → 수익 분석 → 가격 변동 감지

자동스캔 (역방향):
크림 인기상품 수집 → 무신사 매입가 검색 → 2단계 수익 분석 → 실시간 알림
"""

import asyncio
from datetime import datetime, timedelta

from src.config import settings
from src.crawlers.kream import kream_crawler
from src.crawlers.musinsa import musinsa_crawler
from src.crawlers.twentynine_cm import twentynine_cm_crawler
from src.matcher import (
    _pick_best_kream_match,
    extract_model_from_name,
    find_kream_match,
    model_numbers_match,
    normalize_model_number,
)
from src.models.database import Database
from src.models.product import (
    AutoScanOpportunity,
    KreamProduct,
    ProfitOpportunity,
    RetailProduct,
    Signal,
)
from src.price_tracker import PriceChange, PriceTracker
from src.profit_calculator import analyze_auto_scan_opportunity, analyze_opportunity, determine_signal
from src.utils.logging import setup_logger

logger = setup_logger("scanner")


def _match_model_with_slash(musinsa_model: str, kream_model: str) -> bool:
    """모델번호 매칭 (슬래시 포함 복합 모델번호 지원).

    크림: '315122-111/CW2288-111', 무신사: 'CW2288-111' → True
    """
    if model_numbers_match(musinsa_model, kream_model):
        return True

    # 슬래시로 구분된 복합 모델번호인 경우 각각 매칭 시도
    if "/" in kream_model:
        for part in kream_model.split("/"):
            part = part.strip()
            if part and model_numbers_match(musinsa_model, part):
                return True

    if "/" in musinsa_model:
        for part in musinsa_model.split("/"):
            part = part.strip()
            if part and model_numbers_match(part, kream_model):
                return True

    return False


class ScanResult:
    """스캔 결과."""

    def __init__(self):
        self.opportunities: list[ProfitOpportunity] = []
        self.price_changes: list[PriceChange] = []
        self.scanned_products: int = 0
        self.matched_products: int = 0
        self.errors: list[str] = []
        self.started_at: datetime = datetime.now()
        self.finished_at: datetime | None = None

    @property
    def profitable_count(self) -> int:
        return sum(1 for o in self.opportunities if o.signal in (Signal.STRONG_BUY, Signal.BUY))


class AutoScanResult:
    """자동스캔 결과 (크림 기준 역방향)."""

    def __init__(self):
        self.opportunities: list[AutoScanOpportunity] = []
        self.kream_scanned: int = 0  # 크림 상품 조회 수
        self.musinsa_searched: int = 0  # 무신사 검색 수
        self.matched: int = 0  # 매칭 성공 수
        self.errors: list[str] = []
        self.started_at: datetime = datetime.now()
        self.finished_at: datetime | None = None

    @property
    def confirmed_count(self) -> int:
        """확정 수익 기회 수 (ROI 5% 이상)."""
        threshold = settings.auto_scan_confirmed_roi
        return sum(
            1 for o in self.opportunities
            if o.best_confirmed_roi >= threshold
        )

    @property
    def estimated_count(self) -> int:
        """예상 수익 기회 수 (ROI 10% 이상)."""
        threshold = settings.auto_scan_estimated_roi
        return sum(
            1 for o in self.opportunities
            if o.best_estimated_roi >= threshold
        )


class BatchScanResult:
    """배치스캔 결과 (전체 DB 순회)."""

    def __init__(self):
        self.total: int = 0
        self.processed: int = 0  # 실제 검색한 수
        self.new_matched: int = 0  # 새로 매칭된 수
        self.already_matched: int = 0  # 기존 매칭 스킵
        self.brand_skipped: int = 0  # 브랜드 스킵 수
        self.no_match: int = 0
        self.opportunities: list[AutoScanOpportunity] = []
        self.errors: list[str] = []
        self.started_at: datetime = datetime.now()
        self.finished_at: datetime | None = None


# 무신사/29CM에서 판매하지 않는 럭셔리/특수 브랜드 (배치스캔·역방향 스킵용)
_SKIP_BRANDS = {
    # 럭셔리
    "Louis Vuitton", "Chanel", "Gucci", "Hermes", "Hermès",
    "Prada", "Dior", "Balenciaga", "Bottega Veneta", "Saint Laurent",
    "Burberry", "Givenchy", "Fendi", "Valentino", "Versace",
    "Loewe", "Celine", "Céline", "Moncler", "Tom Ford",
    "Chrome Hearts", "Off-White", "Bape", "A Bathing Ape",
    "Supreme", "Fear of God", "Essentials",
    "Hansroom x Chrome Hearts",
    # 전자기기·시계
    "Apple", "Apple Refurbished", "Samsung", "Sony", "Nintendo", "Dyson",
    "Rolex", "Rolex Vintage", "Omega", "Cartier", "Tiffany & Co.",
    # 기타
    "Rimowa", "Goyard",
}


class ReverseScanResult:
    """역방향 스캔 결과 (무신사 세일 → 크림 DB 매칭)."""

    def __init__(self):
        self.opportunities: list[AutoScanOpportunity] = []
        self.sale_collected: int = 0  # 세일 상품 수집 수
        self.detail_fetched: int = 0  # 상세 조회 수
        self.db_matched: int = 0  # 크림 DB 매칭 성공 수
        self.errors: list[str] = []
        self.started_at: datetime = datetime.now()
        self.finished_at: datetime | None = None

    @property
    def confirmed_count(self) -> int:
        threshold = settings.auto_scan_confirmed_roi
        return sum(
            1 for o in self.opportunities
            if o.best_confirmed_roi >= threshold
        )

    @property
    def estimated_count(self) -> int:
        threshold = settings.auto_scan_estimated_roi
        return sum(
            1 for o in self.opportunities
            if o.best_estimated_roi >= threshold
        )


class CategoryScanResult:
    """카테고리 스캔 결과."""

    def __init__(self):
        self.opportunities: list[AutoScanOpportunity] = []
        self.listing_fetched: int = 0      # API 수집 총 상품 수
        self.sold_out_skipped: int = 0
        self.already_scanned: int = 0
        self.brand_filtered: int = 0
        self.name_matched: int = 0         # 이름에서 모델→DB 성공
        self.name_no_match: int = 0        # 이름 모델→DB 실패 (스킵)
        self.detail_fetched: int = 0       # 상세 방문 수
        self.detail_matched: int = 0       # 상세→DB 성공
        self.errors: list[str] = []
        self.pages_scanned: int = 0
        self.started_at: datetime = datetime.now()
        self.finished_at: datetime | None = None

    @property
    def confirmed_count(self) -> int:
        threshold = settings.auto_scan_confirmed_roi
        return sum(
            1 for o in self.opportunities
            if o.best_confirmed_roi >= threshold
        )

    @property
    def estimated_count(self) -> int:
        threshold = settings.auto_scan_estimated_roi
        return sum(
            1 for o in self.opportunities
            if o.best_estimated_roi >= threshold
        )


class Scanner:
    """스캔 오케스트레이터."""

    def __init__(self, db: Database):
        self.db = db
        self.price_tracker = PriceTracker(db)
        # 자동스캔 캐시: {model_number: (timestamp, KreamProduct)}
        self._auto_scan_cache: dict[str, tuple[datetime, KreamProduct]] = {}
        # 매칭 검토 채널 콜백 (봇에서 설정)
        self._match_review_callback = None
        # 배치스캔 중지 플래그
        self._batch_scan_stop: bool = False

    async def scan_keyword(self, keyword: str) -> ScanResult:
        """단일 키워드로 전체 파이프라인 실행.

        1. 무신사에서 키워드 검색
        2. 각 상품의 모델번호 추출
        3. 크림에서 매칭 상품 찾기
        4. 수익 분석
        5. 가격 변동 감지
        """
        result = ScanResult()
        logger.info("스캔 시작: '%s'", keyword)

        # 1. 무신사 검색
        try:
            search_results = await musinsa_crawler.search_products(keyword)
        except Exception as e:
            result.errors.append(f"무신사 검색 실패: {e}")
            logger.error("무신사 검색 실패 (%s): %s", keyword, e)
            result.finished_at = datetime.now()
            return result

        if not search_results:
            logger.info("무신사 검색 결과 없음: '%s'", keyword)
            result.finished_at = datetime.now()
            return result

        logger.info("무신사 검색: %d건 발견", len(search_results))

        # 2. 각 상품 상세 조회 + 크림 매칭 + 수익 분석
        # 모델번호별 그룹핑 (같은 모델번호 상품은 한 번만 크림 검색)
        kream_cache: dict[str, KreamProduct | None] = {}
        retail_by_model: dict[str, list[RetailProduct]] = {}

        for item in search_results:
            result.scanned_products += 1

            try:
                # 상세 페이지에서 모델번호, 가격, 사이즈 수집
                retail_product = await musinsa_crawler.get_product_detail(item["product_id"])
                if not retail_product or not retail_product.model_number:
                    continue

                normalized = normalize_model_number(retail_product.model_number)
                if not normalized:
                    continue

                # 리테일 상품 DB 저장
                await self.db.upsert_retail_product(
                    source=retail_product.source,
                    product_id=retail_product.product_id,
                    name=retail_product.name,
                    model_number=normalized,
                    brand=retail_product.brand,
                    url=retail_product.url,
                    image_url=retail_product.image_url,
                )

                # 모델번호별 그룹핑
                if normalized not in retail_by_model:
                    retail_by_model[normalized] = []
                retail_by_model[normalized].append(retail_product)

            except Exception as e:
                result.errors.append(f"상품 조회 실패 ({item.get('product_id', '?')}): {e}")
                logger.error("상품 조회 실패: %s", e)

        # 3. 모델번호별 크림 매칭 + 수익 분석
        for model_number, retail_products in retail_by_model.items():
            try:
                # 크림 매칭 (캐시 사용)
                if model_number in kream_cache:
                    kream_product = kream_cache[model_number]
                else:
                    kream_product = await find_kream_match(retail_products[0], self.db)
                    kream_cache[model_number] = kream_product

                if not kream_product:
                    continue

                result.matched_products += 1

                # 수익 분석
                opportunity = analyze_opportunity(kream_product, retail_products)
                if opportunity and opportunity.best_profit > 0:
                    result.opportunities.append(opportunity)

                # 가격 변동 감지
                retail_price_map = {}
                for rp in retail_products:
                    for s in rp.sizes:
                        if s.in_stock and (s.size not in retail_price_map or s.price < retail_price_map[s.size]):
                            retail_price_map[s.size] = s.price

                changes = await self.price_tracker.check_kream_price_changes(
                    kream_product, retail_price_map
                )
                significant = self.price_tracker.filter_significant_changes(changes)
                result.price_changes.extend(significant)

                # 리테일 가격 변동도 감지
                kream_sell_map = {
                    sp.size: sp.sell_now_price
                    for sp in kream_product.size_prices
                    if sp.sell_now_price is not None
                }
                for rp in retail_products:
                    retail_changes = await self.price_tracker.check_retail_price_changes(
                        rp, kream_sell_map, kream_product.volume_7d
                    )
                    significant_retail = self.price_tracker.filter_significant_changes(retail_changes)
                    result.price_changes.extend(significant_retail)

            except Exception as e:
                result.errors.append(f"매칭/분석 실패 ({model_number}): {e}")
                logger.error("매칭/분석 실패 (%s): %s", model_number, e)

        # 수익 높은 순으로 정렬
        result.opportunities.sort(key=lambda o: -o.best_profit)
        result.finished_at = datetime.now()

        logger.info(
            "스캔 완료: '%s' | 검색 %d → 매칭 %d → 수익기회 %d (수익 %d) | 가격변동 %d",
            keyword, result.scanned_products, result.matched_products,
            len(result.opportunities), result.profitable_count,
            len(result.price_changes),
        )

        return result

    async def scan_all_keywords(self) -> ScanResult:
        """모든 활성 키워드로 스캔 실행."""
        keywords = await self.db.get_active_keywords()
        if not keywords:
            logger.warning("모니터링 키워드가 없습니다. !키워드추가 명령어로 추가하세요.")
            return ScanResult()

        combined = ScanResult()
        logger.info("전체 스캔 시작: 키워드 %d개 (%s)", len(keywords), ", ".join(keywords))

        for keyword in keywords:
            result = await self.scan_keyword(keyword)
            combined.opportunities.extend(result.opportunities)
            combined.price_changes.extend(result.price_changes)
            combined.scanned_products += result.scanned_products
            combined.matched_products += result.matched_products
            combined.errors.extend(result.errors)

        # 중복 제거 (같은 크림 상품 ID)
        seen_ids = set()
        unique = []
        for op in combined.opportunities:
            if op.kream_product.product_id not in seen_ids:
                seen_ids.add(op.kream_product.product_id)
                unique.append(op)
        combined.opportunities = sorted(unique, key=lambda o: -o.best_profit)
        combined.finished_at = datetime.now()

        logger.info(
            "전체 스캔 완료 | 검색 %d → 매칭 %d → 수익기회 %d",
            combined.scanned_products, combined.matched_products, len(combined.opportunities),
        )

        return combined

    async def scan_single_product(
        self, kream_product_id: str, retail_products: list[RetailProduct] | None = None
    ) -> ProfitOpportunity | None:
        """단일 크림 상품 스캔 (집중 추적용)."""
        kream_product = await kream_crawler.get_full_product_info(kream_product_id)
        if not kream_product:
            return None

        if not retail_products:
            return None

        return analyze_opportunity(kream_product, retail_products)

    async def compare_by_model(self, model_number: str) -> ProfitOpportunity | None:
        """모델번호로 전 사이트 비교 (!비교 명령어용).

        크림 + 무신사에서 같은 모델번호 상품을 찾아 비교.
        """
        normalized = normalize_model_number(model_number)
        logger.info("모델번호 비교: %s", normalized)

        # 크림에서 검색
        kream_results = await kream_crawler.search_product(model_number)
        kream_product = None

        for kr in kream_results[:5]:  # 최대 5건만 상세 조회
            detail = await kream_crawler.get_product_detail(kr["product_id"])
            if detail and detail.model_number:
                from src.matcher import model_numbers_match
                if model_numbers_match(detail.model_number, model_number):
                    kream_product = await kream_crawler.get_full_product_info(kr["product_id"])
                    break

        if not kream_product:
            logger.info("크림에서 매칭 상품 없음: %s", model_number)
            return None

        # 무신사에서 검색
        retail_products = []
        musinsa_results = await musinsa_crawler.search_products(model_number)
        for mr in musinsa_results[:5]:
            detail = await musinsa_crawler.get_product_detail(mr["product_id"])
            if detail and detail.model_number:
                from src.matcher import model_numbers_match
                if model_numbers_match(detail.model_number, model_number):
                    retail_products.append(detail)

        if not retail_products:
            logger.info("리테일 사이트에서 매칭 상품 없음: %s", model_number)
            # 크림 정보만이라도 반환
            return ProfitOpportunity(
                kream_product=kream_product,
                retail_products=[],
                size_profits=[],
                best_profit=0,
                best_roi=0.0,
                signal=Signal.NOT_RECOMMENDED,
            )

        return analyze_opportunity(kream_product, retail_products)

    async def quick_test(self, model_number: str) -> dict:
        """단건 빠른 테스트 — 전체 파이프라인 검증용.

        무신사 검색 → 상세 조회(품절 필터) → 크림 매칭 → 수익 계산 → 시그널 판정.
        진단 정보를 포함한 dict 반환.
        """
        import time
        start = time.monotonic()
        diag: dict = {
            "model_number": model_number,
            "musinsa_found": False,
            "musinsa_pid": None,
            "musinsa_name": None,
            "total_sizes": 0,
            "in_stock_sizes": 0,
            "stock_filter_applied": "없음",
            "kream_matched": False,
            "kream_name": None,
            "kream_volume_7d": 0,
            "best_profit": 0,
            "best_roi": 0.0,
            "signal": None,
            "alert_sent": False,
            "elapsed_sec": 0.0,
            "error": None,
        }
        try:
            normalized = normalize_model_number(model_number)

            # 1) 무신사 검색
            musinsa_results = await musinsa_crawler.search_products(model_number)
            if not musinsa_results:
                diag["error"] = "무신사 검색 결과 없음"
                return diag

            # 모델번호 매칭되는 첫 상품 상세 조회
            retail_product = None
            for mr in musinsa_results[:5]:
                detail = await musinsa_crawler.get_product_detail(mr["product_id"])
                if detail and detail.model_number:
                    if model_numbers_match(detail.model_number, model_number):
                        retail_product = detail
                        break

            if not retail_product:
                diag["error"] = "무신사에서 모델번호 매칭 실패"
                return diag

            diag["musinsa_found"] = True
            diag["musinsa_pid"] = retail_product.product_id
            diag["musinsa_name"] = retail_product.name
            diag["total_sizes"] = len(retail_product.sizes)
            diag["in_stock_sizes"] = sum(1 for s in retail_product.sizes if s.in_stock)

            # 품절 필터 진단
            if diag["total_sizes"] != diag["in_stock_sizes"]:
                diag["stock_filter_applied"] = "API 또는 DOM 크로스체크"
            elif diag["total_sizes"] > 0:
                diag["stock_filter_applied"] = "전체 재고있음 (필터 불필요 또는 미적용)"

            # 2) 크림 매칭
            kream_product = await find_kream_match(retail_product, self.db)
            if not kream_product:
                diag["error"] = "크림 매칭 상품 없음"
                return diag

            # find_kream_match는 DB 기본 정보만 반환 (sizes/volume 없음)
            # 시세+거래량 수집을 위해 풀 데이터 가져오기
            if not kream_product.size_prices:
                full_product = await self._get_kream_with_cache(kream_product.product_id)
                if full_product:
                    kream_product = full_product

            diag["kream_matched"] = True
            diag["kream_name"] = kream_product.name
            diag["kream_volume_7d"] = kream_product.volume_7d

            # 3) 수익 분석
            opportunity = analyze_opportunity(kream_product, [retail_product])
            if not opportunity or not opportunity.size_profits:
                diag["error"] = "수익 분석 실패 (사이즈 매칭 없음)"
                return diag

            # 4) 시그널 판정
            signal = determine_signal(opportunity.best_profit, kream_product.volume_7d)
            opportunity.signal = signal
            diag["best_profit"] = opportunity.best_profit
            diag["best_roi"] = opportunity.best_roi
            diag["signal"] = signal.value
            diag["opportunity"] = opportunity

        except Exception as e:
            diag["error"] = str(e)
            logger.error("빠른테스트 실패 (%s): %s", model_number, e)
        finally:
            diag["elapsed_sec"] = round(time.monotonic() - start, 1)

        return diag

    # ─── 자동스캔 (크림 기준 역방향 파이프라인) ───────────────

    async def auto_scan(
        self,
        on_opportunity=None,
        on_progress=None,
    ) -> AutoScanResult:
        """자동 스캔 — 매칭 DB 기반 가격 갱신 또는 크림 인기상품 탐색.

        매칭 DB가 있으면: 기존 매칭 상품의 가격만 빠르게 갱신
        매칭 DB가 없으면: 크림 인기상품 기반 탐색 (기존 방식)
        """
        matched_count = await self.db.get_matched_count()
        if matched_count > 0:
            logger.info("=== 자동스캔 시작 (매칭 DB %d건 기반) ===", matched_count)
            return await self._auto_scan_matched(on_opportunity, on_progress)
        else:
            logger.info("=== 자동스캔 시작 (매칭 DB 없음 → 인기상품 탐색) ===")
            return await self._auto_scan_popular(on_opportunity, on_progress)

    async def _auto_scan_popular(
        self,
        on_opportunity=None,
        on_progress=None,
    ) -> AutoScanResult:
        """크림 인기상품 기준 자동 스캔 (매칭 DB 없을 때 폴백)."""
        result = AutoScanResult()

        if on_progress:
            await on_progress("크림 인기상품 수집 중...")

        # 1단계: 크림 인기상품 수집 (여러 소스 병합)
        kream_products_raw = await self._collect_kream_popular()
        if not kream_products_raw:
            result.errors.append("크림 인기상품 수집 실패")
            result.finished_at = datetime.now()
            return result

        max_products = settings.auto_scan_max_products
        kream_products_raw = kream_products_raw[:max_products]

        logger.info("크림 인기상품 %d건 수집 완료", len(kream_products_raw))
        if on_progress:
            await on_progress(
                f"크림 인기상품 {len(kream_products_raw)}건 수집 완료. "
                f"상세 조회 + 무신사 매칭 시작..."
            )

        # 2단계: 상세 정보 수집 + 무신사 매칭 (병렬 처리)
        semaphore = asyncio.Semaphore(settings.auto_scan_concurrency)
        seen_models: set[str] = set()

        async def process_product(idx: int, item: dict) -> AutoScanOpportunity | None:
            """단일 크림 상품 처리 (세마포어로 동시성 제한)."""
            async with semaphore:
                try:
                    product_id = item["product_id"]
                    result.kream_scanned += 1

                    # 진행 상황 업데이트
                    if on_progress and idx % 5 == 0:
                        await on_progress(
                            f"진행 중... {idx + 1}/{len(kream_products_raw)} "
                            f"(매칭 {result.matched}건, 에러 {len(result.errors)}건)"
                        )

                    # 크림 상세 정보 수집 (캐시 확인)
                    kream_product = await self._get_kream_with_cache(product_id)
                    if not kream_product:
                        return None

                    model_number = normalize_model_number(
                        kream_product.model_number
                        or item.get("model_number", "")
                    )
                    if not model_number:
                        return None

                    # 중복 모델번호 스킵
                    if model_number in seen_models:
                        return None
                    seen_models.add(model_number)

                    # 리테일 검색 (무신사 + 29CM 병렬)
                    result.musinsa_searched += 1
                    retail_result = await self._search_retail_for_model(
                        model_number, kream_product.name,
                        kream_brand=kream_product.brand or "",
                    )
                    if not retail_result:
                        return None

                    merged_sizes, best_url, best_name, best_pid, source_prices = retail_result
                    if not merged_sizes:
                        return None

                    result.matched += 1

                    # 2단계 수익 분석
                    opportunity = analyze_auto_scan_opportunity(
                        kream_product=kream_product,
                        musinsa_sizes=merged_sizes,
                        musinsa_url=best_url,
                        musinsa_name=best_name,
                        musinsa_product_id=best_pid,
                    )
                    if opportunity:
                        opportunity.source_prices = source_prices

                    if opportunity and (
                        opportunity.best_confirmed_profit > 0
                        or opportunity.best_estimated_profit > 0
                    ):
                        # DB 저장
                        await self.db.upsert_kream_product(
                            product_id=kream_product.product_id,
                            name=kream_product.name,
                            model_number=model_number,
                            brand=kream_product.brand,
                            image_url=kream_product.image_url,
                            url=kream_product.url,
                        )

                        # 시그널 판정 + BUY 이상만 알림
                        signal = determine_signal(
                            opportunity.best_confirmed_profit,
                            opportunity.volume_7d,
                        )
                        opportunity.signal = signal
                        if on_opportunity and signal in (Signal.STRONG_BUY, Signal.BUY):
                            await on_opportunity(opportunity)
                        elif signal in (Signal.WATCH, Signal.NOT_RECOMMENDED):
                            logger.debug(
                                "알림 스킵: signal=%s profit=%s",
                                signal.value, opportunity.best_confirmed_profit,
                            )

                        return opportunity

                except Exception as e:
                    result.errors.append(
                        f"처리 실패 ({item.get('product_id', '?')}): {e}"
                    )
                    logger.error("자동스캔 상품 처리 실패: %s", e)
                return None

        # 거래량 많은 상품부터 우선 처리
        kream_products_raw.sort(
            key=lambda x: x.get("volume_7d", 0), reverse=True
        )

        # 병렬 처리
        scan_tasks = [
            process_product(i, item)
            for i, item in enumerate(kream_products_raw)
        ]
        gathered = await asyncio.gather(*scan_tasks, return_exceptions=True)

        for r in gathered:
            if isinstance(r, Exception):
                result.errors.append(f"병렬 처리 예외: {r}")
                logger.error("자동스캔 병렬 처리 예외: %s", r)
            elif isinstance(r, AutoScanOpportunity):
                result.opportunities.append(r)

        # 확정 수익 높은 순 → 예상 수익 높은 순으로 정렬
        result.opportunities.sort(
            key=lambda o: (-o.best_confirmed_profit, -o.best_estimated_profit)
        )
        result.finished_at = datetime.now()

        elapsed = (result.finished_at - result.started_at).total_seconds()
        logger.info(
            "=== 자동스캔 완료 (%.0f초) | 크림 %d → 리테일 %d → 매칭 %d → "
            "수익기회 %d (확정 %d / 예상 %d) | 에러 %d ===",
            elapsed,
            result.kream_scanned,
            result.musinsa_searched,
            result.matched,
            len(result.opportunities),
            result.confirmed_count,
            result.estimated_count,
            len(result.errors),
        )

        return result

    async def _auto_scan_matched(
        self,
        on_opportunity=None,
        on_progress=None,
    ) -> AutoScanResult:
        """매칭 DB 기반 가격 갱신 스캔 (검색 없이 빠르게)."""
        result = AutoScanResult()

        if on_progress:
            await on_progress("매칭 DB 기반 가격 갱신 시작...")

        max_products = settings.auto_scan_max_products
        matched_rows = await self.db.get_matched_kream_products(limit=max_products)
        if not matched_rows:
            result.finished_at = datetime.now()
            return result

        logger.info("매칭 DB %d건 가격 갱신 시작", len(matched_rows))
        if on_progress:
            await on_progress(f"매칭 상품 {len(matched_rows)}건 가격 갱신 중...")

        semaphore = asyncio.Semaphore(settings.auto_scan_concurrency)

        async def process_matched(idx: int, row) -> AutoScanOpportunity | None:
            async with semaphore:
                try:
                    product_id = row["product_id"]
                    model_number = row["model_number"]
                    result.kream_scanned += 1

                    if on_progress and idx % 10 == 0 and idx > 0:
                        await on_progress(
                            f"가격 갱신 {idx}/{len(matched_rows)} "
                            f"(수익기회 {len(result.opportunities)}건)"
                        )

                    # 크림 가격 수집
                    kream_product = await self._get_kream_with_cache(product_id)
                    if not kream_product or not kream_product.size_prices:
                        return None

                    # 리테일 가격 수집 (DB에서 알려진 상품)
                    result.musinsa_searched += 1
                    retail_result = await self._refresh_retail_for_model(model_number)
                    if not retail_result:
                        return None

                    merged_sizes, best_url, best_name, best_pid, source_prices = retail_result
                    result.matched += 1

                    # 수익 분석
                    opportunity = analyze_auto_scan_opportunity(
                        kream_product=kream_product,
                        musinsa_sizes=merged_sizes,
                        musinsa_url=best_url,
                        musinsa_name=best_name,
                        musinsa_product_id=best_pid,
                    )
                    if opportunity:
                        opportunity.source_prices = source_prices

                    if opportunity and (
                        opportunity.best_confirmed_profit > 0
                        or opportunity.best_estimated_profit > 0
                    ):
                        signal = determine_signal(
                            opportunity.best_confirmed_profit,
                            opportunity.volume_7d,
                        )
                        opportunity.signal = signal
                        if on_opportunity and signal in (Signal.STRONG_BUY, Signal.BUY):
                            await on_opportunity(opportunity)
                        elif signal in (Signal.WATCH, Signal.NOT_RECOMMENDED):
                            logger.debug(
                                "알림 스킵: signal=%s profit=%s",
                                signal.value, opportunity.best_confirmed_profit,
                            )
                        return opportunity

                except Exception as e:
                    result.errors.append(f"갱신 실패 ({row['product_id']}): {e}")
                    logger.error("매칭 가격갱신 실패 (%s): %s", row["product_id"], e)
                return None

        tasks = [process_matched(i, row) for i, row in enumerate(matched_rows)]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        for r in gathered:
            if isinstance(r, Exception):
                result.errors.append(f"병렬 처리 예외: {r}")
            elif isinstance(r, AutoScanOpportunity):
                result.opportunities.append(r)

        result.opportunities.sort(
            key=lambda o: (-o.best_confirmed_profit, -o.best_estimated_profit)
        )
        result.finished_at = datetime.now()

        elapsed = (result.finished_at - result.started_at).total_seconds()
        logger.info(
            "=== 자동스캔 완료 (%.0f초) | 매칭DB %d → 가격갱신 %d → 수익기회 %d ===",
            elapsed, result.kream_scanned, result.matched, len(result.opportunities),
        )
        return result

    async def _refresh_retail_for_model(
        self, model_number: str,
    ) -> tuple[dict[str, tuple[int, bool]], str, str, str, dict[str, int]] | None:
        """기존 매칭 상품의 가격만 갱신 (검색 없이)."""
        from src.profit_calculator import _normalize_size

        retail_rows = await self.db.find_retail_by_model(model_number)
        if not retail_rows:
            return None

        merged_sizes: dict[str, tuple[int, bool]] = {}
        source_prices: dict[str, int] = {}
        best_url = ""
        best_name = ""
        best_pid = ""
        best_min_price = float("inf")

        for rr in retail_rows:
            source = rr["source"]
            pid = rr["product_id"]

            try:
                if source == "musinsa":
                    detail = await musinsa_crawler.get_product_detail(pid)
                elif source == "29cm":
                    detail = await twentynine_cm_crawler.get_product_detail(pid)
                else:
                    continue

                if not detail or not detail.sizes:
                    continue

                sizes: dict[str, tuple[int, bool]] = {}
                for s in detail.sizes:
                    if s.in_stock and s.price > 0:
                        ns = _normalize_size(s.size)
                        if ns not in sizes or s.price < sizes[ns][0]:
                            sizes[ns] = (s.price, True)

                if sizes:
                    cur_min = min(p for p, _ in sizes.values())
                    label = "무신사" if source == "musinsa" else "29CM"
                    source_prices[label] = cur_min

                    if cur_min < best_min_price:
                        best_min_price = cur_min
                        best_url = rr["url"]
                        best_name = rr["name"]
                        best_pid = pid

                    for sk, sv in sizes.items():
                        if sk not in merged_sizes or sv[0] < merged_sizes[sk][0]:
                            merged_sizes[sk] = sv
            except Exception as e:
                logger.error("리테일 가격 갱신 실패 (%s/%s): %s", source, pid, e)

        if not merged_sizes:
            return None

        return (merged_sizes, best_url, best_name, best_pid, source_prices)

    # ─── 배치스캔 (전체 DB 순회) ─────────────────────────

    async def batch_scan(
        self,
        on_opportunity=None,
        on_progress=None,
    ) -> BatchScanResult:
        """전체 DB 배치 스캔.

        kream_products 전체를 500개씩 순회하며 리테일 매칭을 수행한다.
        이미 매칭된 상품은 검색 스킵, 미매칭 상품만 리테일 검색.
        """
        result = BatchScanResult()
        self._batch_scan_stop = False

        # 배치스캔 중 매칭 검토는 터미널 로그만 (디스코드 비활성화)
        saved_callback = self._match_review_callback
        self._match_review_callback = None

        try:
            total = await self.db.get_kream_product_count()
            result.total = total
            batch_size = 500
            total_batches = (total + batch_size - 1) // batch_size

            print(f"[DEBUG batch_scan] 시작: total={total}, batches={total_batches}")
            logger.info(
                "=== 배치스캔 시작: 총 %d개, %d배치 ===", total, total_batches,
            )
            if on_progress:
                await on_progress(
                    f"배치스캔 시작 — 총 {total:,}개 상품, {total_batches}배치"
                )

            semaphore = asyncio.Semaphore(settings.auto_scan_concurrency)

            for batch_idx in range(total_batches):
                if self._batch_scan_stop:
                    logger.info("배치스캔 중지 요청 수신")
                    break

                offset = batch_idx * batch_size
                products = await self.db.get_kream_products_batch(offset, batch_size)
                print(f"[DEBUG batch_scan] 배치 {batch_idx+1}/{total_batches} DB조회 완료: {len(products)}건")

                async def process_one(row) -> AutoScanOpportunity | None:
                    async with semaphore:
                        model_number = row["model_number"]
                        if not model_number:
                            return None

                        # 무신사/29CM에서 안 파는 브랜드 스킵
                        brand = (row["brand"] or "").strip()
                        if brand in _SKIP_BRANDS:
                            result.brand_skipped += 1
                            return None

                        # 이미 매칭 있으면 스킵
                        existing = await self.db.find_retail_by_model(model_number)
                        if existing:
                            result.already_matched += 1
                            return None

                        result.processed += 1
                        print(f"[DEBUG process_one] #{result.processed} 검색: {model_number} ({row['name'][:30]})")

                        # 리테일 검색 (무신사 + 29CM 병렬)
                        try:
                            retail_result = await self._search_retail_for_model(
                                model_number, row["name"],
                                kream_brand=row["brand"] or "",
                            )
                        except Exception as e:
                            print(f"[DEBUG process_one] 검색 예외: {model_number} → {e}")
                            result.errors.append(f"검색 실패 ({model_number}): {e}")
                            return None

                        if not retail_result:
                            result.no_match += 1
                            return None

                        merged_sizes, best_url, best_name, best_pid, source_prices = retail_result
                        if not merged_sizes:
                            result.no_match += 1
                            return None

                        result.new_matched += 1
                        print(f"[DEBUG process_one] ✅ 매칭 성공: {model_number} → {best_name[:30]} ({best_url[:50]})")

                        # 리테일 상품 DB 저장
                        source = "29cm" if "29cm" in best_url else "musinsa"
                        await self.db.upsert_retail_product(
                            source=source,
                            product_id=best_pid,
                            name=best_name,
                            model_number=model_number,
                            brand=row["brand"] or "",
                            url=best_url,
                        )

                        # 크림 상세 조회 + 수익 분석
                        try:
                            kream_product = await self._get_kream_with_cache(
                                row["product_id"],
                            )
                            if kream_product and kream_product.size_prices:
                                opportunity = analyze_auto_scan_opportunity(
                                    kream_product=kream_product,
                                    musinsa_sizes=merged_sizes,
                                    musinsa_url=best_url,
                                    musinsa_name=best_name,
                                    musinsa_product_id=best_pid,
                                )
                                if opportunity:
                                    opportunity.source_prices = source_prices
                                if opportunity and (
                                    opportunity.best_confirmed_profit > 0
                                    or opportunity.best_estimated_profit > 0
                                ):
                                    signal = determine_signal(
                                        opportunity.best_confirmed_profit,
                                        opportunity.volume_7d,
                                    )
                                    opportunity.signal = signal
                                    if on_opportunity and signal in (
                                        Signal.STRONG_BUY, Signal.BUY,
                                    ):
                                        await on_opportunity(opportunity)
                                    return opportunity
                        except Exception as e:
                            logger.error(
                                "배치스캔 수익분석 실패 (%s): %s",
                                row["product_id"], e,
                            )

                        return None

                # 배치 내 병렬 처리
                print(f"[DEBUG batch_scan] 배치 {batch_idx+1} gather 시작: {len(products)}태스크")
                batch_tasks = [process_one(row) for row in products]
                gathered = await asyncio.gather(
                    *batch_tasks, return_exceptions=True,
                )
                print(f"[DEBUG batch_scan] 배치 {batch_idx+1} gather 완료")

                for r in gathered:
                    if isinstance(r, Exception):
                        print(f"[DEBUG batch_scan] gather 예외: {r}")
                        result.errors.append(str(r))
                    elif isinstance(r, AutoScanOpportunity):
                        result.opportunities.append(r)

                # 진행률 알림
                if on_progress:
                    await on_progress(
                        f"배치 {batch_idx + 1}/{total_batches} 완료 — "
                        f"매칭 {result.new_matched}건, "
                        f"수익기회 {len(result.opportunities)}건, "
                        f"스킵 {result.already_matched}건, "
                        f"브랜드스킵 {result.brand_skipped}건"
                    )

                logger.info(
                    "배치 %d/%d 완료: 새매칭 %d, 스킵 %d, 브랜드스킵 %d, 미매칭 %d, 수익기회 %d",
                    batch_idx + 1, total_batches,
                    result.new_matched, result.already_matched,
                    result.brand_skipped,
                    result.no_match, len(result.opportunities),
                )

                # 배치 간 딜레이 30초 (차단 방지)
                if batch_idx < total_batches - 1 and not self._batch_scan_stop:
                    print(f"[DEBUG batch_scan] 배치 {batch_idx+1} 완료, 30초 대기...")
                    await asyncio.sleep(30)

            result.finished_at = datetime.now()
            elapsed = (result.finished_at - result.started_at).total_seconds()

            logger.info(
                "=== 배치스캔 %s (%.0f초) | 총 %d → 브랜드스킵 %d → 검색 %d → 매칭 %d → "
                "수익기회 %d | 기존매칭 %d | 에러 %d ===",
                "완료" if not self._batch_scan_stop else "중지",
                elapsed, result.total, result.brand_skipped, result.processed,
                result.new_matched, len(result.opportunities),
                result.already_matched, len(result.errors),
            )

            return result

        finally:
            # 매칭 검토 콜백 복원
            self._match_review_callback = saved_callback

    def stop_batch_scan(self) -> None:
        """배치스캔 중지 요청."""
        self._batch_scan_stop = True
        logger.info("배치스캔 중지 요청됨")

    # ─── 역방향 스캔 (전략 C: 브랜드별 무신사 검색 → 크림 DB 매칭) ──────

    # 크림 브랜드명 → 무신사 검색 키워드 매핑 (주요 30개 한글 하드코딩)
    _BRAND_SEARCH_QUERIES: dict[str, list[str]] = {
        "Nike": ["나이키"],
        "Adidas": ["아디다스"],
        "New Balance": ["뉴발란스"],
        "Jordan": ["조던"],
        "Converse": ["컨버스"],
        "Vans": ["반스"],
        "Puma": ["푸마"],
        "Asics": ["아식스"],
        "Reebok": ["리복"],
        "The North Face": ["노스페이스"],
        "Arc'teryx": ["아크테릭스"],
        "Patagonia": ["파타고니아"],
        "Stussy": ["스투시"],
        "Carhartt WIP": ["칼하트"],
        "Carhartt": ["칼하트"],
        "New Era": ["뉴에라"],
        "Palace": ["팔라스"],
        "Sacai": ["사카이"],
        "Kith": ["키스"],
        "Aime Leon Dore": ["에메레온도레"],
        "Salomon": ["살로몬"],
        "On": ["온러닝"],
        "Hoka": ["호카"],
        "Saucony": ["사코니"],
        "Crocs": ["크록스"],
        "Birkenstock": ["버켄스탁"],
        "Dr. Martens": ["닥터마틴"],
        "Timberland": ["팀버랜드"],
        "Columbia": ["컬럼비아"],
        "Descente": ["데상트"],
    }

    async def reverse_scan(
        self,
        on_opportunity=None,
        on_progress=None,
        max_results_per_brand: int = 10,
        max_brands: int = 0,
    ) -> ReverseScanResult:
        """역방향 스캔: 크림 DB 브랜드별 무신사 검색 → 모델번호 매칭 → 수익 분석.

        전략 C: 크림 DB TOP 브랜드의 한글명으로 무신사에서 직접 검색.
        세일 페이지 크롤링 대신 search_products()를 브랜드별로 호출한다.
        정가 상품도 크림보다 싸면 수익기회로 포함.

        Args:
            on_opportunity: 수익 기회 발견 시 콜백 (실시간 알림용)
            on_progress: 진행 상황 콜백
            max_results_per_brand: 브랜드당 상세 조회할 최대 상품 수
        """
        from src.profit_calculator import _normalize_size

        result = ReverseScanResult()

        # 1단계: 크림 DB에서 상품 10개 이상 브랜드 → 무신사 검색 키워드 매핑
        all_brands = await self.db.get_brands_min_count(min_count=10)
        search_brands: list[tuple[str, str]] = []  # [(brand, search_query), ...]

        for brand in all_brands:
            if brand in _SKIP_BRANDS:
                continue
            queries = self._BRAND_SEARCH_QUERIES.get(brand)
            if queries:
                for q in queries:
                    search_brands.append((brand, q))
            else:
                # 한글 매핑 없으면 영문 브랜드명 그대로 검색
                search_brands.append((brand, brand))

        # max_brands > 0이면 브랜드 수 제한 (테스트 모드)
        if max_brands > 0:
            search_brands = search_brands[:max_brands]

        logger.info(
            "=== 역방향 스캔 시작: %d개 브랜드 검색 ===", len(search_brands),
        )
        if on_progress:
            brand_names = ", ".join(b for b, _ in search_brands[:8])
            await on_progress(
                f"브랜드별 무신사 검색 시작 ({len(search_brands)}개: {brand_names}...)"
            )

        # 2단계: 브랜드별 무신사 검색 → 상품 수집
        all_items: list[dict] = []  # [{"product_id", "brand", "search_query"}, ...]
        seen_pids: set[str] = set()

        for brand, query in search_brands:
            try:
                search_results = await musinsa_crawler.search_products(query)
                added = 0
                for item in search_results:
                    pid = item["product_id"]
                    if pid not in seen_pids:
                        seen_pids.add(pid)
                        item["_brand"] = brand
                        item["_query"] = query
                        all_items.append(item)
                        added += 1
                        if added >= max_results_per_brand:
                            break

                logger.info(
                    "역방향 검색: '%s' (%s) → %d건 검색, %d건 추가",
                    query, brand, len(search_results), added,
                )
            except Exception as e:
                result.errors.append(f"검색 실패 ({brand}/{query}): {e}")
                logger.error("역방향 검색 실패 (%s): %s", query, e)

        result.sale_collected = len(all_items)
        logger.info("역방향 검색 완료: 총 %d건 수집", len(all_items))

        if not all_items:
            result.finished_at = datetime.now()
            return result

        if on_progress:
            await on_progress(
                f"무신사 {len(all_items)}건 수집 완료. "
                f"상세 조회 + 크림 DB 매칭 시작..."
            )

        # 3단계: 각 상품 상세 조회 → 모델번호 → 크림 DB 매칭 → 수익 분석
        semaphore = asyncio.Semaphore(settings.auto_scan_concurrency)
        seen_models: set[str] = set()

        async def process_item(
            idx: int, item: dict,
        ) -> AutoScanOpportunity | None:
            async with semaphore:
                try:
                    product_id = item["product_id"]
                    result.detail_fetched += 1

                    if on_progress and idx % 10 == 0 and idx > 0:
                        await on_progress(
                            f"진행 {idx}/{len(all_items)} "
                            f"(DB매칭 {result.db_matched}건, "
                            f"수익기회 {len(result.opportunities)}건)"
                        )

                    # 상세 페이지에서 모델번호/사이즈/가격 수집
                    retail_product = await musinsa_crawler.get_product_detail(
                        product_id,
                    )
                    if not retail_product or not retail_product.model_number:
                        return None

                    normalized = normalize_model_number(
                        retail_product.model_number,
                    )
                    if not normalized:
                        return None

                    # 중복 모델번호 스킵
                    if normalized in seen_models:
                        return None
                    seen_models.add(normalized)

                    # 크림 DB 매칭 (복수 매칭 시 콜라보 제외)
                    rows = await self.db.find_kream_all_by_model(normalized)
                    if rows:
                        row = _pick_best_kream_match(rows, retail_product.name)
                    else:
                        row = await self.db.search_kream_by_model_like(normalized)
                    if not row:
                        return None

                    result.db_matched += 1

                    # 크림 가격: DB 우선, 없으면 API 호출
                    # sqlite3.Row는 .get() 불가 → dict 변환
                    row = dict(row)
                    kream_product = await self._get_kream_from_db_or_api(
                        row["product_id"], row,
                    )
                    if not kream_product or not kream_product.size_prices:
                        return None

                    # 무신사 사이즈맵 구축
                    musinsa_sizes: dict[str, tuple[int, bool]] = {}
                    for s in retail_product.sizes:
                        if s.in_stock and s.price > 0:
                            ns = _normalize_size(s.size)
                            if ns not in musinsa_sizes or s.price < musinsa_sizes[ns][0]:
                                musinsa_sizes[ns] = (s.price, True)

                    if not musinsa_sizes:
                        return None

                    # 가격 이상치 체크: 크림 최고가가 무신사의 200% 이상이면 오매칭 의심
                    max_kream = max(
                        (sp.sell_now_price for sp in kream_product.size_prices
                         if sp.sell_now_price),
                        default=0,
                    )
                    min_musinsa = min(
                        musinsa_sizes.values(), key=lambda x: x[0],
                    )[0] if musinsa_sizes else 0
                    if (max_kream > 0 and min_musinsa > 0
                            and max_kream > min_musinsa * 2):
                        logger.warning(
                            "가격 이상치: %s — 무신사 %s원 vs 크림 %s원 (%.0f%%)",
                            kream_product.name[:30],
                            f"{min_musinsa:,}", f"{max_kream:,}",
                            max_kream / min_musinsa * 100,
                        )

                    # 수익 분석 (할인가든 정가든 크림보다 싸면 수익기회)
                    opportunity = analyze_auto_scan_opportunity(
                        kream_product=kream_product,
                        musinsa_sizes=musinsa_sizes,
                        musinsa_url=retail_product.url,
                        musinsa_name=retail_product.name,
                        musinsa_product_id=retail_product.product_id,
                    )
                    if not opportunity:
                        return None

                    opportunity.source_prices = {"무신사": min(
                        p for p, _ in musinsa_sizes.values()
                    )}

                    # 수익 있는 건만 (정가도 크림보다 싸면 수익기회)
                    if (
                        opportunity.best_confirmed_profit <= 0
                        and opportunity.best_estimated_profit <= 0
                    ):
                        return None

                    # 리테일 상품 DB 저장 (매칭 성공한 것만)
                    await self.db.upsert_retail_product(
                        source="musinsa",
                        product_id=retail_product.product_id,
                        name=retail_product.name,
                        model_number=normalized,
                        brand=item.get("_brand", ""),
                        url=retail_product.url,
                        image_url=retail_product.image_url,
                    )

                    # 시그널 판정
                    signal = determine_signal(
                        opportunity.best_confirmed_profit,
                        opportunity.volume_7d,
                    )
                    opportunity.signal = signal

                    # 실시간 알림 — BUY 이상만 전송
                    if on_opportunity and signal in (Signal.STRONG_BUY, Signal.BUY):
                        try:
                            await on_opportunity(opportunity)
                        except Exception as e:
                            logger.error(
                                "역방향 알림 콜백 실패: %s — %s",
                                kream_product.name[:30], e,
                            )

                    elif signal in (Signal.WATCH, Signal.NOT_RECOMMENDED):
                        logger.debug(
                            "알림 스킵: signal=%s profit=%s",
                            signal.value, opportunity.best_confirmed_profit,
                        )

                    return opportunity

                except Exception as e:
                    result.errors.append(
                        f"역방향 처리 실패 ({item.get('product_id', '?')}): {e}"
                    )
                    logger.error("역방향 스캔 상품 처리 실패: %s", e)
                    return None

        # 병렬 처리
        tasks = [
            process_item(i, item)
            for i, item in enumerate(all_items)
        ]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        for r in gathered:
            if isinstance(r, Exception):
                result.errors.append(f"병렬 처리 예외: {r}")
                logger.error("역방향 스캔 병렬 예외: %s", r)
            elif isinstance(r, AutoScanOpportunity):
                result.opportunities.append(r)

        # 확정 수익 높은 순 정렬
        result.opportunities.sort(
            key=lambda o: (-o.best_confirmed_profit, -o.best_estimated_profit)
        )
        result.finished_at = datetime.now()

        elapsed = (result.finished_at - result.started_at).total_seconds()
        logger.info(
            "=== 역방향 스캔 완료 (%.0f초) | 브랜드 %d → 검색 %d → 상세 %d → "
            "DB매칭 %d → 수익기회 %d (확정 %d / 예상 %d) | 에러 %d ===",
            elapsed,
            len(search_brands),
            result.sale_collected,
            result.detail_fetched,
            result.db_matched,
            len(result.opportunities),
            result.confirmed_count,
            result.estimated_count,
            len(result.errors),
        )

        return result

    async def run_category_scan(
        self,
        categories: list[str] | None = None,
        max_pages: int = 30,
        on_opportunity=None,
        on_progress=None,
        resume: bool = True,
    ) -> CategoryScanResult:
        """카테고리 기반 전체 페이지 크롤링.

        무신사 카테고리 페이지를 스크롤하여 리스팅 수집 → 4단계 필터 → 크림 DB 매칭 → 수익 분석.

        Args:
            categories: 스캔할 카테고리 코드 목록. None이면 ["103"] (신발).
            max_pages: 카테고리당 최대 페이지 수 (1페이지 = 60건)
            on_opportunity: 수익 기회 발견 시 콜백
            on_progress: 진행 상황 콜백
            resume: True면 이전 스캔 이어서, False면 처음부터 (미사용, 향후 확장)
        """
        from src.crawlers.musinsa import musinsa_crawler
        from src.profit_calculator import _normalize_size

        result = CategoryScanResult()

        if categories is None:
            categories = ["103"]  # 기본: 신발

        # 1단계: 사전 데이터 로딩
        # 이미 스캔한 상품 SET
        if not resume:
            await self.db.clear_category_scan_history()
            scanned_set: set[str] = set()
            logger.info("카테고리 스캔 이력 초기화 완료")
        else:
            scanned_set = await self.db.load_scanned_goods_nos()

        logger.info(
            "=== 카테고리 스캔 시작: 카테고리 %s, 최대 %d페이지, "
            "이미스캔 %d건 ===",
            categories, max_pages, len(scanned_set),
        )

        if on_progress:
            cat_names = ", ".join(
                next(
                    (name for name, code in musinsa_crawler.CATEGORY_CODES.items()
                     if code == c),
                    c,
                )
                for c in categories
            )
            await on_progress(
                f"카테고리 스캔 시작: {cat_names} ({max_pages}페이지씩)"
            )

        seen_models: set[str] = set()

        for category in categories:
            # 스크롤+인터셉트로 전체 리스팅 수집 (한 번에)
            if on_progress:
                await on_progress(
                    f"카테고리 [{category}] 리스팅 수집 중... ({max_pages}페이지 스크롤)"
                )

            listing = await musinsa_crawler.fetch_category_listing(
                category=category, max_pages=max_pages,
            )

            if not listing:
                logger.info("카테고리 %s: 리스팅 0건 → 스캔 종료", category)
                continue

            result.listing_fetched += len(listing)
            result.pages_scanned += len(listing) // 60 + (1 if len(listing) % 60 else 0)

            if on_progress:
                await on_progress(
                    f"카테고리 [{category}] 리스팅 {len(listing)}건 수집 완료. 필터링 시작..."
                )

            # 브랜드 디버그 로그 (처음 5건)
            for i, item in enumerate(listing[:5]):
                logger.info(
                    "카테고리 리스팅 샘플 %d: brand=%s brandName=%s goodsName=%s",
                    i + 1, item.get("brand"), item.get("brandName"),
                    (item.get("goodsName") or "")[:40],
                )

            # 3단계 필터링 (브랜드 필터 제거 — 모델번호 매칭으로만 판단)
            detail_queue: list[dict] = []
            name_match_queue: list[tuple[dict, str]] = []

            for item in listing:
                goods_no = item["goodsNo"]

                # Layer 1: 품절/이미스캔 필터
                if item.get("isSoldOut"):
                    result.sold_out_skipped += 1
                    continue
                if goods_no in scanned_set:
                    result.already_scanned += 1
                    continue

                brand_slug = (item.get("brand") or "").strip().lower()

                # Layer 2: 상품명에서 모델번호 추출
                goods_name = item.get("goodsName", "")
                name_model = extract_model_from_name(goods_name)

                if name_model:
                    rows = await self.db.find_kream_all_by_model(name_model)
                    if rows:
                        row = _pick_best_kream_match(rows, goods_name)
                    else:
                        row = await self.db.search_kream_by_model_like(name_model)

                    if row:
                        result.name_matched += 1
                        name_match_queue.append((item, name_model))
                    else:
                        # 이름에서 추출한 모델번호가 DB에 없으면 상세 페이지에서 재시도
                        result.name_no_match += 1
                        detail_queue.append(item)
                else:
                    detail_queue.append(item)

            logger.info(
                "카테고리 %s 필터 결과: 총 %d → 품절 %d / 이미스캔 %d / "
                "이름매칭 %d / 이름미매칭 %d / 상세필요 %d",
                category, len(listing),
                result.sold_out_skipped, result.already_scanned,
                result.name_matched,
                result.name_no_match, len(detail_queue),
            )

            if on_progress:
                await on_progress(
                    f"카테고리 [{category}] 필터 완료\n"
                    f"• 리스팅 {len(listing)}건 → "
                    f"품절 {result.sold_out_skipped} / 이미스캔 {result.already_scanned}\n"
                    f"• 이름매칭 {result.name_matched}건 + "
                    f"상세방문 대기 {len(detail_queue)}건"
                )

            # 상세 방문 처리
            all_detail_items = [
                (item, model) for item, model in name_match_queue
            ] + [
                (item, "") for item in detail_queue
            ]

            for idx, (item, pre_model) in enumerate(all_detail_items):
                goods_no = item["goodsNo"]
                goods_name = item.get("goodsName", "")
                brand_slug = (item.get("brand") or "").strip()

                try:
                    result.detail_fetched += 1

                    # 진행 보고
                    if on_progress and idx > 0 and idx % 10 == 0:
                        await on_progress(
                            f"카테고리 [{category}] 상세 조회 {idx}/{len(all_detail_items)}\n"
                            f"• DB매칭 {result.detail_matched}건 / "
                            f"수익기회 {len(result.opportunities)}건"
                        )

                    retail_product = await musinsa_crawler.get_product_detail(
                        goods_no,
                    )
                    if not retail_product:
                        await self.db.save_category_scan(
                            goods_no=goods_no, category=category,
                            brand=brand_slug, goods_name=goods_name,
                            price=item.get("price", 0),
                        )
                        scanned_set.add(goods_no)
                        continue

                    model = pre_model or normalize_model_number(
                        retail_product.model_number,
                    )
                    if not model:
                        await self.db.save_category_scan(
                            goods_no=goods_no, category=category,
                            brand=brand_slug, goods_name=goods_name,
                            price=item.get("price", 0),
                        )
                        scanned_set.add(goods_no)
                        continue

                    if model in seen_models:
                        scanned_set.add(goods_no)
                        continue
                    seen_models.add(model)

                    rows = await self.db.find_kream_all_by_model(model)
                    if rows:
                        row = _pick_best_kream_match(rows, goods_name)
                    else:
                        row = await self.db.search_kream_by_model_like(model)
                    if not row:
                        await self.db.save_category_scan(
                            goods_no=goods_no, category=category,
                            brand=brand_slug, goods_name=goods_name,
                            model_number=model,
                            price=item.get("price", 0),
                        )
                        scanned_set.add(goods_no)
                        continue

                    result.detail_matched += 1

                    row = dict(row)
                    kream_product = await self._get_kream_from_db_or_api(
                        row["product_id"], row,
                    )
                    if not kream_product or not kream_product.size_prices:
                        await self.db.save_category_scan(
                            goods_no=goods_no, category=category,
                            brand=brand_slug, goods_name=goods_name,
                            model_number=model, kream_matched=True,
                            kream_product_id=row["product_id"],
                            price=item.get("price", 0),
                        )
                        scanned_set.add(goods_no)
                        continue

                    musinsa_sizes: dict[str, tuple[int, bool]] = {}
                    for s in retail_product.sizes:
                        if s.in_stock and s.price > 0:
                            ns = _normalize_size(s.size)
                            if ns not in musinsa_sizes or s.price < musinsa_sizes[ns][0]:
                                musinsa_sizes[ns] = (s.price, True)

                    if not musinsa_sizes:
                        await self.db.save_category_scan(
                            goods_no=goods_no, category=category,
                            brand=brand_slug, goods_name=goods_name,
                            model_number=model, kream_matched=True,
                            kream_product_id=row["product_id"],
                            price=item.get("price", 0),
                        )
                        scanned_set.add(goods_no)
                        continue

                    opportunity = analyze_auto_scan_opportunity(
                        kream_product=kream_product,
                        musinsa_sizes=musinsa_sizes,
                        musinsa_url=retail_product.url,
                        musinsa_name=retail_product.name,
                        musinsa_product_id=goods_no,
                    )

                    await self.db.save_category_scan(
                        goods_no=goods_no, category=category,
                        brand=brand_slug, goods_name=goods_name,
                        model_number=model, kream_matched=True,
                        kream_product_id=row["product_id"],
                        price=item.get("price", 0),
                    )
                    scanned_set.add(goods_no)

                    if not opportunity:
                        continue

                    opportunity.source_prices = {"무신사": min(
                        p for p, _ in musinsa_sizes.values()
                    )}

                    if (
                        opportunity.best_confirmed_profit <= 0
                        and opportunity.best_estimated_profit <= 0
                    ):
                        continue

                    await self.db.upsert_retail_product(
                        source="musinsa",
                        product_id=goods_no,
                        name=retail_product.name,
                        model_number=model,
                        brand=brand_slug,
                        url=retail_product.url,
                        image_url=retail_product.image_url,
                    )

                    # 시그널 판정
                    signal = determine_signal(
                        opportunity.best_confirmed_profit,
                        opportunity.volume_7d,
                    )
                    opportunity.signal = signal

                    result.opportunities.append(opportunity)

                    # BUY 이상만 알림 전송
                    if on_opportunity and signal in (Signal.STRONG_BUY, Signal.BUY):
                        try:
                            await on_opportunity(opportunity)
                        except Exception as e:
                            logger.error(
                                "카테고리 스캔 알림 콜백 실패: %s", e,
                            )
                    elif signal in (Signal.WATCH, Signal.NOT_RECOMMENDED):
                        logger.debug(
                            "알림 스킵: signal=%s profit=%s",
                            signal.value, opportunity.best_confirmed_profit,
                        )

                except Exception as e:
                    result.errors.append(
                        f"상세 처리 실패 ({goods_no}): {e}"
                    )
                    logger.error("카테고리 스캔 상품 처리 실패 (%s): %s", goods_no, e)

            # 진행 상황 DB 저장
            await self.db.update_category_progress(
                category, result.pages_scanned, len(listing),
            )

        # 결과 정렬
        result.opportunities.sort(
            key=lambda o: (-o.best_confirmed_profit, -o.best_estimated_profit)
        )
        result.finished_at = datetime.now()

        elapsed = (result.finished_at - result.started_at).total_seconds()
        logger.info(
            "=== 카테고리 스캔 완료 (%.0f초) | 리스팅 %d → "
            "품절 %d / 이미스캔 %d / "
            "이름매칭 %d / 상세 %d → DB매칭 %d → "
            "수익기회 %d (확정 %d / 예상 %d) | 에러 %d ===",
            elapsed,
            result.listing_fetched,
            result.sold_out_skipped,
            result.already_scanned,
            result.name_matched,
            result.detail_fetched,
            result.detail_matched,
            len(result.opportunities),
            result.confirmed_count,
            result.estimated_count,
            len(result.errors),
        )

        return result

    async def _collect_kream_popular(
        self, categories: list[str] | None = None,
    ) -> list[dict]:
        """크림 인기상품 여러 카테고리/소스에서 수집 후 병합/중복 제거.

        Args:
            categories: 수집할 카테고리 목록. None이면 기본 카테고리 사용.
                지원 카테고리: sneakers, clothing, bags, accessories
        """
        if categories is None:
            categories = ["sneakers", "clothing", "bags", "accessories"]

        all_products: list[dict] = []
        seen_ids: set[str] = set()

        def add_products(items: list[dict], category_tag: str = ""):
            for item in items:
                pid = item.get("product_id", "")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    if category_tag:
                        item["_category"] = category_tag
                    all_products.append(item)

        # 카테고리별 수집 비중 (전체 대비)
        category_weights = {
            "sneakers": 0.4,
            "clothing": 0.25,
            "bags": 0.15,
            "accessories": 0.2,  # 모자 포함
        }

        for cat in categories:
            weight = category_weights.get(cat, 0.2)
            popular_limit = max(int(50 * weight), 5)
            sales_limit = max(int(40 * weight), 5)

            # 인기순
            try:
                popular = await kream_crawler.get_popular_products(
                    category=cat, sort="popular", limit=popular_limit,
                )
                add_products(popular, cat)
                logger.info("크림 인기순(%s) %d건 수집", cat, len(popular))
            except Exception as e:
                logger.error("인기 상품 수집 실패 (%s): %s", cat, e)

            # 거래 많은 순
            try:
                sales = await kream_crawler.get_popular_products(
                    category=cat, sort="sales", limit=sales_limit,
                )
                add_products(sales, cat)
                logger.info("크림 거래량순(%s) %d건 수집", cat, len(sales))
            except Exception as e:
                logger.error("거래량순 상품 수집 실패 (%s): %s", cat, e)

        # 급상승 상품 (카테고리 무관)
        try:
            trending = await kream_crawler.get_trending_products(limit=30)
            add_products(trending, "trending")
        except Exception as e:
            logger.error("급상승 상품 수집 실패: %s", e)

        # 카테고리별 통계 로그
        cat_counts: dict[str, int] = {}
        for item in all_products:
            cat = item.get("_category", "unknown")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        cat_summary = ", ".join(f"{k}={v}" for k, v in sorted(cat_counts.items()))

        logger.info(
            "크림 인기상품 총 %d건 수집 (중복 제거 후) [%s]",
            len(all_products), cat_summary,
        )
        return all_products

    async def _get_kream_with_cache(self, product_id: str) -> KreamProduct | None:
        """크림 상품 상세 수집 (캐시 활용)."""
        cache_ttl = timedelta(minutes=settings.auto_scan_cache_minutes)

        # 캐시 확인
        for model, (ts, product) in list(self._auto_scan_cache.items()):
            if product.product_id == product_id and datetime.now() - ts < cache_ttl:
                logger.debug("캐시 히트: %s", product.name)
                return product

        # 크림에서 수집
        product = await kream_crawler.get_full_product_info(product_id)
        if product and product.model_number:
            normalized = normalize_model_number(product.model_number)
            self._auto_scan_cache[normalized] = (datetime.now(), product)

        return product

    async def _get_kream_from_db_or_api(
        self, product_id: str, row: dict,
    ) -> KreamProduct | None:
        """크림 상품 정보를 DB 가격 우선으로 구축, 없으면 API 호출.

        역방향 스캔용: DB에 가격 이력이 있으면 API 호출 없이 KreamProduct 구성.
        가격 이력이 없으면 그때만 크림 API 호출.
        """
        from src.models.product import KreamSizePrice

        # 1. DB에서 최신 가격 이력 조회
        db_prices = await self.db.get_latest_kream_prices(product_id)

        if db_prices:
            # DB 가격으로 KreamProduct 구성 (API 호출 없음)
            # sqlite3.Row는 .get() 불가 → dict 변환
            db_prices = [dict(p) for p in db_prices]
            size_prices = []
            for p in db_prices:
                size_prices.append(KreamSizePrice(
                    size=p["size"],
                    sell_now_price=p["sell_now_price"],
                    buy_now_price=p["buy_now_price"],
                    bid_count=p.get("bid_count", 0),
                    last_sale_price=p.get("last_sale_price"),
                ))

            product = KreamProduct(
                product_id=product_id,
                name=row["name"],
                model_number=row["model_number"],
                brand=row["brand"] or "",
                category=row.get("category", "sneakers") or "sneakers",
                image_url=row.get("image_url", "") or "",
                url=row.get("url", "") or "",
                size_prices=size_prices,
            )
            logger.debug("크림 DB 가격 사용: %s (%d사이즈)", product.name, len(size_prices))
            return product

        # 2. DB에 가격 없으면 크림 API 호출
        logger.info("크림 DB 가격 없음, API 호출: %s", row["name"])
        return await self._get_kream_with_cache(product_id)

    async def _search_musinsa_for_model(
        self, model_number: str, kream_name: str = "",
        kream_brand: str = "",
    ) -> tuple[dict[str, tuple[int, bool]], str, str, str] | None:
        """모델번호로 무신사에서 검색하여 사이즈별 가격 반환.

        3단계 검색 전략:
        1차: 모델번호 직접 검색 (복합 모델번호 분리 포함)
        2차: 크림 상품명(한글/영문)으로 검색
        3차: 브랜드 + 상품명 조합으로 검색

        매칭 규칙:
        - 모델번호 정확 일치만 인정 (부분 일치 절대 불허)
        - 검색 결과 여러 개면 가격이 가장 낮은 상품 선택
        - 매칭 애매한 경우 검토 채널에 기록

        Returns:
            (sizes_dict, url, name, product_id) or None
            sizes_dict: {normalized_size: (price, in_stock)}
        """
        from src.profit_calculator import _normalize_size
        import re

        # ── 1차: 모델번호 검색 (복합 모델번호 분리 포함) ──
        search_queries_phase1 = []

        # 슬래시 포함 복합 모델번호 분리 (315122-111/CW2288-111 → 각각 검색)
        if "/" in model_number:
            parts = [p.strip() for p in model_number.split("/") if p.strip()]
            search_queries_phase1.extend(parts)

        # 원본 모델번호
        if model_number not in search_queries_phase1:
            search_queries_phase1.append(model_number)

        # 하이픈→공백 변형
        for q in list(search_queries_phase1):
            alt = re.sub(r"[-]", " ", q)
            if alt != q and alt not in search_queries_phase1:
                search_queries_phase1.append(alt)

        search_results = []
        used_query = ""
        for query in search_queries_phase1:
            search_results = await musinsa_crawler.search_products(query)
            if search_results:
                used_query = query
                logger.debug("무신사 1차 검색 성공: '%s' → %d건", query, len(search_results))
                break

        # ── 2차: 상품명으로 검색 (모델번호 검색 실패 시) ──
        if not search_results and kream_name:
            name_query = re.sub(r"['\"]", "", kream_name)
            # 영문 상품명에서 주요 단어만 (최대 4단어)
            words = name_query.split()[:4]
            if words:
                search_results = await musinsa_crawler.search_products(" ".join(words))
                if search_results:
                    used_query = " ".join(words)
                    logger.debug("무신사 2차 검색(상품명) 성공: '%s' → %d건", used_query, len(search_results))

        # ── 3차: 브랜드 + 상품명 조합 검색 (브랜드 중복 방지) ──
        if not search_results and kream_brand and kream_name:
            name_words = kream_name.split()
            # 상품명이 브랜드명으로 시작하면 브랜드 중복 방지
            if name_words and name_words[0].lower() == kream_brand.lower():
                # 이미 브랜드 포함된 상품명에서 앞 3단어 사용
                brand_name_query = " ".join(name_words[:3])
            else:
                brand_name_query = f"{kream_brand} {name_words[0] if name_words else ''}"
            brand_name_query = brand_name_query.strip()
            if brand_name_query:
                search_results = await musinsa_crawler.search_products(brand_name_query)
                if search_results:
                    used_query = brand_name_query
                    logger.debug("무신사 3차 검색(브랜드+상품명) 성공: '%s' → %d건", used_query, len(search_results))

        if not search_results:
            return None

        # ── 검색 결과에서 모델번호 정확 매칭 찾기 (최대 8건 상세 조회) ──
        matched_products: list[tuple[dict[str, tuple[int, bool]], str, str, str, int]] = []
        near_miss_items: list[dict] = []  # 매칭 애매한 건 (검토 채널용)

        for item in search_results[:8]:
            try:
                detail = await musinsa_crawler.get_product_detail(item["product_id"])
                if not detail:
                    continue

                if not detail.model_number:
                    continue

                # 모델번호 정확 매칭 확인 (복합 모델번호 지원)
                if _match_model_with_slash(detail.model_number, model_number):
                    # 사이즈별 가격 맵 구축
                    sizes: dict[str, tuple[int, bool]] = {}
                    for s in detail.sizes:
                        if s.in_stock and s.price > 0:
                            norm_size = _normalize_size(s.size)
                            if norm_size not in sizes or s.price < sizes[norm_size][0]:
                                sizes[norm_size] = (s.price, s.in_stock)

                    if sizes:
                        # 최저가 계산 (가격 비교용)
                        min_price = min(p for p, _ in sizes.values())
                        matched_products.append(
                            (sizes, detail.url, detail.name, detail.product_id, min_price)
                        )
                    else:
                        logger.info(
                            "무신사 모델매칭 성공이나 사이즈 0개 (전체품절): %s (%s) 전체사이즈=%d개",
                            detail.name, model_number, len(detail.sizes),
                        )
                else:
                    # 모델번호 불일치 — 상품명이 유사한데 모델번호가 다른 경우 기록
                    near_miss_items.append({
                        "musinsa_name": detail.name,
                        "musinsa_model": detail.model_number,
                        "musinsa_url": detail.url,
                        "musinsa_price": item.get("price", 0),
                    })

            except Exception as e:
                logger.error(
                    "무신사 상세 조회 실패 (%s): %s", item.get("product_id"), e
                )

        # ── 매칭 애매한 건 검토 채널에 기록 ──
        if near_miss_items and not matched_products:
            await self._log_match_review(
                kream_name=kream_name,
                kream_model=model_number,
                kream_brand=kream_brand,
                near_misses=near_miss_items,
                search_query=used_query,
            )

        if not matched_products:
            return None

        # ── 여러 매칭 중 가격이 가장 낮은 상품 선택 ──
        matched_products.sort(key=lambda x: x[4])  # min_price 기준 정렬
        best = matched_products[0]

        if len(matched_products) > 1:
            logger.info(
                "무신사 매칭 %d건 중 최저가 선택: %s (%s원)",
                len(matched_products), best[2], f"{best[4]:,}",
            )

        return (best[0], best[1], best[2], best[3])

    async def _search_29cm_for_model(
        self, model_number: str, kream_name: str = "",
    ) -> tuple[dict[str, tuple[int, bool]], str, str, str] | None:
        """모델번호로 29CM에서 검색하여 사이즈별 가격 반환.

        3단계 검색 전략:
        1차: 모델번호 직접 검색 (복합 모델번호 분리 포함)
        2차: 크림 상품명(한글/영문)으로 검색

        Returns:
            (sizes_dict, url, name, product_id) or None
        """
        from src.profit_calculator import _normalize_size
        import re as _re

        # ── 1차: 모델번호 검색 ──
        search_queries = [model_number]
        if "/" in model_number:
            parts = [p.strip() for p in model_number.split("/") if p.strip()]
            search_queries = parts + [model_number]

        search_results = []
        for query in search_queries:
            search_results = await twentynine_cm_crawler.search_products(query, limit=10)
            if search_results:
                break

        # ── 2차: 상품명으로 검색 (모델번호 검색 실패 시) ──
        if not search_results and kream_name:
            name_query = _re.sub(r"['\"]", "", kream_name)
            words = name_query.split()[:4]
            if words:
                search_results = await twentynine_cm_crawler.search_products(
                    " ".join(words), limit=10,
                )
                if search_results:
                    logger.debug(
                        "29CM 2차 검색(상품명) 성공: '%s' → %d건",
                        " ".join(words), len(search_results),
                    )

        if not search_results:
            return None

        # 모델번호 매칭 (검색 결과의 model_number 필드 또는 상세 페이지)
        for item in search_results[:5]:
            item_model = item.get("model_number", "")

            # 검색 결과에 모델번호가 있으면 직접 매칭
            if item_model and _match_model_with_slash(item_model, model_number):
                result = await self._fetch_29cm_sizes(item["product_id"], model_number)
                if result:
                    return result
                continue

            # 모델번호가 없으면 상세 페이지에서 모델번호 확인
            if not item_model:
                detail = await twentynine_cm_crawler.get_product_detail(item["product_id"])
                if not detail or not detail.sizes:
                    continue
                if detail.model_number and _match_model_with_slash(
                    detail.model_number, model_number,
                ):
                    sizes: dict[str, tuple[int, bool]] = {}
                    for s in detail.sizes:
                        if s.in_stock and s.price > 0:
                            norm_size = _normalize_size(s.size)
                            if norm_size not in sizes or s.price < sizes[norm_size][0]:
                                sizes[norm_size] = (s.price, s.in_stock)
                    if sizes:
                        return (sizes, detail.url, detail.name, detail.product_id)

        return None

    async def _fetch_29cm_sizes(
        self, product_id: str, model_number: str,
    ) -> tuple[dict[str, tuple[int, bool]], str, str, str] | None:
        """29CM 상세 페이지에서 사이즈별 가격 수집."""
        from src.profit_calculator import _normalize_size

        detail = await twentynine_cm_crawler.get_product_detail(product_id)
        if not detail or not detail.sizes:
            return None

        sizes: dict[str, tuple[int, bool]] = {}
        for s in detail.sizes:
            if s.in_stock and s.price > 0:
                norm_size = _normalize_size(s.size)
                if norm_size not in sizes or s.price < sizes[norm_size][0]:
                    sizes[norm_size] = (s.price, s.in_stock)

        if sizes:
            return (sizes, detail.url, detail.name, detail.product_id)
        return None

    async def _search_retail_for_model(
        self, model_number: str, kream_name: str = "",
        kream_brand: str = "",
    ) -> tuple[dict[str, tuple[int, bool]], str, str, str, dict[str, int]] | None:
        """무신사 + 29CM 병렬 검색 후 사이즈별 최저가 병합.

        Returns:
            (merged_sizes, best_url, best_name, best_pid, source_prices) or None
            source_prices: {"무신사": 최저가, "29CM": 최저가}
        """
        # 무신사 + 29CM 병렬 검색 (29CM 실패해도 무신사 결과 사용)
        musinsa_task = self._search_musinsa_for_model(
            model_number, kream_name, kream_brand=kream_brand,
        )
        twentynine_task = self._search_29cm_for_model(
            model_number, kream_name,
        )

        results = await asyncio.gather(
            musinsa_task, twentynine_task, return_exceptions=True,
        )

        musinsa_result = results[0] if not isinstance(results[0], Exception) else None
        twentynine_result = results[1] if not isinstance(results[1], Exception) else None

        if isinstance(results[0], Exception):
            logger.warning("무신사 검색 예외: %s", results[0])
        if isinstance(results[1], Exception):
            logger.warning("29CM 검색 예외: %s", results[1])

        if not musinsa_result and not twentynine_result:
            return None

        # 소싱처별 최저가 기록
        source_prices: dict[str, int] = {}

        # 소싱처별 결과 수집: [(sizes, url, name, pid, source_label)]
        source_items: list[tuple[dict, str, str, str, str]] = []

        if musinsa_result:
            m_sizes, m_url, m_name, m_pid = musinsa_result
            source_items.append((m_sizes, m_url, m_name, m_pid, "무신사"))
            if m_sizes:
                source_prices["무신사"] = min(p for p, _ in m_sizes.values())

        if twentynine_result:
            t_sizes, t_url, t_name, t_pid = twentynine_result
            source_items.append((t_sizes, t_url, t_name, t_pid, "29CM"))
            if t_sizes:
                source_prices["29CM"] = min(p for p, _ in t_sizes.values())

        # 사이즈별 최저가 병합 + 소싱처 추적
        merged_sizes: dict[str, tuple[int, bool]] = {}
        # 어떤 소싱처가 최저가인지 (best_url/name/pid 결정용)
        best_source_label = ""
        best_min_price = float("inf")

        for sizes, url, name, pid, label in source_items:
            cur_min = min((p for p, _ in sizes.values()), default=float("inf"))
            if cur_min < best_min_price:
                best_min_price = cur_min
                best_url = url
                best_name = name
                best_pid = pid
                best_source_label = label

            for size_key, (price, in_stock) in sizes.items():
                if size_key not in merged_sizes or price < merged_sizes[size_key][0]:
                    merged_sizes[size_key] = (price, in_stock)

        if not merged_sizes:
            return None

        if len(source_prices) > 1:
            prices_str = " / ".join(f"{k} {v:,}원" for k, v in source_prices.items())
            logger.info(
                "리테일 가격 비교: %s → 최저가 %s (%s)",
                model_number, best_source_label, prices_str,
            )

        return (merged_sizes, best_url, best_name, best_pid, source_prices)

    async def _log_match_review(
        self,
        kream_name: str,
        kream_model: str,
        kream_brand: str,
        near_misses: list[dict],
        search_query: str,
    ) -> None:
        """매칭 애매한 건을 #수정 디스코드 채널에 기록."""
        if not settings.channel_match_review or not self._match_review_callback:
            return

        lines = [
            f"**크림 상품:** {kream_name}",
            f"**크림 모델번호:** `{kream_model}`",
            f"**브랜드:** {kream_brand}",
            f"**검색어:** `{search_query}`",
            "",
            "**무신사 검색 결과 (모델번호 불일치로 매칭 거부):**",
        ]
        for i, item in enumerate(near_misses[:3], 1):
            price_str = f"{item['musinsa_price']:,}원" if item.get("musinsa_price") else "가격 미확인"
            lines.append(
                f"{i}. {item['musinsa_name']}\n"
                f"   모델번호: `{item['musinsa_model']}` ≠ `{kream_model}`\n"
                f"   가격: {price_str} | [링크]({item['musinsa_url']})"
            )
        lines.append("")
        lines.append("**사유:** 상품명은 유사하지만 모델번호가 정확히 일치하지 않아 매칭하지 않음")

        try:
            await self._match_review_callback("\n".join(lines))
        except Exception as e:
            logger.error("매칭 검토 기록 실패: %s", e)
