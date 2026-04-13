"""alert_outcome — Phase 4 축 8 피드백 루프 (스키마/helper 단계).

설계 메모: docs/phase4_design.md

목적:
    알림 발생 후 N분 내 실제 체결 여부를 추적해서, 어떤 조건의 알림이
    실제 거래로 이어지는지 측정 → 점수 가중치 자동 튜닝의 기반.

이번 단계 (Phase 3 말):
    - 스키마는 이미 alert_followup 테이블로 존재 (src/models/database.py)
    - 본 모듈은 writer/reader/sweep helper 만 — 실제 sweep loop 와
      가중치 튜닝은 Phase 4 진입 시 alert-outcome-tracker 에이전트가
      이 helper 위에 빌드한다.
    - 외부 API 호출 0 — 순수 DB I/O.

테스트:
    tests/test_alert_outcome.py — sqlite 메모리/임시 DB 로 검증.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FollowupRecord:
    """alert_followup 1행 — 외부 노출용 dto."""

    id: int
    alert_id: int | None
    fired_at: float
    kream_product_id: str
    size: str
    retail_price: int
    kream_sell_price_at_fire: int
    checked_at: float | None
    actual_sold: int
    actual_price: int | None


# ─── writer ──────────────────────────────────────────────


async def record_alert(
    db_path: str,
    *,
    alert_id: int | None,
    kream_product_id: str | int,
    size: str,
    retail_price: int,
    kream_sell_price_at_fire: int,
    fired_at: float | None = None,
) -> int:
    """알림 발생 시 followup row INSERT.

    Returns
    -------
    int
        새로 생성된 followup row id.
    """
    fired = fired_at if fired_at is not None else time.time()
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        cur = await conn.execute(
            """INSERT INTO alert_followup (
                alert_id, fired_at, kream_product_id, size,
                retail_price, kream_sell_price_at_fire,
                checked_at, actual_sold, actual_price
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL)""",
            (
                alert_id,
                fired,
                str(kream_product_id),
                size,
                int(retail_price),
                int(kream_sell_price_at_fire),
            ),
        )
        await conn.commit()
        return cur.lastrowid or 0


# ─── reader ──────────────────────────────────────────────


async def pending_followups(
    db_path: str, *, older_than_sec: int = 1800, limit: int = 100
) -> list[FollowupRecord]:
    """체크 안 된 followup 중 fire 후 N초 경과한 것만 (sweep 후보).

    Parameters
    ----------
    older_than_sec: 알림 발생 후 최소 N초 (기본 30분)
    limit: 1회 최대 행수
    """
    cutoff = time.time() - older_than_sec
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT * FROM alert_followup
               WHERE checked_at IS NULL
                 AND fired_at <= ?
               ORDER BY fired_at ASC
               LIMIT ?""",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
    return [_row_to_record(r) for r in rows]


async def get_followup(db_path: str, followup_id: int) -> FollowupRecord | None:
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM alert_followup WHERE id = ?", (followup_id,)
        )
        row = await cur.fetchone()
    return _row_to_record(row) if row else None


def _row_to_record(row: aiosqlite.Row) -> FollowupRecord:
    return FollowupRecord(
        id=row["id"],
        alert_id=row["alert_id"],
        fired_at=float(row["fired_at"] or 0),
        kream_product_id=str(row["kream_product_id"] or ""),
        size=str(row["size"] or ""),
        retail_price=int(row["retail_price"] or 0),
        kream_sell_price_at_fire=int(row["kream_sell_price_at_fire"] or 0),
        checked_at=(float(row["checked_at"]) if row["checked_at"] else None),
        actual_sold=int(row["actual_sold"] or 0),
        actual_price=(int(row["actual_price"]) if row["actual_price"] else None),
    )


# ─── outcome 마킹 ──────────────────────────────────────────


async def mark_checked(
    db_path: str,
    followup_id: int,
    *,
    actual_sold: bool,
    actual_price: int | None = None,
    checked_at: float | None = None,
) -> None:
    """sweep 결과 기록 — 체결 여부 + 체결가 (있으면)."""
    ts = checked_at if checked_at is not None else time.time()
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        await conn.execute(
            """UPDATE alert_followup SET
                  checked_at = ?,
                  actual_sold = ?,
                  actual_price = ?
               WHERE id = ?""",
            (ts, 1 if actual_sold else 0, actual_price, followup_id),
        )
        await conn.commit()


