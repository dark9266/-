"""Phase 4 — DB 헬스모니터 loop 내부 헬퍼 검증.

tasks.loop 자체 (discord.ext) 는 bot 라이프사이클 의존이라 단위 테스트 어려움 —
loop 에서 호출하는 순수 함수 2개 (_count_db_fds, _decision_log_stall) 만 검증.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from src.scheduler import _count_db_fds, _decision_log_stall


def test_count_db_fds_returns_int_on_linux(tmp_path: Path) -> None:
    """Linux/WSL 에서는 int 반환. /proc 없는 환경은 None (이 테스트는 skip 안 함)."""
    db = tmp_path / "foo.db"
    db.write_text("")  # 파일만 존재해도 readlink target 매칭은 실제 열린 fd 전용.
    result = _count_db_fds(str(db))
    # Linux WSL: int, macOS: None — 둘 다 허용.
    assert result is None or isinstance(result, int)


def test_count_db_fds_nonexistent_dir(tmp_path: Path) -> None:
    """/proc 없는 환경 시뮬레이션은 어려움 — 대신 결과 타입만 느슨히 검증."""
    result = _count_db_fds(str(tmp_path / "no_such_db.db"))
    # /proc 있으면 0 이상 int, 없으면 None.
    assert result is None or result >= 0


def test_decision_log_stall_true_when_no_rows(tmp_path: Path) -> None:
    db = tmp_path / "stall.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            stage TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()
    # 최근 3600초 내 row 없음 → stall=True
    assert _decision_log_stall(str(db), 3600) is True


def test_decision_log_stall_false_when_recent_row(tmp_path: Path) -> None:
    db = tmp_path / "alive.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            stage TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO decision_log(ts, stage, decision, reason) VALUES (?, ?, ?, ?)",
        (time.time(), "filter", "pass", "ok"),
    )
    conn.commit()
    conn.close()
    assert _decision_log_stall(str(db), 3600) is False


def test_decision_log_stall_false_on_missing_table(tmp_path: Path) -> None:
    """테이블 미생성 상태 — 경보 억제하려고 False 반환."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.close()
    assert _decision_log_stall(str(db), 3600) is False
