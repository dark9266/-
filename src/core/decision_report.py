"""decision_report — Decision Ledger 드롭 사유 분포 집계.

orchestrator 가 파이프라인 각 단계에서 pass/block 결정을 decision_log 테이블에
기록함. 이 모듈은 최근 윈도우의 기록을 사유별로 집계해 대시보드에 공급한다.

용도:
    - 어떤 필터가 과도하게 차단하고 있는지 (= 튜닝 여지)
    - 어떤 단계에서 병목이 생기는지 (throttle vs floor 등)
    - 통과/차단 비율 추이
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReasonBucket:
    stage: str
    decision: str  # "pass" | "block"
    reason: str
    count: int


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    window_hours: float
    total: int
    by_stage: dict[str, dict[str, int]]  # {stage: {"pass": n, "block": n}}
    top_drops: list[ReasonBucket]        # 상위 drop 사유 (block 만)
    top_passes: list[ReasonBucket]       # 상위 pass 사유


async def decision_summary(
    db_path: str,
    *,
    hours: float = 24.0,
    top_n: int = 10,
) -> DecisionSummary:
    """최근 hours 시간 동안의 decision_log 집계."""
    since = time.time() - hours * 3600.0
    try:
        async with aiosqlite.connect(db_path, timeout=30.0) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """SELECT stage, decision, reason, COUNT(*) AS c
                   FROM decision_log
                   WHERE ts >= ?
                   GROUP BY stage, decision, reason
                   ORDER BY c DESC""",
                (since,),
            ) as cur:
                rows = await cur.fetchall()
    except Exception:
        logger.exception("[decision_report] summary 쿼리 실패")
        return DecisionSummary(
            window_hours=hours, total=0, by_stage={}, top_drops=[], top_passes=[],
        )

    by_stage: dict[str, dict[str, int]] = {}
    all_buckets: list[ReasonBucket] = []
    total = 0
    for row in rows:
        stage = row["stage"]
        decision = row["decision"]
        reason = row["reason"]
        c = int(row["c"])
        total += c
        by_stage.setdefault(stage, {"pass": 0, "block": 0})
        by_stage[stage][decision] = by_stage[stage].get(decision, 0) + c
        all_buckets.append(ReasonBucket(stage, decision, reason, c))

    top_drops = [b for b in all_buckets if b.decision == "block"][:top_n]
    top_passes = [b for b in all_buckets if b.decision == "pass"][:top_n]
    return DecisionSummary(
        window_hours=hours,
        total=total,
        by_stage=by_stage,
        top_drops=top_drops,
        top_passes=top_passes,
    )


__all__ = ["DecisionSummary", "ReasonBucket", "decision_summary"]
