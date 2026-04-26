"""체크포인트 스토어 — 이벤트 영속화 + 재시작 복구 (Phase 2.3a).

event_bus 런타임이 강제 종료되거나 크래시될 때 처리 중이던
이벤트를 잃지 않도록 SQLite에 영속화한다.

상태 모델 (status 컬럼):
    - pending   : 기록 직후. 아직 처리 안 됨
    - consumed  : 정상 처리 완료 (mark_consumed)
    - deferred  : throttle 등으로 일시 거부됨. recover 때 재시도 대상
    - failed    : attempts 초과 / 알 수 없는 event_type / TTL 초과 등 영구 실패

흐름:
    1) 컨슈머가 이벤트 수신 직후 `record(event, consumer)` → checkpoint_id
    2) 처리 성공 후 `mark_consumed(checkpoint_id)`
    3) throttle 거부 시 `mark_deferred(checkpoint_id, reason)` (재시도 여지)
    4) 재시작 시 `replay(consumer)` 로 pending+deferred 재구성 → attempts++
    5) attempts 3회 초과 시 자동으로 `status='failed'` + ERROR 로깅
    6) 알 수 없는 event_type 은 해당 row 만 skip → `status='failed'`, 나머지 계속

event_bus 모듈의 frozen dataclass 5종 (`CatalogDumped`, `CandidateMatched`,
`ProfitFound`, `AlertSent`, `AlertFollowup`) 을 dataclasses.asdict 로
JSON 직렬화하고, 복원 시 `event_type` 문자열로 클래스를 찾아 `Cls(**payload)`.

주의: 현재 이벤트들은 모두 평탄한 dataclass. 장래 nested dataclass를 도입할 경우
`asdict` 는 내부 dataclass 도 dict 로 만들어버리므로 `Cls(**payload)` 만으로는
복원되지 않는다 — nested 복원 로직 추가 필요.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator
from typing import Any

import aiosqlite

from src.core import event_bus as _event_bus_module
from src.core.db import apply_write_pragmas

logger = logging.getLogger(__name__)

MAX_REPLAY_ATTEMPTS = 3

STATUS_PENDING = "pending"
STATUS_CONSUMED = "consumed"
STATUS_DEFERRED = "deferred"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_checkpoint (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    consumed_at REAL,
    consumer TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ckpt_pending
    ON event_checkpoint(consumer, status);
CREATE INDEX IF NOT EXISTS idx_event_ckpt_created
    ON event_checkpoint(created_at);
"""


