#!/usr/bin/env python3
"""대시보드용 alert_history 컬럼 확장 (비파괴, idempotent).

실행: PYTHONPATH=. python scripts/migrate_dashboard.py
"""

from __future__ import annotations

import sqlite3

from src.config import settings

NEW_COLUMNS = [
    ("source", "TEXT"),
    ("roi", "REAL"),
    ("direction", "TEXT"),
    ("retail_price", "INTEGER"),
    ("kream_sell_price", "INTEGER"),
    ("size", "TEXT"),
]


def main() -> None:
    conn = sqlite3.connect(settings.db_path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(alert_history)")}
        added = []
        for name, sql_type in NEW_COLUMNS:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE alert_history ADD COLUMN {name} {sql_type}")
            added.append(name)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM alert_history").fetchone()[0]
        print(f"✓ alert_history 컬럼 추가: {added or '(이미 존재)'}")
        print(f"✓ 기존 행 수: {total}건 (NULL로 남음)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
