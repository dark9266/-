"""거래량 기반 우선순위 시세 갱신.

hot tier (7일 거래량 >= 5): 30분마다 시세 갱신
cold tier (7일 거래량 < 5): 6시간마다 시세 갱신
우선순위 큐: hot 먼저, 같은 tier 내에서는 마지막 갱신이 오래된 순.
"""

from datetime import datetime

import aiosqlite

from src.crawlers.kream import kream_crawler
from src.config import settings
from src.utils.logging import setup_logger

logger = setup_logger("kream_price_refresher")


class KreamPriceRefresher:
    """크림 시세 우선순위 갱신기."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def build_refresh_queue(self, batch_size: int | None = None) -> list:
        """갱신이 필요한 상품을 우선순위 큐로 반환.

        1순위: hot tier + last_price_refresh가 30분 이상 지난 상품
        2순위: cold tier + last_price_refresh가 6시간 이상 지난 상품
        정렬: hot 먼저, 같은 tier 내에서는 last_price_refresh ASC (오래된 순)
        """
        if batch_size is None:
            batch_size = settings.realtime_refresh_batch_size

        hot_minutes = settings.realtime_hot_refresh_minutes
        cold_hours = settings.realtime_cold_refresh_hours

        cursor = await self.db.execute(
            """SELECT product_id, name, model_number, volume_7d, refresh_tier, last_price_refresh
            FROM kream_products
            WHERE model_number != ''
            AND (
                (refresh_tier = 'hot' AND (
                    last_price_refresh IS NULL
                    OR last_price_refresh < datetime('now', ?)
                ))
                OR
                (refresh_tier = 'cold' AND (
                    last_price_refresh IS NULL
                    OR last_price_refresh < datetime('now', ?)
                ))
            )
            ORDER BY
                CASE refresh_tier WHEN 'hot' THEN 0 ELSE 1 END,
                last_price_refresh ASC NULLS FIRST
            LIMIT ?""",
            (f"-{hot_minutes} minutes", f"-{cold_hours} hours", batch_size),
        )
        return await cursor.fetchall()

    async def refresh_product(self, product_id: str) -> bool:
        """단일 상품 시세 + 거래량 갱신. 성공 여부 반환."""
        try:
            detail = await kream_crawler.get_full_product_info(product_id)
            if not detail:
                logger.warning("시세 갱신 실패 (상세 없음): %s", product_id)
                return False

            if detail.size_prices:
                for sp in detail.size_prices:
                    await self.db.execute(
                        """INSERT INTO kream_price_history
                        (product_id, size, sell_now_price, buy_now_price, bid_count, last_sale_price)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (product_id, sp.size, sp.sell_now_price, sp.buy_now_price,
                         sp.bid_count or 0, sp.last_sale_price),
                    )

            new_volume = detail.volume_7d or 0
            new_tier = "hot" if new_volume >= settings.realtime_hot_volume_min else "cold"

            await self.db.execute(
                """UPDATE kream_products SET
                    volume_7d = ?, volume_30d = ?, refresh_tier = ?,
                    last_price_refresh = CURRENT_TIMESTAMP,
                    last_volume_check = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ?""",
                (new_volume, detail.volume_30d or 0, new_tier, product_id),
            )
            await self.db.commit()
            logger.debug("시세 갱신: %s (vol=%d, tier=%s)", product_id, new_volume, new_tier)
            return True

        except Exception as e:
            logger.error("시세 갱신 에러 (%s): %s", product_id, e)
            return False

    async def update_product_tier(self, product_id: str, new_volume_7d: int) -> None:
        """상품의 tier를 거래량 기준으로 업데이트."""
        new_tier = "hot" if new_volume_7d >= settings.realtime_hot_volume_min else "cold"
        await self.db.execute(
            "UPDATE kream_products SET volume_7d = ?, refresh_tier = ? WHERE product_id = ?",
            (new_volume_7d, new_tier, product_id),
        )
        await self.db.commit()

    async def run(self) -> dict:
        """배치 시세 갱신 실행."""
        started = datetime.now()
        queue = await self.build_refresh_queue()
        queue_size = len(queue)

        refreshed = 0
        failed = 0

        for row in queue:
            success = await self.refresh_product(row["product_id"])
            if success:
                refreshed += 1
            else:
                failed += 1

        elapsed = (datetime.now() - started).total_seconds()
        logger.info("시세 갱신 완료: %d/%d 성공 (큐 %d, %.0f초)", refreshed, queue_size, queue_size, elapsed)
        return {"refreshed": refreshed, "failed": failed, "queue_size": queue_size, "elapsed": elapsed}
