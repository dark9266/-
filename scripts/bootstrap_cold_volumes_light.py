"""Cold-tier volume bootstrap (light, single-call per product).

기존 bootstrap_cold_volumes.py 가 4 호출/상품 사용 -> 9시간 + 캡 35% 부담.
이 light 버전은 _trades_from_screens_api 1 호출만 사용 -> 캡 ~10% 안전 운영.

이미 갱신된 상품 (last_volume_check 존재) 은 skip -> 자동 resume 가능.

진단: docs/diagnosis/2026-05-01-low-volume-match-spot-check.md

옵션:
    --sources thenorthface,nbkorea  특정 어댑터만 (콤마구분). 미지정 시 전 어댑터.
    --dry-run                       대상 건수 + 예상 캡 % + 시간만 출력 후 종료.
    --sleep 2.0                     호출 간 sleep (기본 1.5s).
    --limit 100                     디버그용. 처음 N건만 처리.
"""

import argparse
import asyncio
import sys

import aiosqlite

from src.config import settings
from src.core.kream_budget import BUDGET, kream_purpose
from src.crawlers.kream import kream_crawler


def _format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}분"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}시간 {rem}분"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="크림 cold-tier 거래량 부트스트랩 (light, 1 호출/상품)",
    )
    p.add_argument("--sources", type=str, default=None,
                   help="콤마구분 어댑터 이름. 미지정 시 전 어댑터 매칭.")
    p.add_argument("--dry-run", action="store_true",
                   help="대상 건수 + 예상 캡 %% + 시간만 출력. 실제 호출 없음.")
    p.add_argument("--sleep", type=float, default=1.5,
                   help="호출 간 sleep 초 (기본 1.5). 안전 모드 2.0~3.0 권장.")
    p.add_argument("--limit", type=int, default=None,
                   help="디버그용. 처음 N건만 처리.")
    return p.parse_args()


SQL_WITH_SOURCES = """
    SELECT DISTINCT kp.product_id
    FROM retail_products rp
    JOIN kream_products kp ON kp.model_number = rp.model_number
    WHERE rp.source IN ({placeholders})
      AND kp.model_number != ''
      AND kp.last_volume_check IS NULL
"""

SQL_ALL_SOURCES = """
    SELECT DISTINCT kp.product_id
    FROM retail_products rp
    JOIN kream_products kp ON kp.model_number = rp.model_number
    WHERE kp.model_number != ''
      AND kp.last_volume_check IS NULL
"""


async def _fetch_target_pids(
    db: aiosqlite.Connection,
    sources: tuple,
    limit,
) -> list:
    if sources:
        placeholders = ",".join(["?"] * len(sources))
        sql = SQL_WITH_SOURCES.format(placeholders=placeholders)
        params: tuple = sources
    else:
        sql = SQL_ALL_SOURCES
        params = ()

    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"

    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [r["product_id"] for r in rows]


async def _process_one(db, pid: str) -> str:
    result = await kream_crawler._trades_from_screens_api(pid)
    if result and (result.get("volume_7d", 0) > 0 or result.get("volume_30d", 0) > 0):
        new_tier = (
            "hot" if result["volume_7d"] >= settings.realtime_hot_volume_min else "cold"
        )
        await db.execute(
            "UPDATE kream_products SET volume_7d = ?, volume_30d = ?, "
            "refresh_tier = ?, last_volume_check = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP WHERE product_id = ?",
            (result["volume_7d"], result["volume_30d"], new_tier, pid),
        )
        await db.commit()
        return "vol_pos"
    else:
        await db.execute(
            "UPDATE kream_products SET last_volume_check = CURRENT_TIMESTAMP "
            "WHERE product_id = ?",
            (pid,),
        )
        await db.commit()
        return "vol_zero"


async def main() -> int:
    args = _parse_args()

    sources = None
    if args.sources:
        sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())

    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row

    try:
        pids = await _fetch_target_pids(db, sources, args.limit)
    except Exception as e:
        print(f"[bootstrap-light] 대상 조회 실패: {e}")
        await db.close()
        return 1

    target_label = (
        f"지정 어댑터 {len(sources)}개" if sources else "전 어댑터 매칭"
    )
    cap_pct = (len(pids) / BUDGET * 100) if BUDGET else 0.0
    eta_seconds = len(pids) * args.sleep

    if args.dry_run:
        print("[bootstrap-light] DRY RUN")
        print(f"  대상 건수      : {len(pids):,}")
        print(f"  예상 호출      : {len(pids):,} (1 호출/상품)")
        print(f"  KREAM 캡 사용  : {cap_pct:.1f}% ({len(pids):,} / {BUDGET:,})")
        print(f"  예상 소요      : {_format_duration(eta_seconds)} (sleep={args.sleep}s)")
        print(f"  대상 어댑터    : {target_label}")
        if sources:
            print(f"    -> {', '.join(sources)}")
        print()
        print("  실 실행 시: 위 인자에서 --dry-run 빼고 재실행")
        await db.close()
        return 0

    if not pids:
        print("[bootstrap-light] 대상 없음 — 종료 (이미 갱신됐거나 매칭 0건)")
        await db.close()
        return 0

    print(
        f"[bootstrap-light] 시작 | 대상 {len(pids):,}건 | 캡 {cap_pct:.1f}% | "
        f"예상 {_format_duration(eta_seconds)} | {target_label}"
    )

    vol_pos = vol_zero = error = 0
    interrupted = False

    try:
        with kream_purpose("bootstrap_light"):
            for i, pid in enumerate(pids, 1):
                try:
                    outcome = await _process_one(db, pid)
                    if outcome == "vol_pos":
                        vol_pos += 1
                    else:
                        vol_zero += 1
                except Exception as e:
                    error += 1
                    print(f"[bootstrap-light] {pid} 에러: {e}")

                if i % 50 == 0 or i == len(pids):
                    remaining = len(pids) - i
                    eta_left = remaining * args.sleep
                    print(
                        f"[bootstrap-light] {i:,}/{len(pids):,} "
                        f"(vol>0 {vol_pos} / vol=0 {vol_zero} / 에러 {error}) | "
                        f"ETA: {_format_duration(eta_left)}"
                    )

                await asyncio.sleep(args.sleep)
    except KeyboardInterrupt:
        interrupted = True
        print()
        print("[bootstrap-light] 사용자 중단 (Ctrl+C)")

    print()
    if interrupted:
        print(f"[bootstrap-light] 중단 시점 요약: 처리 {vol_pos + vol_zero + error}건")
        print(f"  거래량>0 {vol_pos} / 거래량=0 {vol_zero} / 에러 {error}")
        print("  재실행 시 자동 resume — last_volume_check 채워진 건 skip.")
    else:
        print(
            f"[bootstrap-light] 완료: 거래량>0 {vol_pos} / "
            f"거래량=0 {vol_zero} / 에러 {error} / 총 {len(pids)}"
        )

    await db.close()
    return 0 if error < len(pids) * 0.5 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
