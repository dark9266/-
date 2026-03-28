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
from src.matcher import find_kream_match, model_numbers_match, normalize_model_number
from src.models.database import Database
from src.models.product import (
    AutoScanOpportunity,
    KreamProduct,
    ProfitOpportunity,
    RetailProduct,
    Signal,
)
from src.price_tracker import PriceChange, PriceTracker
from src.profit_calculator import analyze_auto_scan_opportunity, analyze_opportunity
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


class Scanner:
    """스캔 오케스트레이터."""

    def __init__(self, db: Database):
        self.db = db
        self.price_tracker = PriceTracker(db)
        # 자동스캔 캐시: {model_number: (timestamp, KreamProduct)}
        self._auto_scan_cache: dict[str, tuple[datetime, KreamProduct]] = {}
        # 매칭 검토 채널 콜백 (봇에서 설정)
        self._match_review_callback = None

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

    # ─── 자동스캔 (크림 기준 역방향 파이프라인) ───────────────

    async def auto_scan(
        self,
        on_opportunity=None,
        on_progress=None,
    ) -> AutoScanResult:
        """크림 인기상품 기준 전체 자동 스캔.

        파이프라인:
        1. 크림 인기상품 수집 (인기순 + 거래량순 + 급상승)
        2. 각 상품의 모델번호로 무신사 검색
        3. 2단계 수익 분석 (확정 + 예상)
        4. 수익 기회 발견 즉시 콜백 (실시간 알림)

        Args:
            on_opportunity: 수익 기회 발견 시 호출할 콜백 (async callable)
            on_progress: 진행 상황 업데이트 콜백 (async callable)
        """
        result = AutoScanResult()
        logger.info("=== 자동스캔 시작 ===")

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

                        # 수익 기회 발견 즉시 알림 (전체 완료 안 기다림)
                        confirmed_threshold = settings.auto_scan_confirmed_roi
                        estimated_threshold = settings.auto_scan_estimated_roi

                        if (
                            opportunity.best_confirmed_roi >= confirmed_threshold
                            or opportunity.best_estimated_roi >= estimated_threshold
                        ):
                            if on_opportunity:
                                await on_opportunity(opportunity)

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

        # ── 검색 결과에서 모델번호 정확 매칭 찾기 (최대 5건 상세 조회) ──
        matched_products: list[tuple[dict[str, tuple[int, bool]], str, str, str, int]] = []
        near_miss_items: list[dict] = []  # 매칭 애매한 건 (검토 채널용)

        for item in search_results[:5]:
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
