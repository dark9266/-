"""공용 kream_collect_queue 쓰기 헬퍼.

19개 어댑터가 독립 sqlite3 연결로 동시 INSERT 하다 발생한
`database is locked` 경합을 단일 writer lock + 재시도로 해소한다.
모든 쓰기는 이 헬퍼를 거쳐 직렬화된다.

v3 푸시 전환(2026-04-13 저녁) 이후 어댑터 수가 19개로 늘면서 사이클 중
행 단위 INSERT 충돌이 빈발해 배치 API(`enqueue_collect_batch`) 를 추가했다.
어댑터는 가능하면 매칭 루프 끝에 배치를 한 번에 flush 하는 방향을 권장.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable

logger = logging.getLogger(__name__)

_writer_lock = threading.Lock()

_SQL = (
    "INSERT OR IGNORE INTO kream_collect_queue "
    "(model_number, brand_hint, name_hint, source, source_url) "
    "VALUES (?, ?, ?, ?, ?)"
)


def _acquire_conn(db_path: str) -> sqlite3.Connection:
    """busy_timeout 60s 로 커넥션 하나 확보 (WAL 모드 가정)."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def enqueue_collect(
    db_path: str,
    *,
    model_number: str,
    brand_hint: str,
    name_hint: str,
    source: str,
    source_url: str,
    attempts: int = 8,
) -> bool:
    """INSERT OR IGNORE INTO kream_collect_queue — 락+재시도로 직렬화."""
    delay = 0.3
    for i in range(attempts):
        try:
            with _writer_lock:
                conn = _acquire_conn(db_path)
                try:
                    cur = conn.execute(
                        _SQL,
                        (model_number, brand_hint, name_hint, source, source_url),
                    )
                    conn.commit()
                    return (cur.rowcount or 0) > 0
                finally:
                    conn.close()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or i == attempts - 1:
                raise
            logger.debug(
                "kream_collect_queue 쓰기 경합 (%d/%d): %s",
                i + 1, attempts, e,
            )
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
    return False


def enqueue_collect_batch(
    db_path: str,
    rows: Iterable[tuple[str, str, str, str, str]],
    *,
    attempts: int = 8,
) -> int:
    """여러 row 를 단일 트랜잭션으로 배치 삽입.

    rows: (model_number, brand_hint, name_hint, source, source_url) 튜플 iterable.
    반환: 실제 신규 삽입된 행 수 (INSERT OR IGNORE 로 중복은 제외).

    락 획득 창을 한 번만 열기 때문에 행 단위 호출보다 경합을 크게 줄인다.
    """
    batch = [
        (str(m or ""), str(b or ""), str(n or ""), str(s or ""), str(u or ""))
        for (m, b, n, s, u) in rows
        if m
    ]
    if not batch:
        return 0

    delay = 0.3
    for i in range(attempts):
        try:
            with _writer_lock:
                conn = _acquire_conn(db_path)
                try:
                    before = conn.total_changes
                    conn.executemany(_SQL, batch)
                    conn.commit()
                    return conn.total_changes - before
                finally:
                    conn.close()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or i == attempts - 1:
                raise
            logger.debug(
                "kream_collect_queue 배치 경합 (%d/%d, n=%d): %s",
                i + 1, attempts, len(batch), e,
            )
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
    return 0


async def aenqueue_collect(
    db_path: str,
    *,
    model_number: str,
    brand_hint: str,
    name_hint: str,
    source: str,
    source_url: str,
    attempts: int = 8,
) -> bool:
    """async wrapper — sync sqlite 경로를 스레드풀로 offload 해 이벤트 루프를 비차단한다."""
    return await asyncio.to_thread(
        enqueue_collect,
        db_path,
        model_number=model_number,
        brand_hint=brand_hint,
        name_hint=name_hint,
        source=source,
        source_url=source_url,
        attempts=attempts,
    )


async def aenqueue_collect_batch(
    db_path: str,
    rows: Iterable[tuple[str, str, str, str, str]],
    *,
    attempts: int = 8,
) -> int:
    """async wrapper — 배치 INSERT 를 스레드풀에서 돌려 asyncio loop 를 블로킹하지 않는다."""
    materialized = list(rows)
    return await asyncio.to_thread(
        enqueue_collect_batch,
        db_path,
        materialized,
        attempts=attempts,
    )
