"""역방향 스캐너 — 크림 hot 상품 기준 소싱처 가격 조회.

기존 방식: 무신사 전체 긁기 → 크림 DB 대조
새 방식: 크림 DB hot 상품 → 소싱처 5곳 병렬 검색 → 수익 분석

매칭률 100% (이미 크림 상품), 소싱처 5곳 동시 검색.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from src.config import settings
from src.crawlers.registry import get_active, record_failure, record_success
from src.matcher import model_numbers_match, normalize_model_number
from src.models.database import Database
from src.models.product import (
    AutoScanOpportunity,
    AutoScanSizeProfit,
    KreamProduct,
    KreamSizePrice,
    Signal,
)
from src.profit_calculator import calculate_kream_fees, determine_signal
from src.scan_cache import ScanCache
from src.utils.logging import setup_logger
from src.watchlist import Watchlist, WatchlistItem

logger = setup_logger("reverse_scanner")


@dataclass
class ReverseLookupResult:
    """역방향 스캔 결과."""

    hot_count: int = 0
    searched: int = 0
    sourced: int = 0  # 소싱처에서 발견
    no_prices: int = 0  # 크림 시세 미수집 (size_prices 빈 리스트)
    profitable: int = 0
    opportunities: list[AutoScanOpportunity] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None


class ReverseLookupScanner:
    """크림 hot 상품 → 소싱처 5곳 가격 조회."""

    def __init__(
        self,
        db: Database,
        watchlist: Watchlist | None = None,
        scan_cache: ScanCache | None = None,
    ):
        self.db = db
        self.watchlist = watchlist
        self.scan_cache = scan_cache

    async def run(
        self,
        limit: int = 50,
        on_opportunity=None,
        on_progress=None,
    ) -> ReverseLookupResult:
        """역방향 스캔 실행.

        Args:
            limit: hot 상품 조회 수
            on_opportunity: 수익 기회 발견 시 콜백 (async)
            on_progress: 진행 상황 콜백 (async)
        """
        result = ReverseLookupResult()

        # 1. hot 상품 조회
        hot_products = await self.db.get_hot_products(limit)
        result.hot_count = len(hot_products)

        if not hot_products:
            logger.warning("hot 상품 없음 — collect_loop 선행 필요")
            return result

        logger.info("역방향 스캔 시작: hot %d건", result.hot_count)
        if on_progress:
            await on_progress(
                f"🔍 역방향 스캔 시작: hot {result.hot_count}건 처리 예정"
            )

        # 2. 순차 병렬 처리 (Semaphore로 동시 제한, 개별 완료 시 즉시 콜백)
        sem = asyncio.Semaphore(settings.httpx_concurrency)
        completed = 0  # 완료 카운터 (콜백용)
        scan_start = datetime.now()

        async def process_one(product: dict) -> AutoScanOpportunity | None:
            nonlocal completed
            res = None
            try:
                async with sem:
                    res = await self._process_hot_product(product, result)
            except Exception as exc:
                model = product.get("model_number", "?")
                result.errors.append(f"{model}: {exc}")
                logger.debug("역방향 처리 실패: %s — %s", model, exc)

            completed += 1

            # 결과 처리 + 콜백 (개별 완료 즉시)
            if res is not None:
                result.opportunities.append(res)
                result.profitable += 1
                # 수익 기회 상세 로그
                kp = res.kream_product
                top_sp = res.size_profits[0] if res.size_profits else None
                if top_sp:
                    logger.info(
                        "수익 발견: %s [%s] | %s %s원 → 크림 %s원 | "
                        "실수익 %s원 (ROI %.1f%%) | 시그널: %s | 매칭사이즈: %s",
                        kp.name[:30], kp.model_number,
                        top_sp.source, f"{top_sp.musinsa_price:,}",
                        f"{top_sp.kream_bid_price:,}" if top_sp.kream_bid_price else "?",
                        f"{top_sp.confirmed_profit:,}", top_sp.confirmed_roi,
                        res.signal.value,
                        ", ".join(res.matched_sizes[:5]) if res.matched_sizes else "?",
                    )
                if on_opportunity:
                    try:
                        await on_opportunity(res)
                    except Exception as e:
                        logger.debug("콜백 실패: %s", e)

            # 진행 보고 (10건마다 + ETA)
            if on_progress and completed % 10 == 0:
                elapsed = (datetime.now() - scan_start).total_seconds()
                remaining = result.hot_count - completed
                eta_sec = (elapsed / completed * remaining) if completed > 0 else 0
                eta_min = eta_sec / 60
                await on_progress(
                    f"⏳ 진행 중: {completed}/{result.hot_count}건 완료 "
                    f"| 소싱 {result.sourced} | 수익 {result.profitable} "
                    f"| 예상 완료: {eta_min:.1f}분 후"
                )

            return res

        await asyncio.gather(
            *[process_one(p) for p in hot_products],
            return_exceptions=True,
        )

        # 정렬: 확정 수익 내림차순
        result.opportunities.sort(key=lambda o: -o.best_confirmed_profit)
        result.finished_at = datetime.now()

        elapsed_sec = (
            (result.finished_at - result.started_at).total_seconds()
            if result.finished_at and result.started_at else 0
        )

        logger.info(
            "역방향 스캔 완료: hot %d → 검색 %d → 소싱 %d → 시세없음 %d → 수익 %d"
            " (에러 %d, %.0f초)",
            result.hot_count, result.searched, result.sourced,
            result.no_prices, result.profitable, len(result.errors), elapsed_sec,
        )

        if on_progress:
            elapsed_min = elapsed_sec / 60
            await on_progress(
                f"✅ 역방향 완료: {result.hot_count}건\n"
                f"- 소싱 매칭: {result.sourced}건\n"
                f"- 시세 미수집: {result.no_prices}건\n"
                f"- 수익 기회: {result.profitable}건\n"
                f"- 소요시간: {elapsed_min:.1f}분"
            )

        return result

    async def _process_hot_product(
        self, product: dict, result: ReverseLookupResult,
    ) -> AutoScanOpportunity | None:
        """hot 상품 하나 처리: 소싱처 5곳 검색 → 수익 분석."""
        model_number = product["model_number"]
        normalized = normalize_model_number(model_number)

        result.searched += 1

        # 캐시 체크 (searched 카운트 후 스킵 판정)
        if self.scan_cache and self.scan_cache.should_skip(normalized):
            return None

        # 크림 시세 먼저 확인 (없으면 소싱처 검색 불필요)
        kream_product = self._build_kream_product(product)

        if not kream_product.size_prices:
            result.no_prices += 1
            logger.debug(
                "시세 미수집 탈락: %s [%s] — kream_price_history 없음",
                kream_product.name[:30], normalized,
            )
            return None

        # 소싱처 5곳 병렬 검색
        source_results = await self._search_all_sources(normalized)

        if not source_results:
            # 캐시 기록 (소싱처 미발견)
            if self.scan_cache:
                self.scan_cache.record(normalized, profitable=False, source="reverse")
            return None

        result.sourced += 1

        # 소싱처별 최저 사이즈 가격 맵 구축
        best_sizes, best_source_prices, best_source_urls = self._build_best_size_map(
            source_results,
        )

        if not best_sizes:
            return None

        # 수익 분석
        opportunity = self._analyze_profit(
            kream_product, best_sizes, best_source_prices, best_source_urls,
        )

        # 캐시 기록
        if self.scan_cache:
            profitable = opportunity is not None and opportunity.best_confirmed_profit > 0
            self.scan_cache.record(normalized, profitable=profitable, source="reverse")

        # 워치리스트 추가
        if opportunity and self.watchlist:
            self._add_to_watchlist(product, opportunity, best_source_prices)

        return opportunity

    async def _search_all_sources(
        self, model_number: str,
    ) -> dict[str, list[dict]]:
        """활성 소싱처 5곳에서 모델번호 검색."""
        active = get_active()
        if not active:
            return {}

        search_tasks = {}
        for key, info in active.items():
            try:
                search_tasks[key] = info["crawler"].search_products(
                    model_number, limit=5,
                )
            except TypeError:
                search_tasks[key] = info["crawler"].search_products(model_number)

        if not search_tasks:
            return {}

        raw_results = await asyncio.gather(
            *search_tasks.values(), return_exceptions=True,
        )

        source_results: dict[str, list[dict]] = {}
        for key, raw in zip(search_tasks.keys(), raw_results):
            if isinstance(raw, Exception):
                record_failure(key)
                logger.debug("소싱처 %s 검색 실패: %s", key, raw)
                continue

            if not raw:
                continue

            record_success(key)

            # 모델번호 재검증: 검색 결과 중 정확 매칭만 필터
            verified = []
            for item in raw:
                item_model = item.get("model_number", "")
                if item_model and model_numbers_match(item_model, model_number):
                    verified.append(item)

            if verified:
                source_results[key] = verified

        # 사이즈 미반환 소싱처 → 상세 페이지에서 사이즈 보강
        await self._enrich_missing_sizes(source_results, active)

        return source_results

    async def _enrich_missing_sizes(
        self,
        source_results: dict[str, list[dict]],
        active: dict[str, dict],
    ) -> None:
        """검색 결과에 sizes가 없는 상품의 상세 페이지에서 사이즈 보강."""
        for key, items in source_results.items():
            crawler = active[key]["crawler"]
            if not hasattr(crawler, "get_product_detail"):
                continue

            for item in items:
                if item.get("sizes"):
                    continue  # 이미 사이즈 있음

                pid = item.get("product_id", "")
                if not pid:
                    continue

                try:
                    detail = await crawler.get_product_detail(pid)
                    if detail and detail.sizes:
                        item["sizes"] = [
                            {
                                "size": s.size,
                                "price": s.price,
                                "in_stock": s.in_stock,
                            }
                            for s in detail.sizes
                        ]
                        logger.debug(
                            "%s 상세 사이즈 보강: %s → %d개",
                            active[key]["label"], pid, len(detail.sizes),
                        )
                except Exception as e:
                    logger.debug(
                        "%s 상세 조회 실패: %s — %s",
                        active[key]["label"], pid, e,
                    )

    def _build_kream_product(self, product: dict) -> KreamProduct:
        """DB 조회 결과를 KreamProduct로 변환."""
        size_prices = []
        for sp in product.get("size_prices", []):
            size_prices.append(KreamSizePrice(
                size=sp.get("size", ""),
                sell_now_price=sp.get("sell_now_price"),
                buy_now_price=sp.get("buy_now_price"),
                bid_count=sp.get("bid_count", 0),
                last_sale_price=sp.get("last_sale_price"),
            ))

        return KreamProduct(
            product_id=product["product_id"],
            name=product["name"],
            model_number=product["model_number"],
            brand=product.get("brand", ""),
            category=product.get("category", "sneakers"),
            image_url=product.get("image_url", ""),
            url=product.get("url", ""),
            size_prices=size_prices,
            volume_7d=product.get("volume_7d", 0),
            volume_30d=product.get("volume_30d", 0),
        )

    def _build_best_size_map(
        self, source_results: dict[str, list[dict]],
    ) -> tuple[dict[str, tuple[int, str, str]], dict[str, int], dict[str, str]]:
        """소싱처 결과에서 사이즈별 최저가 맵 구축.

        Returns:
            best_sizes: {normalized_size: (price, source_key, source_url)}
            best_source_prices: {source_label: min_price}
            best_source_urls: {source_label: url}
        """
        best_sizes: dict[str, tuple[int, str, str]] = {}
        best_source_prices: dict[str, int] = {}
        best_source_urls: dict[str, str] = {}

        for source_key, items in source_results.items():
            for item in items:
                item_price = item.get("price", 0)
                item_url = item.get("url", "")
                sizes = item.get("sizes", [])

                if sizes:
                    # 사이즈별 가격이 있는 경우
                    for s in sizes:
                        size_str = str(s.get("size", ""))
                        s_price = s.get("price", item_price)
                        in_stock = s.get("in_stock", True)
                        if not in_stock or s_price <= 0:
                            continue
                        norm_size = self._normalize_size(size_str)
                        current = best_sizes.get(norm_size)
                        if current is None or s_price < current[0]:
                            best_sizes[norm_size] = (s_price, source_key, item_url)
                elif item_price > 0:
                    # 사이즈 정보 없이 단일 가격만 있는 경우
                    current = best_sizes.get("ONE_SIZE")
                    if current is None or item_price < current[0]:
                        best_sizes["ONE_SIZE"] = (item_price, source_key, item_url)

                # 소싱처별 최저가 + URL
                if item_price > 0:
                    from src.crawlers.registry import get_label
                    label = get_label(source_key)
                    cur = best_source_prices.get(label)
                    if cur is None or item_price < cur:
                        best_source_prices[label] = item_price
                        if item_url:
                            best_source_urls[label] = item_url

        return best_sizes, best_source_prices, best_source_urls

    def _normalize_size(self, size: str) -> str:
        """사이즈 정규화 (profit_calculator와 동일 로직)."""
        size = size.strip().upper().replace("MM", "").replace("CM", "").strip()
        try:
            num = float(size)
            if num <= 35:
                return str(int(num * 10))
            return str(int(num))
        except ValueError:
            return size

    def _analyze_profit(
        self,
        kream_product: KreamProduct,
        best_sizes: dict[str, tuple[int, str, str]],
        best_source_prices: dict[str, int],
        best_source_urls: dict[str, str] | None = None,
    ) -> AutoScanOpportunity | None:
        """사이즈별 수익 분석 (sell_now > 0 교차 매칭 필터 적용)."""
        size_profits: list[AutoScanSizeProfit] = []
        matched_sizes: list[str] = []  # 교차 매칭된 사이즈 목록

        # 최근 체결가 맵
        recent_prices: dict[str, int] = {}
        for sp in kream_product.size_prices:
            if sp.last_sale_price:
                recent_prices[self._normalize_size(sp.size)] = sp.last_sale_price

        for ksp in kream_product.size_prices:
            kream_size = self._normalize_size(ksp.size)

            # ★ 사이즈 교차 매칭 필터: sell_now_price > 0인 사이즈만 수익 계산
            if not ksp.sell_now_price or ksp.sell_now_price <= 0:
                continue

            # best_sizes에서 매칭
            source_info = best_sizes.get(kream_size)
            if not source_info and "ONE_SIZE" in best_sizes:
                source_info = best_sizes["ONE_SIZE"]
            if not source_info:
                continue

            source_price, source_key, source_url = source_info
            if source_price <= 0:
                continue

            matched_sizes.append(ksp.size)

            from src.crawlers.registry import get_label
            source_label = get_label(source_key)

            sp = AutoScanSizeProfit(
                size=ksp.size,
                musinsa_price=source_price,
                source=source_label,
                source_url=source_url,
                kream_bid_price=ksp.sell_now_price,
                kream_recent_price=recent_prices.get(kream_size) or ksp.last_sale_price,
                bid_count=ksp.bid_count,
                in_stock=True,
            )

            # 확정 수익 (즉시판매가 기반)
            fees = calculate_kream_fees(ksp.sell_now_price)
            total_cost = source_price + fees["total_fees"]
            sp.confirmed_profit = ksp.sell_now_price - total_cost
            sp.confirmed_roi = round(
                (sp.confirmed_profit / source_price * 100) if source_price > 0 else 0, 1
            )

            # 예상 수익 (최근 체결가 기반)
            recent_price = sp.kream_recent_price
            if recent_price and recent_price > 0:
                fees = calculate_kream_fees(recent_price)
                total_cost = source_price + fees["total_fees"]
                sp.estimated_profit = recent_price - total_cost
                sp.estimated_roi = round(
                    (sp.estimated_profit / source_price * 100) if source_price > 0 else 0, 1
                )

            size_profits.append(sp)

        if not size_profits:
            return None

        # 시그널 판정
        best_confirmed = max(size_profits, key=lambda x: x.confirmed_profit)
        best_estimated = max(size_profits, key=lambda x: x.estimated_profit)
        signal = determine_signal(best_confirmed.confirmed_profit, kream_product.volume_7d)

        if signal == Signal.NOT_RECOMMENDED:
            return None

        # 소싱처 URL 수집 (best_source_urls 우선, size_profits fallback)
        source_urls: dict[str, str] = dict(best_source_urls or {})
        for sp in size_profits:
            if sp.source_url and sp.source not in source_urls:
                source_urls[sp.source] = sp.source_url

        # 최고 수익 사이즈의 소싱처 URL
        best_source_info = best_sizes.get(
            self._normalize_size(best_confirmed.size), ("", "", "")
        )

        return AutoScanOpportunity(
            kream_product=kream_product,
            musinsa_url=best_source_info[2] if len(best_source_info) > 2 else "",
            musinsa_name=kream_product.name,
            musinsa_product_id="",
            size_profits=sorted(size_profits, key=lambda x: -x.confirmed_profit),
            best_confirmed_profit=best_confirmed.confirmed_profit,
            best_confirmed_roi=best_confirmed.confirmed_roi,
            best_estimated_profit=best_estimated.estimated_profit,
            best_estimated_roi=best_estimated.estimated_roi,
            volume_7d=kream_product.volume_7d,
            signal=signal,
            source_prices=best_source_prices,
            source_urls=source_urls,
            matched_sizes=matched_sizes,
        )

    def _add_to_watchlist(
        self,
        product: dict,
        opportunity: AutoScanOpportunity,
        best_source_prices: dict[str, int],
    ) -> None:
        """수익 기회를 워치리스트에 추가."""
        if not self.watchlist:
            return

        # 최저가 소싱처 결정
        best_source = "unknown"
        best_price = 0
        best_url = opportunity.musinsa_url

        if opportunity.size_profits:
            top = opportunity.size_profits[0]
            best_price = top.musinsa_price
            best_source = top.source

        item = WatchlistItem(
            kream_product_id=product["product_id"],
            model_number=product["model_number"],
            kream_name=product["name"],
            musinsa_product_id="",
            musinsa_price=best_price,
            kream_price=opportunity.kream_product.size_prices[0].sell_now_price or 0
            if opportunity.kream_product.size_prices else 0,
            gap=best_price - (
                opportunity.kream_product.size_prices[0].sell_now_price or 0
                if opportunity.kream_product.size_prices else 0
            ),
            source=best_source,
            source_price=best_price,
            source_url=best_url,
        )
        self.watchlist.add(item)
