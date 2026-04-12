"""체크포인트 스토어 — 이벤트 영속화 + 재시작 복구 (Phase 2.3a).

event_bus 런타임이 강제 종료되거나 크래시될 때 처리 중이던
이벤트를 잃지 않도록 SQLite에 영속화한다.

흐름:
    1) 컨슈머가 이벤트 수신 직전 `record(event, consumer)` → checkpoint_id 획득
    2) 처리 성공 후 `mark_consumed(checkpoint_id)`
    3) 재시작 시 `replay(consumer)` 로 미처리 이벤트 재구성해 재처리

event_bus 모듈의 frozen dataclass 5종 (`CatalogDumped`, `CandidateMatched`,
`ProfitFound`, `AlertSent`, `AlertFollowup`) 을 dataclasses.asdict 로
JSON 직렬화하고, 복원 시 `event_type` 문자열로 클래스를 찾아 `Cls(**payload)`.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import aiosqlite

from src.core import event_bus as _event_bus_module

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_checkpoint (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    consumed_at REAL,
    consumer TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ckpt_pending
    ON event_checkpoint(consumer, consumed_at);
CREATE INDEX IF NOT EXISTS idx_event_ckpt_created
    ON event_checkpoint(created_at);
"""


def _resolve_event_class(event_type: str) -> type:
    """`event_type` 문자열에서 event_bus 모듈의 Event 서브클래스 찾기."""
    cls = getattr(_event_bus_module, event_type, None)
    if cls is None or not isinstance(cls, type):
        raise ValueError(f"unknown event_type: {event_type!r}")
    base = getattr(_event_bus_module, "Event", None)
    if base is not None and not issubclass(cls, base):
        raise ValueError(f"{event_type!r} is not an Event subclass")
    return cls


class CheckpointStore:
    """이벤트 체크포인트 영속화 스토어 (aiosqlite)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """DB 연결 및 테이블 생성."""
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        """DB 연결 종료."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("CheckpointStore.init() must be called first")
        return self._db

    async def record(self, event: object, consumer: str) -> int:
        """이벤트를 체크포인트 테이블에 저장하고 id 반환.

        `event` 는 frozen dataclass 인스턴스여야 한다.
        """
        if not dataclasses.is_dataclass(event) or isinstance(event, type):
            raise TypeError(
                f"record requires a dataclass instance, got {type(event).__name__}"
            )
        db = self._require_db()
        event_type = type(event).__name__
        payload = json.dumps(dataclasses.asdict(event), ensure_ascii=False)
        created_at = time.time()
        cursor = await db.execute(
            "INSERT INTO event_checkpoint "
            "(event_type, payload, created_at, consumed_at, consumer) "
            "VALUES (?, ?, ?, NULL, ?)",
            (event_type, payload, created_at, consumer),
        )
        await db.commit()
        checkpoint_id = cursor.lastrowid
        await cursor.close()
        assert checkpoint_id is not None
        return int(checkpoint_id)

    async def mark_consumed(self, checkpoint_id: int) -> None:
        """해당 체크포인트를 처리 완료로 표시."""
        db = self._require_db()
        await db.execute(
            "UPDATE event_checkpoint SET consumed_at = ? WHERE id = ?",
            (time.time(), checkpoint_id),
        )
        await db.commit()

    async def pending(self, consumer: str | None = None) -> list[dict[str, Any]]:
        """미처리 이벤트 목록 (payload는 dict로 역직렬화)."""
        db = self._require_db()
        if consumer is None:
            sql = (
                "SELECT id, event_type, payload, created_at, consumer "
                "FROM event_checkpoint WHERE consumed_at IS NULL "
                "ORDER BY id ASC"
            )
            params: tuple[Any, ...] = ()
        else:
            sql = (
                "SELECT id, event_type, payload, created_at, consumer "
                "FROM event_checkpoint WHERE consumed_at IS NULL AND consumer = ? "
                "ORDER BY id ASC"
            )
            params = (consumer,)
        rows = await db.execute_fetchall(sql, params)
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
                "consumer": row["consumer"],
            }
            for row in rows
        ]

    async def replay(
        self, consumer: str
    ) -> AsyncIterator[tuple[int, object]]:
        """미처리 이벤트를 재구성된 dataclass 인스턴스로 yield."""
        pending = await self.pending(consumer)
        for row in pending:
            cls = _resolve_event_class(row["event_type"])
            event = cls(**row["payload"])
            yield row["id"], event

    async def purge_consumed(self, older_than_sec: float = 86400) -> int:
        """일정 시간 이상 된 완료 이벤트 정리. 삭제된 행 수 반환."""
        db = self._require_db()
        threshold = time.time() - older_than_sec
        cursor = await db.execute(
            "DELETE FROM event_checkpoint "
            "WHERE consumed_at IS NOT NULL AND consumed_at < ?",
            (threshold,),
        )
        await db.commit()
        deleted = cursor.rowcount or 0
        await cursor.close()
        return int(deleted)


__all__ = ["CheckpointStore"]
