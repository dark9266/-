"""Cold-tier volume bootstrap (⛔ 폐기 — 2-v2 F1 로 실행 차단).

4 브랜드 (thenorthface/nbkorea/puma/reebok) 의 매칭 kream_products 거래량을
1회 채워 prefilter_low_volume 차단 해소.

진단: docs/diagnosis/2026-05-01-low-volume-match-spot-check.md

⛔ **실행 차단 (2026-07-25, 코덱스 수렴 라운드 F1)** — 이 스크립트는
`KreamPriceRefresher` 로 크림을 직접 때린다: 백그라운드 브로커
(`acquire_background`) 도, 페이서(2.5~3.5s 지터) 도, 램프 소프트캡·라이브
예약분도, 배치 상호배제 잠금도 **전부 우회**한다. purpose 도
`BG_PURPOSE_PREFIXES` 에 없어 소프트캡 소비로 집계조차 안 된다 —
2단계(2-0~2-v)가 세운 실계정 보호막에 뚫린 구멍이었다.

**대체**: `scripts/bootstrap_cold_volumes_light.py` (동일 목적, 브로커·페이서·
잠금·상태모델 전부 배선됨). 굳이 이 레거시를 돌려야 하면
`KREAM_LEGACY_BOOTSTRAP_GO=1` 을 명시적으로 세워야 한다 — 그때는 캡·페이싱을
사람이 책임진다.
"""

import asyncio
import os
import sys

import aiosqlite

from src.config import settings
from src.core.kream_budget import kream_purpose
from src.kream_realtime.price_refresher import KreamPriceRefresher

SOURCES = ("thenorthface", "nbkorea", "puma", "reebok")


LEGACY_GO_ENV = "KREAM_LEGACY_BOOTSTRAP_GO"


async def main() -> int:
    if os.getenv(LEGACY_GO_ENV) != "1":
        print(
            "[bootstrap] ⛔ 폐기된 경로 — 실행 거부.\n"
            "  이 스크립트는 백그라운드 브로커·페이서·소프트캡·배치잠금을 전부 우회한다"
            " (2-v2 F1).\n"
            "  → scripts/bootstrap_cold_volumes_light.py 를 쓸 것.\n"
            f"  그래도 돌려야 하면 {LEGACY_GO_ENV}=1 을 명시적으로 세워라."
        )
        return 1

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
