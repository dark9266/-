"""Tier 1 워치리스트 빌더.

2단계 구성:
1. 역방향 스캔: 크림 hot 상품 → 소싱처 5곳 가격 조회 (주력)
2. 카테고리 스캔: 무신사 리스팅 → 크림 매칭 → gap 스크리닝 (보조)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from src.config import settings
from src.crawlers.kream import kream_crawler
from src.crawlers.musinsa_httpx import musinsa_crawler
from src.crawlers.registry import get_active, record_failure, record_success
from src.matcher import extract_model_from_name, normalize_model_number
from src.models.database import Database
from src.reverse_scanner import ReverseLookupScanner
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter
from src.watchlist import Watchlist, WatchlistItem

logger = setup_logger("tier1")


@dataclass
class Tier1Result:
    """Tier1 스캔 결과."""

    scanned: int = 0
    matched: int = 0
    added: int = 0
    # 역방향 스캔 결과
    reverse_hot: int = 0
    reverse_sourced: int = 0
    reverse_profitable: int = 0
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None


class Tier1Scanner:
    """워치리스트 빌더 — 역방향 스캔(주력) + 카테고리 gap 스크리닝(보조)."""

    def __init__(
        self,
        db: Database,
        watchlist: Watchlist,
        scan_cache=None,
    ):
        self.db = db
        self.watchlist = watchlist
        self.rate_limiter = AsyncRateLimiter(
            max_concurrent=settings.httpx_concurrency,
            min_interval=settings.musinsa_min_interval,
        )
        self.reverse_scanner = ReverseLookupScanner(
            db=db, watchlist=watchlist, scan_cache=scan_cache,
        )

    async def run(self, categories: list[str] | None = None) -> Tier1Result:
        """워치리스트 빌더 실행.

        1단계 (주력): 역방향 스캔 — 크림 hot 상품 → 소싱처 5곳 가격 조회
        2단계 (보조): 카테고리 스캔 — 무신사 리스팅 → gap 스크리닝
        """
        result = Tier1Result()

        # ── 1단계: 역방향 스캔 (주력) ──
        try:
            reverse_result = await self.reverse_scanner.run(limit=50)
            result.reverse_hot = reverse_result.hot_count
            result.reverse_sourced = reverse_result.sourced
            result.reverse_profitable = reverse_result.profitable
            logger.info(
                "역방향 스캔: hot %d → 소싱 %d → 수익 %d",
                reverse_result.hot_count, reverse_result.sourced,
                reverse_result.profitable,
            )
        except Exception as e:
            logger.error("역방향 스캔 실패: %s", e)

        # ── 2단계: 카테고리 gap 스크리닝 (보조) ──
        if categories is None:
            categories = ["103"]

        all_listings = []
        for cat in categories:
            try:
                listings = await musinsa_crawler.fetch_category_listing(
                    category=cat, max_pages=1,
                )
                all_listings.extend(listings)
            except Exception as e:
                logger.error("카테고리 %s 리스팅 실패: %s", cat, e)

        result.scanned = len(all_listings)
        logger.info("Tier1 카테고리: %d건 리스팅 수집", result.scanned)

        sem = asyncio.Semaphore(settings.httpx_concurrency)

        async def process_listing(listing: dict) -> None:
            async with sem:
                await self._process_one(listing, result)

        await asyncio.gather(
            *[process_listing(lst) for lst in all_listings],
            return_exceptions=True,
        )

        result.finished_at = datetime.now()
        logger.info(
            "Tier1 완료: 역방향 hot=%d/소싱=%d/수익=%d | "
            "카테고리 스캔=%d/매칭=%d/추가=%d",
            result.reverse_hot, result.reverse_sourced, result.reverse_profitable,
            result.scanned, result.matched, result.added,
        )
        return result

    async def _process_one(self, listing: dict, result: Tier1Result) -> None:
        """개별 리스팅 처리."""
        try:
            name = listing.get("name", "") or listing.get("goodsNm", "")
            brand = listing.get("brand", "") or listing.get("brandNm", "")
            price = listing.get("price", 0) or listing.get("normalPrice", 0)
            product_id = str(listing.get("goodsNo", "") or listing.get("id", ""))

            if not name or not price:
                return

            # 모델번호 추출
            model = extract_model_from_name(name)
            if not model:
                return

            model_norm = normalize_model_number(model)
            if not model_norm:
                return

            # 크림 DB 매칭
            kream_rows = await self.db.find_kream_all_by_model(model_norm)
            if not kream_rows:
                return

            result.matched += 1

            # 첫 매칭 상품의 시세로 gap 계산
            kream = kream_rows[0]
            kream_price = kream.get("buy_now_price") or kream.get("estimated_price") or 0
            if not kream_price:
                return

            gap = price - kream_price

            # gap 기준 확인
            if gap > settings.watchlist_gap_threshold:
                # 다른 소싱처 병렬 검색 → 최저가 선택
                best_price = price
                best_source = "musinsa"
                best_pid = product_id
                best_url = f"https://www.musinsa.com/products/{product_id}"

                try:
                    other_tasks = {}
                    for key, info in get_active().items():
                        if key == "musinsa":
                            continue
                        try:
                            other_tasks[key] = info["crawler"].search_products(
                                model_norm, limit=5,
                            )
                        except TypeError:
                            other_tasks[key] = info["crawler"].search_products(
                                model_norm,
                            )

                    if other_tasks:
                        other_results = await asyncio.gather(
                            *other_tasks.values(), return_exceptions=True,
                        )
                        for skey, sresult in zip(other_tasks.keys(), other_results):
                            if isinstance(sresult, Exception):
                                record_failure(skey)
                                continue
                            if sresult:
                                record_success(skey)
                            for r in (sresult or []):
                                r_price = r.get("price", 0)
                                if 0 < r_price < best_price:
                                    best_price = r_price
                                    best_source = skey
                                    best_pid = str(r.get("product_id", ""))
                                    best_url = r.get("url", "")
                except Exception as e:
                    logger.debug("멀티소스 검색 실패: %s", e)

                item = WatchlistItem(
                    kream_product_id=str(kream.get("product_id", "")),
                    model_number=model_norm,
                    kream_name=kream.get("name", ""),
                    musinsa_product_id=product_id,
                    musinsa_price=price,
                    kream_price=kream_price,
                    gap=best_price - kream_price,
                    source=best_source,
                    source_product_id=best_pid,
                    source_price=best_price,
                    source_url=best_url,
                )
                if self.watchlist.add(item):
                    result.added += 1

        except Exception as e:
            logger.debug("Tier1 리스팅 처리 실패: %s", e)