async def _ensure_columns(db: aiosqlite.Connection) -> None:
    """기존 DB 호환: 누락된 컬럼을 ALTER TABLE ADD COLUMN 으로 보강."""
    cur = await db.execute("PRAGMA table_info(event_checkpoint)")
    cols = {row[1] for row in await cur.fetchall()}
    await cur.close()
    if "status" not in cols:
        await db.execute(
            "ALTER TABLE event_checkpoint "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
        )
        # 기존 행 중 consumed_at 있는 건 consumed 로 마이그레이션
        await db.execute(
            "UPDATE event_checkpoint SET status='consumed' "
            "WHERE consumed_at IS NOT NULL"
        )
    if "attempts" not in cols:
        await db.execute(
            "ALTER TABLE event_checkpoint "
            "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "last_reason" not in cols:
        await db.execute(
            "ALTER TABLE event_checkpoint ADD COLUMN last_reason TEXT"
        )
    await db.commit()


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
        """DB 연결 및 테이블 생성 (기존 DB 호환 ALTER 포함)."""
        if self._db is not None:
            return
        # isolation_level=None (autocommit) — 2026-04-18 WAL 재발 대응.
        # SELECT snapshot 누적으로 WAL checkpoint 영구 차단되던 문제 해소.
        self._db = await aiosqlite.connect(
            self.db_path, timeout=30.0, isolation_level=None
        )
        self._db.row_factory = aiosqlite.Row
        # WAL 경합 방어 PRAGMA 4종 (src/core/db.py로 통합). 2026-04-18 incident 대응.
        await apply_write_pragmas(self._db)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await _ensure_columns(self._db)

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
        """이벤트를 체크포인트 테이블에 저장 (status=pending) 후 id 반환.

        SQLite lock contention 시 5회 exponential backoff retry
        (2026-04-26 진단: candidate handler 의 'database is locked' 다발).
        """
        if not dataclasses.is_dataclass(event) or isinstance(event, type):
            raise TypeError(
                f"record requires a dataclass instance, got {type(event).__name__}"
            )
        db = self._require_db()
        event_type = type(event).__name__
        payload = json.dumps(dataclasses.asdict(event), ensure_ascii=False)
        created_at = time.time()
        last_err: Exception | None = None
        for delay in (0.0, 0.05, 0.15, 0.4, 1.0):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                cursor = await db.execute(
                    "INSERT INTO event_checkpoint "
                    "(event_type, payload, created_at, consumed_at, consumer, "
                    " status, attempts, last_reason) "
                    "VALUES (?, ?, ?, NULL, ?, 'pending', 0, NULL)",
                    (event_type, payload, created_at, consumer),
                )
                await db.commit()
                checkpoint_id = cursor.lastrowid
                await cursor.close()
                assert checkpoint_id is not None
                return int(checkpoint_id)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                last_err = e
        raise last_err  # type: ignore[misc]

    async def mark_consumed(self, checkpoint_id: int) -> None:
        """해당 체크포인트를 정상 처리 완료로 표시."""
        db = self._require_db()
        last_err: Exception | None = None
        for delay in (0.0, 0.05, 0.15, 0.4, 1.0):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await db.execute(
                    "UPDATE event_checkpoint "
                    "SET consumed_at = ?, status = 'consumed' "
                    "WHERE id = ?",
                    (time.time(), checkpoint_id),
                )
                await db.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                last_err = e
        raise last_err  # type: ignore[misc]

    async def mark_deferred(self, checkpoint_id: int, reason: str) -> None:
        """일시 거부 — recover 때 재시도 대상으로 남긴다."""
        db = self._require_db()
        last_err: Exception | None = None
        for delay in (0.0, 0.05, 0.15, 0.4, 1.0):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await db.execute(
                    "UPDATE event_checkpoint "
                    "SET status = 'deferred', last_reason = ? "
                    "WHERE id = ?",
                    (reason, checkpoint_id),
                )
                await db.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                last_err = e
        raise last_err  # type: ignore[misc]

    async def mark_failed(self, checkpoint_id: int, reason: str) -> None:
        """영구 실패로 표시."""
        db = self._require_db()
        await db.execute(
            "UPDATE event_checkpoint "
            "SET status = 'failed', last_reason = ? "
            "WHERE id = ?",
            (reason, checkpoint_id),
        )
        await db.commit()

    async def increment_attempts(self, checkpoint_id: int) -> int:
        """attempts += 1 후 새 값 반환."""
        db = self._require_db()
        await db.execute(
            "UPDATE event_checkpoint SET attempts = attempts + 1 WHERE id = ?",
            (checkpoint_id,),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT attempts FROM event_checkpoint WHERE id = ?",
            (checkpoint_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row["attempts"]) if row else 0

    async def pending(self, consumer: str | None = None) -> list[dict[str, Any]]:
        """미처리 이벤트 목록 (pending + deferred 포함).

        payload 는 dict 로 역직렬화. status/attempts 도 포함.
        """
        db = self._require_db()
        if consumer is None:
            sql = (
                "SELECT id, event_type, payload, created_at, consumer, "
                "status, attempts, last_reason "
                "FROM event_checkpoint "
                "WHERE status IN ('pending','deferred') "
                "ORDER BY id ASC"
            )
            params: tuple[Any, ...] = ()
        else:
            sql = (
                "SELECT id, event_type, payload, created_at, consumer, "
                "status, attempts, last_reason "
                "FROM event_checkpoint "
                "WHERE status IN ('pending','deferred') AND consumer = ? "
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
                "status": row["status"],
                "attempts": row["attempts"],
                "last_reason": row["last_reason"],
            }
            for row in rows
        ]

    async def replay(
        self, consumer: str
    ) -> AsyncIterator[tuple[int, object]]:
        """미처리 이벤트를 재구성된 dataclass 인스턴스로 yield.

        - 알 수 없는 event_type 은 해당 row 만 `status='failed'` 처리 후 skip
          (나머지는 계속 yield)
        - attempts 가 이미 MAX_REPLAY_ATTEMPTS 이상인 row 는 failed 처리 후 skip
        """
        pending = await self.pending(consumer)
        for row in pending:
            ckpt_id = int(row["id"])
            if int(row["attempts"]) >= MAX_REPLAY_ATTEMPTS:
                logger.error(
                    "checkpoint attempts 초과 — failed 처리: id=%s type=%s attempts=%s",
                    ckpt_id,
                    row["event_type"],
                    row["attempts"],
                )
                await self.mark_failed(ckpt_id, "attempts_exceeded")
                continue
            try:
                cls = _resolve_event_class(row["event_type"])
                payload = row["payload"]
                # JSON 라운드트립은 tuple → list 로 되돌아오므로 dataclass
                # 필드 정의와 일치하도록 tuple 기본값 필드를 복원한다.
                # (e.g. CandidateMatched.available_sizes, ProfitFound.size_profits)
                import dataclasses as _dc
                if _dc.is_dataclass(cls):
                    for f in _dc.fields(cls):
                        if (
                            f.name in payload
                            and isinstance(payload[f.name], list)
                            and isinstance(f.default, tuple)
                        ):
                            payload[f.name] = tuple(payload[f.name])
                event = cls(**payload)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "checkpoint replay 복원 실패 — failed 처리: id=%s type=%s err=%s",
                    ckpt_id,
                    row["event_type"],
                    exc,
                )
                await self.mark_failed(ckpt_id, f"resolve:{exc}")
                continue
            yield ckpt_id, event

    async def purge_consumed(
        self,
        older_than_sec: float = 86400,
        pending_ttl_sec: float | None = None,
    ) -> dict[str, int]:
        """오래된 완료/pending 정리.

        - consumed: `older_than_sec` 지난 row 는 DELETE
        - pending_ttl_sec 지정 시: 그보다 오래된 pending/deferred 는 `failed` 로
          전환 + WARN 로깅. 이 건은 삭제하지 않고 보존(감사용).

        Returns
        -------
        dict
            {"deleted": n, "failed_stale": m}
        """
        db = self._require_db()
        now = time.time()
        threshold = now - older_than_sec
        cursor = await db.execute(
            "DELETE FROM event_checkpoint "
            "WHERE status = 'consumed' AND consumed_at IS NOT NULL "
            "AND consumed_at < ?",
            (threshold,),
        )
        deleted = cursor.rowcount or 0
        await cursor.close()

        failed_stale = 0
        if pending_ttl_sec is not None:
            stale_threshold = now - pending_ttl_sec
            cur = await db.execute(
                "SELECT id, event_type, consumer, created_at "
                "FROM event_checkpoint "
                "WHERE status IN ('pending','deferred') AND created_at < ?",
                (stale_threshold,),
            )
            stale_rows = await cur.fetchall()
            await cur.close()
            for r in stale_rows:
                logger.warning(
                    "pending TTL 초과 — failed 처리: id=%s type=%s consumer=%s age=%.1fs",
                    r["id"],
                    r["event_type"],
                    r["consumer"],
                    now - float(r["created_at"]),
                )
                await db.execute(
                    "UPDATE event_checkpoint "
                    "SET status = 'failed', last_reason = 'pending_ttl_exceeded' "
                    "WHERE id = ?",
                    (r["id"],),
                )
                failed_stale += 1

        await db.commit()
        return {"deleted": int(deleted), "failed_stale": int(failed_stale)}


__all__ = [
    "MAX_REPLAY_ATTEMPTS",
    "STATUS_CONSUMED",
    "STATUS_DEFERRED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "CheckpointStore",
]
