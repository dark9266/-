"""DB connection helper — PRAGMA 4종 자동 적용 (2026-04-18 WAL incident 대응).

50+ 지점에서 각자 sqlite3/aiosqlite.connect() 하던 것을 이 helper로 통합.
모든 writer connection에 동일 PRAGMA 보장 → checkpoint 경합 해소.

PRAGMA 4종:
    - journal_mode=WAL        : WAL 모드 (DB 레벨 영구 설정, 한 번만 적용되면 됨)
    - synchronous=NORMAL      : WAL 모드 권장값. writer 블로킹 완화
    - wal_autocheckpoint=1000 : WAL 페이지 1000 초과 시 자동 checkpoint (약 4MB)
    - busy_timeout=30000      : lock 발생 시 최대 30초 대기 후 포기

read-only URI 모드(`file:{path}?mode=ro`)에서는 journal_mode 변경 불가이므로
busy_timeout만 적용.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import aiosqlite

_WRITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA wal_autocheckpoint=1000",
    "PRAGMA busy_timeout=30000",
)
_READONLY_PRAGMAS = ("PRAGMA busy_timeout=30000",)


@contextmanager
def sync_connect(
    db_path: str,
    *,
    read_only: bool = False,
    timeout: float = 30.0,
    row_factory: type | None = sqlite3.Row,
) -> Iterator[sqlite3.Connection]:
    """동기 SQLite 연결 — PRAGMA 자동 적용 + close 보장."""
    if read_only:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=timeout
        )
    else:
        conn = sqlite3.connect(db_path, timeout=timeout)
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        pragmas = _READONLY_PRAGMAS if read_only else _WRITE_PRAGMAS
        for p in pragmas:
            conn.execute(p)
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def async_connect(
    db_path: str,
    *,
    read_only: bool = False,
    timeout: float = 30.0,
    row_factory: bool = True,
) -> AsyncIterator[aiosqlite.Connection]:
    """비동기 aiosqlite 연결 — PRAGMA 자동 적용 + close 보장."""
    if read_only:
        conn = await aiosqlite.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=timeout
        )
    else:
        conn = await aiosqlite.connect(db_path, timeout=timeout)
    if row_factory:
        conn.row_factory = aiosqlite.Row
    try:
        pragmas = _READONLY_PRAGMAS if read_only else _WRITE_PRAGMAS
        for p in pragmas:
            await conn.execute(p)
        yield conn
    finally:
        await conn.close()


async def apply_write_pragmas(db: aiosqlite.Connection) -> None:
    """이미 열린 long-lived connection에 PRAGMA 4종 적용.

    `CheckpointStore.init()` / `Database.connect()` 같이 self._db를 오래 보관하는
    곳에서 connect 직후 호출. `async_connect` context manager 를 못 쓰는 경우.
    """
    for p in _WRITE_PRAGMAS:
        await db.execute(p)


def apply_write_pragmas_sync(conn: sqlite3.Connection) -> None:
    """동기 버전 — sqlite3.Connection에 PRAGMA 4종 적용."""
    for p in _WRITE_PRAGMAS:
        conn.execute(p)


__all__ = [
    "apply_write_pragmas",
    "apply_write_pragmas_sync",
    "async_connect",
    "sync_connect",
]
