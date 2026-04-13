"""공용 kream_collect_queue 쓰기 헬퍼.

19개 어댑터가 독립 sqlite3 연결로 동시 INSERT 하다 발생한
`database is locked` 경합을 단일 writer lock + 재시도로 해소한다.
모든 쓰기는 이 헬퍼를 거쳐 직렬화된다.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

_writer_lock = threading.Lock()


def enqueue_collect(
    db_path: str,
    *,
    model_number: str,
    brand_hint: str,
    name_hint: str,
    source: str,
    source_url: str,
    attempts: int = 5,
) -> bool:
    """INSERT OR IGNORE INTO kream_collect_queue — 락+재시도로 직렬화."""
    delay = 0.3
    for i in range(attempts):
        try:
            with _writer_lock:
                conn = sqlite3.connect(db_path, timeout=30.0)
                try:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO kream_collect_queue "
                        "(model_number, brand_hint, name_hint, source, source_url) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            model_number,
                            brand_hint,
                            name_hint,
                            source,
                            source_url,
                        ),
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
            delay *= 2
    return False
