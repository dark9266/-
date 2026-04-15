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

import contextvars
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import aiosqlite

from src.config import settings
from src.utils.logging import setup_logger

logger = setup_logger("kream_budget")

BUDGET: int = int(os.getenv("KREAM_DAILY_CAP", "10000"))
WARN_RATIO: float = 0.9

# 캡 초과 캐시 — DB 재조회/로그 스팸 동시 억제.
# check_budget 은 각 kream 호출 전 호출되므로, 초과 상태에서 루프가 10초마다
# 재진입하면 기존에는 매번 DB COUNT + CRITICAL 로그가 찍혀 kream_budget.log
# 가 1,700+ 줄로 불어났다. 캐시 TTL 동안은 쿼리 없이 즉시 예외.
_EXCEEDED_CACHE_TTL_SEC: float = 30.0
_LOG_THROTTLE_SEC: float = 60.0
_exceeded_until: float = 0.0
_last_exceeded_log_at: float = 0.0
_warned_today: str | None = None

# 호출 원점(워커/루프) 태그. 각 스케줄 루프가 kream 호출 직전에
# `with kream_purpose("v3_delta"):` 로 감싸면 _request 가 이 값을
# 자동으로 picking up 하여 kream_api_calls.purpose 컬럼에 기록한다.
# 기본값 "manual" 은 사용자 명령/임시 호출을 의미.
_purpose_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kream_call_purpose", default="manual"
)


def current_purpose() -> str:
    """현재 컨텍스트의 호출 원점 태그. _request 기본값 분기에 사용."""
    return _purpose_var.get()


@contextmanager
def kream_purpose(tag: str) -> Iterator[None]:
    """크림 호출 원점 태그 설정 (동기/비동기 블록 공통, contextvars 기반).

    사용:
        with kream_purpose("v3_delta"):
            await kream_client.get_products(...)

    중첩 허용 — 안쪽 값이 우선.
    """
    token = _purpose_var.set(tag)
    try:
        yield
    finally:
        _purpose_var.reset(token)


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
    """호출 전 체크. 100% 초과 시 예외.

    초과 상태가 관찰되면 `_exceeded_until` 타임스탬프까지 DB 쿼리/로그
    없이 즉시 예외를 던져 루프 스토밍을 흡수한다. 캐시 만료 시 재조회 →
    정상 복귀하면 자동으로 캐시 해제 + INFO 로그.
    """
    global _warned_today, _exceeded_until, _last_exceeded_log_at

    now = time.monotonic()
    if _exceeded_until and now < _exceeded_until:
        # 최근에 초과 확인됨 — 조용히 즉시 예외
        raise KreamBudgetExceeded(f"exceeded (cached until +{_exceeded_until - now:.0f}s)")

    used = await _count_last_24h()
    if used >= BUDGET:
        was_recovered = _exceeded_until == 0.0
        _exceeded_until = now + _EXCEEDED_CACHE_TTL_SEC
        # 첫 초과거나 로그 쓰로틀 윈도우 경과 시에만 CRITICAL 기록
        if was_recovered or (now - _last_exceeded_log_at) >= _LOG_THROTTLE_SEC:
            logger.critical(
                "KREAM 일일 캡 초과: %d/%d — 파이프라인 정지 (다음 %ds 쿼리 스킵)",
                used, BUDGET, int(_EXCEEDED_CACHE_TTL_SEC),
            )
            _last_exceeded_log_at = now
        raise KreamBudgetExceeded(f"{used}/{BUDGET} calls in last 24h")

    # 정상 범위 — 캐시 해제 + 복구 전이 로그
    if _exceeded_until:
        logger.info("KREAM 일일 캡 복구: %d/%d — 파이프라인 재개", used, BUDGET)
        _exceeded_until = 0.0
        _last_exceeded_log_at = 0.0

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
    """호출 기록 (비동기, 실패 무시 — 계측 실패로 서비스 막지 않음).

    purpose 가 기본값 "manual" 이면 contextvar 의 현재 태그로 대체한다.
    명시적으로 다른 값을 넘긴 호출부는 그대로 존중 (예: blacklist_500 접미사).
    """
    if purpose == "manual":
        purpose = _purpose_var.get()
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
