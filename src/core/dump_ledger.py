"""dump_ledger — 소싱처 카탈로그 덤프 per-item 기록.

목적:
    retail_products 는 "크림 매칭 성공" 한 것만 저장함 (orchestrator 설계).
    이 모듈은 매칭 실패/PDP 드롭 포함 **전수 덤프 아이템**을 별도로 기록해서
    오프라인 분석 가능하게 한다 (왜 못 잡았나 → 크롤러/매칭 엔진 수정 근거).

설계:
    - 테이블: catalog_dump_items(source, model_no, name, url, first_seen_at,
              last_seen_at, seen_count, UNIQUE(source, model_no))
    - upsert 는 first_seen_at 는 유지, last_seen_at 은 now, seen_count +=1
    - 호출자: 각 어댑터 run_once() 루프 (매칭 시도 직전, is_sold_out/ 발매예정
      필터 통과 후)

스키마만 담당 — 분석 쿼리는 별도 report 모듈.
외부 API 호출 0, 순수 DB I/O.
"""

from __future__ import annotations

import logging
import time

import aiosqlite

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_dump_items (
    source TEXT NOT NULL,
    model_no TEXT NOT NULL,
    name TEXT DEFAULT '',
    url TEXT DEFAULT '',
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, model_no)
);
CREATE INDEX IF NOT EXISTS idx_dump_items_source_last
    ON catalog_dump_items(source, last_seen_at);
"""


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA)


async def record_dump_item(
    db_path: str,
    *,
    source: str,
    model_no: str,
    name: str = "",
    url: str = "",
) -> None:
    """덤프된 1건을 ledger 에 upsert. 외부 노이즈(예외)는 흡수."""
    if not source or not model_no:
        return
    now = time.time()
    try:
        async with aiosqlite.connect(db_path, timeout=30.0) as conn:
            await _ensure_schema(conn)
            await conn.execute(
                """INSERT INTO catalog_dump_items
                    (source, model_no, name, url, first_seen_at, last_seen_at, seen_count)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(source, model_no) DO UPDATE SET
                       last_seen_at = excluded.last_seen_at,
                       name = CASE WHEN length(excluded.name) > 0 THEN excluded.name ELSE catalog_dump_items.name END,
                       url  = CASE WHEN length(excluded.url)  > 0 THEN excluded.url  ELSE catalog_dump_items.url  END,
                       seen_count = catalog_dump_items.seen_count + 1""",
                (source, model_no, name, url, now, now),
            )
            await conn.commit()
    except Exception:
        logger.exception(
            "[dump_ledger] record_dump_item 실패 source=%s model=%s",
            source,
            model_no,
        )


async def unmatched_sample(
    db_path: str,
    source: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """해당 source 의 아이템 중 retail_products 에 없는 (=매칭 실패) 상위 limit."""
    try:
        async with aiosqlite.connect(db_path, timeout=30.0) as conn:
            await _ensure_schema(conn)
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """SELECT d.model_no, d.name, d.url, d.last_seen_at, d.seen_count
                   FROM catalog_dump_items d
                   LEFT JOIN retail_products r
                     ON r.source = d.source AND r.model_number = d.model_no
                   WHERE d.source = ? AND r.id IS NULL
                   ORDER BY d.seen_count DESC, d.last_seen_at DESC
                   LIMIT ?""",
                (source, limit),
            ) as cur:
                rows = await cur.fetchall()
    except Exception:
        logger.exception("[dump_ledger] unmatched_sample 실패 source=%s", source)
        return []
    return [dict(r) for r in rows]


async def match_rate(db_path: str, source: str) -> dict:
    """해당 source 의 덤프 vs 매칭 비율."""
    try:
        async with aiosqlite.connect(db_path, timeout=30.0) as conn:
            await _ensure_schema(conn)
            async with conn.execute(
                "SELECT COUNT(*) FROM catalog_dump_items WHERE source = ?",
                (source,),
            ) as cur:
                dumped = int((await cur.fetchone())[0] or 0)
            async with conn.execute(
                """SELECT COUNT(*) FROM catalog_dump_items d
                   INNER JOIN retail_products r
                     ON r.source = d.source AND r.model_number = d.model_no
                   WHERE d.source = ?""",
                (source,),
            ) as cur:
                matched = int((await cur.fetchone())[0] or 0)
    except Exception:
        logger.exception("[dump_ledger] match_rate 실패 source=%s", source)
        return {"source": source, "dumped": 0, "matched": 0, "match_rate_pct": None}
    rate = (matched / dumped * 100.0) if dumped else None
    return {
        "source": source,
        "dumped": dumped,
        "matched": matched,
        "match_rate_pct": rate,
    }


__all__ = ["match_rate", "record_dump_item", "unmatched_sample"]
