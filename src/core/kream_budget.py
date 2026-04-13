"""크림 API 호출 일일 예산 가드.

Phase 0 보안 레이어 — 실계정 보호를 위한 하드 캡.
- `kream_api_calls` 테이블에 모든 호출 기록 (kream.py _request wrapper가 주입)
- 요청 전 24h 누적 호출 수 검사
- 90% 도달 → 경고 로그 (Discord 알림은 scheduler에서 훅)
- 100% 도달 → KreamBudgetExceeded 예외 → 파이프라인 자동 중단

사용:
    from src.core.kream_budget import check_budget, record_call, BUDGET

    async def _request(...):
        await check_budget()            # 호출 전
        status, latency = do_request()
        await record_call(endpoint, method, status, latency, purpose)
"""

from __future__ import annotations

import os
from datetime import datetime

import aiosqlite

from src.config import settings
from src.utils.logging import setup_logger

logger = setup_logger("kream_budget")

BUDGET: int = int(os.getenv("KREAM_DAILY_CAP", "10000"))
WARN_RATIO: float = 0.9

_warned_today: str | None = None


class KreamBudgetExceeded(RuntimeError):
    """일일 크림 호출 캡 초과."""


async def _count_last_24h() -> int:
    async with aiosqlite.connect(settings.db_path, timeout=30.0) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM kream_api_calls WHERE ts >= datetime('now','-1 day')"
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def check_budget() -> None:
    """호출 전 체크. 100% 초과 시 예외."""
    global _warned_today
    used = await _count_last_24h()
    if used >= BUDGET:
        logger.critical("KREAM 일일 캡 초과: %d/%d — 파이프라인 정지", used, BUDGET)
        raise KreamBudgetExceeded(f"{used}/{BUDGET} calls in last 24h")

    if used >= int(BUDGET * WARN_RATIO):
        today = datetime.now().strftime("%Y-%m-%d")
        if _warned_today != today:
            logger.warning("KREAM 일일 캡 90%% 도달: %d/%d", used, BUDGET)
            _warned_today = today


async def record_call(
    endpoint: str,
    method: str,
    status: int | None,
    latency_ms: int,
    purpose: str = "manual",
) -> None:
    """호출 기록 (비동기, 실패 무시 — 계측 실패로 서비스 막지 않음)."""
    try:
        async with aiosqlite.connect(settings.db_path, timeout=30.0) as db:
            await db.execute(
                """
                INSERT INTO kream_api_calls(endpoint, method, status, latency_ms, purpose)
                VALUES (?, ?, ?, ?, ?)
                """,
                (endpoint, method, status, latency_ms, purpose),
            )
            await db.commit()
    except Exception as exc:
        logger.debug("kream_api_calls 기록 실패: %s", exc)


async def get_usage() -> dict:
    """현재 사용량 요약 (대시보드/상태 명령에서 사용)."""
    used = await _count_last_24h()
    return {
        "used": used,
        "cap": BUDGET,
        "remaining": max(0, BUDGET - used),
        "ratio": round(used / BUDGET, 3) if BUDGET else 0,
    }
