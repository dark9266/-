"""One-shot: stale volume_7d=15 행 0 으로 리셋.

배경:
    commit 6170947 이전 `_estimate_volume` 이 pinia 5건 캡 × 3 = 15 로 뻥튀기.
    → kream_products.volume_7d 에 "15" 가 246행 박제되어 알림 경로에서
    하드 플로어(≥1) 무력화 + 가짜 거래량 표기 유발.

동작:
    1. volume_7d=15 AND last_volume_check<'2026-04-15' 행을 v7d=0 으로 리셋
    2. 다음 price_refresher 사이클이 돌면 hot tier 자동 재평가
    3. 진짜 15 인 레어 케이스는 refresher 가 되채움

안전장치: 실행 전 count, 실행 후 count 로그. idempotent.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kream_bot.db"

CUTOFF = "2026-04-15"


def main() -> int:
    if not DB.exists():
        print(f"[migrate] DB 없음: {DB}")
        return 1
    conn = sqlite3.connect(str(DB))
    before = conn.execute(
        "SELECT COUNT(*) FROM kream_products WHERE volume_7d=15 AND last_volume_check<?",
        (CUTOFF,),
    ).fetchone()[0]
    print(f"[migrate] stale v7d=15 rows (last_volume_check<{CUTOFF}): {before}")
    if before == 0:
        print("[migrate] 초기화할 행 없음 — noop")
        return 0
    conn.execute(
        "UPDATE kream_products SET volume_7d=0 WHERE volume_7d=15 AND last_volume_check<?",
        (CUTOFF,),
    )
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM kream_products WHERE volume_7d=15 AND last_volume_check<?",
        (CUTOFF,),
    ).fetchone()[0]
    print(f"[migrate] after: {after} (지워진 행: {before - after})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
