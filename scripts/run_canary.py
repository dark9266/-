"""Canary regression runner.

tests/fixtures/canary_matches.json 의 소싱처↔크림 정답 페어를 검증.
- 크림 API 호출 0 (박제 product_id 비교만)
- pre-commit 훅 / /verify / pytest 에서 호출
- 실패 시 exit 1 → 커밋 차단
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "canary_matches.json"
DB_PATH = ROOT / "data" / "kream_bot.db"


def load_canary() -> list[dict]:
    with FIXTURE.open(encoding="utf-8") as f:
        return json.load(f)["pairs"]


def verify_pair(conn: sqlite3.Connection, pair: dict) -> tuple[bool, str]:
    """Return (passed, reason)."""
    pid = pair["kream_product_id"]
    row = conn.execute(
        "SELECT product_id, name, model_number FROM kream_products WHERE product_id=?",
        (pid,),
    ).fetchone()
    if not row:
        return False, f"kream#{pid} DB 에 없음 (삭제되었거나 id 변경)"

    db_name = row[1] or ""
    expected_name = pair["kream_name"]
    if db_name.strip() != expected_name.strip():
        return False, f"kream#{pid} 이름 불일치\n    expected: {expected_name}\n    actual:   {db_name}"

    return True, "OK"


def main() -> int:
    if not DB_PATH.exists():
        print(f"[canary] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    pairs = load_canary()
    conn = sqlite3.connect(str(DB_PATH))

    failures: list[tuple[dict, str]] = []
    for p in pairs:
        ok, reason = verify_pair(conn, p)
        marker = "✓" if ok else "✗"
        label = f"#{p['id']:2d} [{p['source']:10s}] {p['model_number']:16s} → kream#{p['kream_product_id']}"
        print(f"  {marker} {label}")
        if not ok:
            failures.append((p, reason))

    total = len(pairs)
    print()
    if failures:
        print(f"[canary] FAIL {len(failures)}/{total}")
        for p, reason in failures:
            print(f"  #{p['id']} {p['source']} {p['model_number']}: {reason}")
        print()
        print("→ 수정하면 안 되는 매칭이 깨졌습니다. 원인 확인 후 커밋 차단.")
        print("→ 크림 상품 자체가 재발매/변경되어 정당한 경우 tests/fixtures/canary_matches.json 수동 갱신.")
        return 1

    print(f"[canary] PASS {total}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
