"""src/core/db.py — PRAGMA helper 단위 테스트."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from src.core.db import (
    apply_write_pragmas,
    apply_write_pragmas_sync,
    async_connect,
    sync_connect,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "test.db"
    # 빈 DB 하나 생성 (WAL 모드 전환 가능하도록 real file 필요)
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return str(p)


def test_sync_connect_applies_write_pragmas(db_path: str) -> None:
    with sync_connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        autockpt = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode == "wal"
        assert sync == 1  # NORMAL
        assert autockpt == 1000
        assert busy == 30000


def test_sync_connect_read_only_skips_write_pragmas(db_path: str) -> None:
    # read_only에서 WAL PRAGMA 설정 시도 → 파일 잠금 없이 통과 (busy_timeout만)
    with sync_connect(db_path, read_only=True) as conn:
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy == 30000
        # 쓰기 시도 → read-only 실패
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")


def test_sync_connect_closes_on_exception(db_path: str) -> None:
    conn_ref = None
    with pytest.raises(ValueError):
        with sync_connect(db_path) as conn:
            conn_ref = conn
            raise ValueError("boom")
    # 예외 후에도 close 호출되어야 함 — close된 conn에 execute 시도 시 에러
    with pytest.raises(sqlite3.ProgrammingError):
        conn_ref.execute("SELECT 1")  # type: ignore[union-attr]


async def test_async_connect_applies_write_pragmas(db_path: str) -> None:
    async with async_connect(db_path) as conn:
        cur = await conn.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
        cur = await conn.execute("PRAGMA synchronous")
        sync = (await cur.fetchone())[0]
        cur = await conn.execute("PRAGMA wal_autocheckpoint")
        autockpt = (await cur.fetchone())[0]
        cur = await conn.execute("PRAGMA busy_timeout")
        busy = (await cur.fetchone())[0]
        assert mode == "wal"
        assert sync == 1
        assert autockpt == 1000
        assert busy == 30000


async def test_async_connect_read_only(db_path: str) -> None:
    async with async_connect(db_path, read_only=True) as conn:
        cur = await conn.execute("PRAGMA busy_timeout")
        busy = (await cur.fetchone())[0]
        assert busy == 30000


async def test_async_connect_closes_on_exception(db_path: str) -> None:
    with pytest.raises(ValueError):
        async with async_connect(db_path) as conn:
            raise ValueError("boom")


async def test_apply_write_pragmas_on_open_connection(db_path: str) -> None:
    conn = await aiosqlite.connect(db_path, timeout=30.0)
    try:
        await apply_write_pragmas(conn)
        cur = await conn.execute("PRAGMA synchronous")
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()


def test_apply_write_pragmas_sync_on_open_connection(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        apply_write_pragmas_sync(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()
