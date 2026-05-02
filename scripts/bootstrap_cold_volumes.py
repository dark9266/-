"""Cold-tier volume bootstrap.

4 브랜드 (thenorthface/nbkorea/puma/reebok) 의 매칭 kream_products 거래량을
1회 채워 prefilter_low_volume 차단 해소.

진단: docs/diagnosis/2026-05-01-low-volume-match-spot-check.md
실행 전 봇 정지 필수 (캡 충돌 회피).
"""

import asyncio
import sys

import aiosqlite

from src.config import settings
from src.core.kream_budget import kream_purpose
from src.kream_realtime.price_refresher import KreamPriceRefresher

SOURCES = ("thenorthface", "nbkorea", "puma", "reebok")


async def main() -> int:
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row

    placeholders = ",".join(["?"] * len(SOURCES))
    cursor = await db.execute(
        f"""
        SELECT DISTINCT kp.product_id
        FROM retail_products rp
        JOIN kream_products kp ON kp.model_number = rp.model_number
        WHERE rp.source IN ({placeholders}) AND kp.model_number != ''
        """,
        SOURCES,
    )
    rows = await cursor.fetchall()
    pids = [r["product_id"] for r in rows]
    print(f"[bootstrap] 대상: {len(pids)}건 (sources={SOURCES})")

    if not pids:
        print("[bootstrap] 대상 없음 — 종료")
        await db.close()
        return 0

    refresher = KreamPriceRefresher(db)
    success = 0
    fail = 0

    with kream_purpose("bootstrap_cold_volume"):
        for i, pid in enumerate(pids, 1):
            try:
                ok = await refresher.refresh_product(pid)
                if ok:
                    success += 1
                else:
                    fail += 1
            except Exception as e:
                fail += 1
                print(f"[bootstrap] {pid} 에러: {e}")

            if i % 50 == 0 or i == len(pids):
                print(f"[bootstrap] {i}/{len(pids)} (성공 {success} / 실패 {fail})")

            await asyncio.sleep(1.5)

    print(f"[bootstrap] 완료: 성공 {success} / 실패 {fail} / 총 {len(pids)}")
    await db.close()
    return 0 if fail < len(pids) * 0.5 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
