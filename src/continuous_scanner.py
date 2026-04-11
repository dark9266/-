"""v2 연속 배치 스캐너 — hot/warm/cold 우선순위 큐 기반 전체 커버리지.

기존 역방향 스캔(hot 50건)을 확장하여 DB 전체 47k+ 상품을 순환 스캔.
5분 주기, 50건/배치, 우선순위별 TTL로 스캔 빈도 차등화.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.config import settings
from src.models.database import Database
from src.models.product import AutoScanOpportunity, Signal
from src.reverse_scanner import ReverseLookupResult, ReverseLookupScanner
from src.utils.logging import setup_logger

logger = setup_logger("continuous_scanner")


@dataclass
class ContinuousScanResult:
    """연속 배치 스캔 결과."""

    batch_size: int = 0
    processed: int = 0
    sourced: int = 0
    profitable: int = 0
    errors: int = 0
    elapsed_sec: float = 0.0
    opportunities: list[AutoScanOpportunity] = field(default_factory=list)


class ContinuousScanner:
    """v2 연속 배치 스캐너.

    ReverseLookupScanner._process_hot_product를 재사용하여
    DB 전체 상품을 우선순위 큐 기반으로 순환 스캔한다.
    """

    def __init__(
        self,
        db: Database,
        scanner: ReverseLookupScanner,
        config=settings,
    ):
        self.db = db
        self.scanner = scanner
        self.config = config
        self._backfill_done = False

    async def run_batch(
        self,
        on_progress=None,
        on_opportunity=None,
    ) -> ContinuousScanResult:
        """1회 배치 실행.

        1) backfill (첫 실행 시)
        2) 큐에서 batch_size건 조회
        3) _process_hot_product로 처리
        4) next_scan_at 업데이트
        """
        result = ContinuousScanResult()
        started = datetime.now()

        # backfill (첫 실행 시)
        await self._ensure_backfill()

        # 큐 조회
        queue = await self.db.get_continuous_scan_queue(
            self.config.continuous_scan_batch_size,
        )
        result.batch_size = len(queue)

        if not queue:
            logger.info("연속 스캔: 큐 비어있음 (모든 상품 스캔 완료)")
            return result

        if on_progress:
            await on_progress(
                f"🔄 연속 스캔 시작: {len(queue)}건 "
                f"(hot/warm/cold 우선순위)"
            )

        # 동시성 제어
        sem = asyncio.Semaphore(self.config.httpx_concurrency)

        async def process_one(product: dict) -> None:
            async with sem:
                product_id = product["product_id"]
                priority = product.get("scan_priority", "cold")

                try:
                    # 임시 ReverseLookupResult (카운터 추적용)
                    tmp_result = ReverseLookupResult()

                    opportunity = await self.scanner._process_hot_product(
                        product, tmp_result,
                    )

                    # 결과 집계
                    result.processed += 1
                    if tmp_result.sourced > 0:
                        result.sourced += 1

                    profitable = False
                    if opportunity and opportunity.best_confirmed_profit > 0:
                        result.profitable += 1
                        result.opportunities.append(opportunity)
                        profitable = True

                        if on_opportunity:
                            await on_opportunity(opportunity)

                    # next_scan_at 업데이트
                    next_at = self._calculate_next_scan(priority, profitable)
                    await self.db.update_scan_schedule(
                        product_id, priority, next_at.isoformat(),
                    )

                except Exception as e:
                    result.errors += 1
                    logger.warning(
                        "연속 스캔 에러 (%s): %s", product_id, e,
                    )
                    # 에러 시 1시간 후 재시도
                    retry_at = datetime.now() + timedelta(hours=1)
                    try:
                        await self.db.update_scan_schedule(
                            product_id, priority, retry_at.isoformat(),
                        )
                    except Exception:
                        pass

        await asyncio.gather(
            *[process_one(p) for p in queue],
            return_exceptions=True,
        )

        # 수익 기회 정렬
        result.opportunities.sort(key=lambda o: -o.best_confirmed_profit)

        elapsed = (datetime.now() - started).total_seconds()
        result.elapsed_sec = elapsed

        logger.info(
            "연속 스캔 완료: %d건 → 소싱 %d → 수익 %d (에러 %d, %.0f초)",
            result.batch_size, result.sourced, result.profitable,
            result.errors, elapsed,
        )

        if on_progress:
            await on_progress(
                f"✅ 연속 스캔 완료: {result.batch_size}건\n"
                f"- 소싱 매칭: {result.sourced}건\n"
                f"- 수익 기회: {result.profitable}건\n"
                f"- 소요시간: {elapsed:.0f}초"
            )

        return result

    async def _ensure_backfill(self) -> None:
        """next_scan_at NULL 상품 존재 시 1회 backfill."""
        if self._backfill_done:
            return

        cursor = await self.db.db.execute(
            "SELECT COUNT(*) as cnt FROM kream_products "
            "WHERE model_number != '' AND next_scan_at IS NULL"
        )
        row = await cursor.fetchone()
        null_count = row["cnt"] if row else 0

        # 현재 volume_7d 기준으로 scan_priority 재분류
        stats = await self.db.reclassify_scan_priorities()
        logger.info(
            "scan_priority 재분류: hot=%d, warm=%d, cold=%d",
            stats.get("hot", {}).get("total", 0),
            stats.get("warm", {}).get("total", 0),
            stats.get("cold", {}).get("total", 0),
        )

        if null_count > 0:
            logger.info("연속 스캔 backfill 시작: %d건 스케줄 미배정", null_count)
            await self.db.backfill_scan_schedule(
                hot_ttl_h=self.config.continuous_hot_ttl_hours,
                warm_ttl_h=self.config.continuous_warm_ttl_hours,
                cold_ttl_h=self.config.continuous_cold_ttl_hours,
                first_batch_size=self.config.continuous_scan_batch_size,
            )

        self._backfill_done = True

    def _calculate_next_scan(self, priority: str, profitable: bool) -> datetime:
        """우선순위 + 수익 여부에 따른 다음 스캔 시각."""
        ttl_map = {
            "hot": self.config.continuous_hot_ttl_hours,
            "warm": self.config.continuous_warm_ttl_hours,
            "cold": self.config.continuous_cold_ttl_hours,
        }
        ttl_h = ttl_map.get(priority, 48)
        if profitable:
            ttl_h = max(1, ttl_h // 2)
        return datetime.now() + timedelta(hours=ttl_h)
