"""adapter_heartbeat — 어댑터별 candidate emit 기록 + silent 감지.

목적:
    27개(활성 19개) 어댑터 중 어느 하나가 HTML/API 변경으로 후보를 전혀 뽑지
    못하게 되는 경우, 지금은 조용히 무시됨. 이 모듈은 어댑터별 마지막 emit
    시각과 누적 카운트를 기록해두고, 임계 시간 이상 emit 0 이면 경보를 띄운다.

설계:
    - 테이블: adapter_heartbeat(source TEXT PK, last_emit_at REAL, emit_count INT)
    - 기록: `bump(db_path, source)` — orchestrator._process_candidate 진입 시
    - 조회: `silent_adapters(db_path, expected, threshold_hours)`
        * expected 중에서 한 번도 emit 못 한 어댑터 (missing)
        * 또는 last_emit_at 이 threshold_hours 이상 지난 어댑터 (stale)

외부 API 0 — 순수 DB I/O.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

from src.core.db import async_connect

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SilentAdapter:
    source: str
    last_emit_at: float | None  # None → 한 번도 emit 못 함
    emit_count: int
    hours_since_last: float | None

    @property
    def reason(self) -> str:
        return "never_emitted" if self.last_emit_at is None else "stale"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS adapter_heartbeat (
    source TEXT PRIMARY KEY,
    last_emit_at REAL NOT NULL,
    emit_count INTEGER NOT NULL DEFAULT 0
)
"""


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute(_SCHEMA)


async def bump(db_path: str, source: str) -> None:
    """source 의 heartbeat 를 갱신 (last_emit_at=now, count+=1)."""
    if not source:
        return
    now = time.time()
    try:
        async with async_connect(db_path) as conn:
            await _ensure_schema(conn)
            await conn.execute(
                """INSERT INTO adapter_heartbeat (source, last_emit_at, emit_count)
                   VALUES (?, ?, 1)
                   ON CONFLICT(source) DO UPDATE SET
                       last_emit_at = excluded.last_emit_at,
                       emit_count = adapter_heartbeat.emit_count + 1""",
                (source, now),
            )
            await conn.commit()
    except Exception:
        logger.exception("[heartbeat] bump 실패 source=%s", source)


async def snapshot(db_path: str) -> dict[str, tuple[float, int]]:
    """모든 source 의 (last_emit_at, emit_count) 맵."""
    try:
        async with async_connect(db_path) as conn:
            await _ensure_schema(conn)
            async with conn.execute(
                "SELECT source, last_emit_at, emit_count FROM adapter_heartbeat"
            ) as cur:
                rows = await cur.fetchall()
    except Exception:
        logger.exception("[heartbeat] snapshot 실패")
        return {}
    return {
        row["source"]: (float(row["last_emit_at"]), int(row["emit_count"]))
        for row in rows
    }


async def silent_adapters(
    db_path: str,
    expected: list[str] | tuple[str, ...],
    *,
    threshold_hours: float = 168.0,
) -> list[SilentAdapter]:
    """expected 중 한 번도 emit 못 했거나 threshold_hours 이상 emit 없는 목록."""
    snap = await snapshot(db_path)
    now = time.time()
    threshold_sec = threshold_hours * 3600.0
    out: list[SilentAdapter] = []
    for source in expected:
        if source not in snap:
            out.append(SilentAdapter(source=source, last_emit_at=None, emit_count=0, hours_since_last=None))
            continue
        last_at, count = snap[source]
        delta = now - last_at
        if delta >= threshold_sec:
            out.append(
                SilentAdapter(
                    source=source,
                    last_emit_at=last_at,
                    emit_count=count,
                    hours_since_last=delta / 3600.0,
                )
            )
    return out


__all__ = ["SilentAdapter", "bump", "silent_adapters", "snapshot"]
