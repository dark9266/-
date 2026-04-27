"""Chrome 확장 catch row 저장소.

매칭 키: (sourcing, native_id, color_code, size_code).
색·사이즈 일치 검증은 호출자(profit_calculator)가 담당.
페이지 우선순위: checkout > pdp (size_code='NONE' 폴백).
"""

import json
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from src.config import settings


@dataclass
class CatchPayload:
    sourcing: str
    native_id: str
    color_code: str
    size_code: str
    page_type: str  # 'pdp' | 'checkout'
    payload: dict[str, Any]
    captured_at: float


def _resolve_db(db_path: str | None) -> str:
    return db_path if db_path is not None else settings.db_path


async def save_catch(p: CatchPayload, db_path: str | None = None) -> int:
    """행 저장. 동일 키 충돌 시 payload + captured_at 갱신 (upsert).

    Returns:
        SQLite ``lastrowid`` — 신규 INSERT 시 새 rowid, upsert UPDATE 시
        해당 connection의 마지막 INSERT rowid (대상 row id 보장 X).
        호출자는 단순 성공 신호로만 사용하고, 안정적 row 식별이 필요하면
        (sourcing, native_id, color_code, size_code, page_type) 키로
        재조회할 것.
    """
    async with aiosqlite.connect(_resolve_db(db_path)) as conn:
        cur = await conn.execute(
            """
            INSERT INTO coupon_catches
                (sourcing, native_id, color_code, size_code, page_type, payload, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sourcing, native_id, color_code, size_code, page_type)
            DO UPDATE SET
                payload     = excluded.payload,
                captured_at = excluded.captured_at
            """,
            (
                p.sourcing,
                p.native_id,
                p.color_code,
                p.size_code,
                p.page_type,
                json.dumps(p.payload, ensure_ascii=False),
                p.captured_at,
            ),
        )
        await conn.commit()
        return cur.lastrowid or 0


async def lookup_catch(
    sourcing: str,
    native_id: str,
    color_code: str,
    size_code: str,
    db_path: str | None = None,
) -> CatchPayload | None:
    """checkout 우선, 없으면 동일 color의 PDP(size_code='NONE') 폴백."""
    async with aiosqlite.connect(_resolve_db(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT * FROM coupon_catches
            WHERE sourcing = ? AND native_id = ? AND color_code = ?
              AND (size_code = ? OR (size_code = 'NONE' AND page_type = 'pdp'))
            ORDER BY
                CASE page_type WHEN 'checkout' THEN 0 ELSE 1 END,
                captured_at DESC
            LIMIT 1
            """,
            (sourcing, native_id, color_code, size_code),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return CatchPayload(
            sourcing=row["sourcing"],
            native_id=row["native_id"],
            color_code=row["color_code"],
            size_code=row["size_code"],
            page_type=row["page_type"],
            payload=json.loads(row["payload"]),
            captured_at=row["captured_at"],
        )


async def expire_old_catches(ttl_days: int = 3, db_path: str | None = None) -> int:
    """captured_at 기준 ttl_days 초과 행 삭제.

    Returns:
        삭제된 행 수
    """
    cutoff = time.time() - ttl_days * 86400
    async with aiosqlite.connect(_resolve_db(db_path)) as conn:
        cur = await conn.execute(
            "DELETE FROM coupon_catches WHERE captured_at < ?",
            (cutoff,),
        )
        await conn.commit()
        return cur.rowcount
