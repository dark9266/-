"""거래량 급등 감지.

직전 스냅샷 대비 거래량이 threshold 배 이상 증가한 상품을 감지하고
해당 상품의 refresh_tier를 hot으로 승격한다.
"""

from datetime import datetime

import aiosqlite

from src.config import settings
from src.crawlers.kream import kream_crawler
from src.utils.logging import setup_logger

logger = setup_logger("kream_volume_spike")


class VolumeSpikeDetector:
    """거래량 급등 감지기."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def detect_spikes(
        self,
        current_volumes: list[dict],
        threshold: float | None = None,
    ) -> list[dict]:
        """현재 거래량과 직전 스냅샷을 비교하여 급등 상품 반환.

        Args:
            current_volumes: [{"product_id": str, "volume_7d": int, "volume_30d": int}]
            threshold: 급등 판정 배율 (기본 settings.realtime_spike_threshold)

        Returns:
            [{"product_id": str, "prev_volume": int, "curr_volume": int, "ratio": float}]
        """
        if threshold is None:
            threshold = settings.realtime_spike_threshold

        spikes = []

        for vol in current_volumes:
            pid = vol["product_id"]
            curr = vol["volume_7d"]

            cursor = await self.db.execute(
                """SELECT volume_7d FROM kream_volume_snapshots
                WHERE product_id = ?
                ORDER BY snapshot_at DESC LIMIT 1""",
                (pid,),
            )
            prev_row = await cursor.fetchone()

            if not prev_row:
                continue

            prev = prev_row["volume_7d"]

            if prev == 0:
                if curr >= 3:
                    spikes.append({
                        "product_id": pid,
                        "prev_volume": prev,
                        "curr_volume": curr,
                        "ratio": float("inf"),
                    })
                continue

            ratio = curr / prev
            if ratio >= threshold:
                spikes.append({
                    "product_id": pid,
                    "prev_volume": prev,
                    "curr_volume": curr,
                    "ratio": round(ratio, 2),
                })

        if spikes:
            logger.info("거래량 급등 감지: %d건", len(spikes))
            for s in spikes:
                logger.info("  %s: %d → %d (x%.1f)", s["product_id"], s["prev_volume"], s["curr_volume"], s["ratio"])

        return spikes

    async def save_snapshots(self, volumes: list[dict]) -> None:
        """현재 거래량을 스냅샷으로 저장."""
        for vol in volumes:
            await self.db.execute(
                """INSERT INTO kream_volume_snapshots (product_id, volume_7d, volume_30d)
                VALUES (?, ?, ?)""",
                (vol["product_id"], vol.get("volume_7d", 0), vol.get("volume_30d", 0)),
            )
        await self.db.commit()

    async def promote_spiked_products(self, spikes: list[dict]) -> None:
        """급등 상품을 hot tier로 승격."""
        for spike in spikes:
            await self.db.execute(
                """UPDATE kream_products SET
                    refresh_tier = 'hot',
                    volume_7d = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ?""",
                (spike["curr_volume"], spike["product_id"]),
            )
        await self.db.commit()
        if spikes:
            logger.info("%d개 상품 hot tier 승격", len(spikes))

    async def collect_current_volumes(self, batch_size: int = 50) -> list[dict]:
        """DB에서 거래량 체크가 필요한 상품을 선별하여 크림 API로 현재 거래량 수집."""
        cursor = await self.db.execute(
            """SELECT product_id, model_number FROM kream_products
            WHERE model_number != ''
            AND (last_volume_check IS NULL
                 OR last_volume_check < datetime('now', ?))
            ORDER BY last_volume_check ASC NULLS FIRST
            LIMIT ?""",
            (f"-{settings.realtime_volume_check_minutes} minutes", batch_size),
        )
        rows = await cursor.fetchall()

        volumes = []
        for row in rows:
            pid = row["product_id"]
            trade = await kream_crawler.get_trade_history(pid)
            volumes.append({
                "product_id": pid,
                "volume_7d": trade.get("volume_7d", 0),
                "volume_30d": trade.get("volume_30d", 0),
            })
            # volume_7d + scan_priority + refresh_tier 함께 갱신
            new_vol = trade.get("volume_7d", 0)
            new_tier = "hot" if new_vol >= settings.realtime_hot_volume_min else "cold"
            if new_vol >= 5:
                scan_pri = "hot"
            elif new_vol >= 3:
                scan_pri = "warm"
            else:
                scan_pri = "cold"
            await self.db.execute(
                """UPDATE kream_products SET
                    volume_7d = ?, volume_30d = ?, refresh_tier = ?,
                    scan_priority = ?,
                    last_volume_check = CURRENT_TIMESTAMP
                WHERE product_id = ?""",
                (new_vol, trade.get("volume_30d", 0), new_tier, scan_pri, pid),
            )

        await self.db.commit()
        return volumes

    async def run(self) -> dict:
        """급등 감지 전체 사이클."""
        started = datetime.now()
        volumes = await self.collect_current_volumes()

        if not volumes:
            return {"checked": 0, "spikes": 0, "promoted": [], "elapsed": 0}

        spikes = await self.detect_spikes(volumes)
        await self.save_snapshots(volumes)

        promoted = []
        if spikes:
            await self.promote_spiked_products(spikes)
            promoted = [s["product_id"] for s in spikes]

        elapsed = (datetime.now() - started).total_seconds()
        return {"checked": len(volumes), "spikes": len(spikes), "promoted": promoted, "elapsed": elapsed}