# ─── 집계 (Phase 4 가중치 튜닝의 입력) ─────────────────────


async def hit_rate_summary(
    db_path: str, *, hours: int = 168
) -> dict[str, Any]:
    """7일(기본) 윈도우 적중률 집계.

    Returns
    -------
    {
        "window_hours": int,
        "total": int,
        "checked": int,
        "sold": int,
        "hit_rate_pct": float | None,  # checked 중 sold 비율
        "avg_realized_gap_pct": float | None,
            # 체결가가 sell_now_at_fire 대비 얼마나 빠졌는지
    }
    """
    cutoff = time.time() - hours * 3600
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = aiosqlite.Row
        total = (
            await (
                await conn.execute(
                    "SELECT COUNT(*) AS c FROM alert_followup WHERE fired_at >= ?",
                    (cutoff,),
                )
            ).fetchone()
        )["c"]
        checked = (
            await (
                await conn.execute(
                    "SELECT COUNT(*) AS c FROM alert_followup "
                    "WHERE fired_at >= ? AND checked_at IS NOT NULL",
                    (cutoff,),
                )
            ).fetchone()
        )["c"]
        sold_rows = await (
            await conn.execute(
                """SELECT kream_sell_price_at_fire AS fire, actual_price AS got
                   FROM alert_followup
                   WHERE fired_at >= ? AND actual_sold = 1
                     AND actual_price IS NOT NULL""",
                (cutoff,),
            )
        ).fetchall()

    sold = len(sold_rows)
    gaps: list[float] = []
    for r in sold_rows:
        fire = int(r["fire"] or 0)
        got = int(r["got"] or 0)
        if fire > 0 and got > 0:
            gaps.append((got - fire) / fire * 100.0)

    return {
        "window_hours": hours,
        "total": total,
        "checked": checked,
        "sold": sold,
        "hit_rate_pct": round(sold / checked * 100, 2) if checked else None,
        "avg_realized_gap_pct": (
            round(sum(gaps) / len(gaps), 2) if gaps else None
        ),
    }


# ─── sweep 헬퍼 (Phase 4 진입 시 검사 함수 주입) ───────────


CheckFn = Callable[[FollowupRecord], Awaitable[tuple[bool, int | None]]]
"""(record) → (sold_bool, actual_price)."""


async def sweep_pending(
    db_path: str,
    check_fn: CheckFn,
    *,
    older_than_sec: int = 1800,
    limit: int = 100,
) -> dict[str, int]:
    """미체크 followup 을 일괄 처리.

    check_fn 은 외부 검사기 (Phase 4 에서 크림 거래내역 API 어댑터로 주입).
    개별 예외는 격리 — 한 행 실패가 sweep 전체를 막지 않는다.

    Returns
    -------
    {"scanned": int, "sold": int, "not_sold": int, "errors": int}
    """
    pending = await pending_followups(
        db_path, older_than_sec=older_than_sec, limit=limit
    )
    sold = 0
    not_sold = 0
    errors = 0
    for rec in pending:
        try:
            is_sold, price = await check_fn(rec)
        except Exception:
            logger.exception("[alert_outcome] check_fn 예외 id=%s", rec.id)
            errors += 1
            continue
        try:
            await mark_checked(
                db_path, rec.id, actual_sold=is_sold, actual_price=price
            )
        except Exception:
            logger.exception("[alert_outcome] mark_checked 실패 id=%s", rec.id)
            errors += 1
            continue
        if is_sold:
            sold += 1
        else:
            not_sold += 1
    return {
        "scanned": len(pending),
        "sold": sold,
        "not_sold": not_sold,
        "errors": errors,
    }


__all__ = [
    "CheckFn",
    "FollowupRecord",
    "get_followup",
    "hit_rate_summary",
    "mark_checked",
    "pending_followups",
    "record_alert",
    "sweep_pending",
]
