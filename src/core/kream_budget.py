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

--------------------------------------------------------------------------
백그라운드 예산 브로커 (조각 2-0, 2026-07-25 추가)

2-1(bootstrap)~2-4(라이브 배치) 등 "백그라운드" 크림 호출(스케줄러 밖 일회성
배치)은 위 라이브 하드캡과 별개로 이 아래 API 를 통해서만 크림을 조회한다.
기존 `check_budget`/`record_call` 시그니처·동작은 이 확장으로 변경되지 않는다
(라이브 경로는 오히려 예약분 덕에 보호됨).

- **라이브 예약분**: 백그라운드는 `10000 - 최근24h 전체사용량 - 1000` 을 넘지
  못한다 (배치가 라이브 알림 경로를 굶기지 않도록).
- **소프트캡 램프**: canary(100) → day1(500) → day2(1000) → ramp2000(2000) →
  ramp3000(3000). 단계 전환은 시간 경과 자동이 아니라 `advance_ramp_stage()`
  명시 호출로만 (2-4 라이브 배치가 "이상 없음" 확인 후 호출).
- **서킷브레이커**: `report_block(403|429)` 1회 또는 동일 path 5xx/timeout
  연속 3회 → `kream_bg_budget_state.circuit_tripped=1` → 이후 모든
  `acquire_background()` 거부. 해제는 `manual_resume()` 호출로만(자동 복귀 없음).
- **통합 진입점**: `async with acquire_background(purpose): ...` — 서킷 확인 →
  예산 판정 → 페이서(`kream_pacer`) 대기 → `kream_purpose(purpose)` 태깅까지
  한 번에. 재시도도 이 컨텍스트매니저를 다시 통과해야 하므로 페이서/예산
  우회가 불가능하다.

프로세스 내 전역 싱글턴 스코프 — `src.core.kream_pacer` 모듈 docstring 참조.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime

from src.config import settings
from src.core.db import async_connect
from src.core.kream_pacer import get_pacer
from src.utils.logging import setup_logger

logger = setup_logger("kream_budget")

BUDGET: int = int(os.getenv("KREAM_DAILY_CAP", "10000"))
WARN_RATIO: float = 0.9

# --- 백그라운드 예산 브로커 상수 ---------------------------------------------
BG_LIVE_RESERVE: int = int(os.getenv("KREAM_BG_LIVE_RESERVE", "1000"))
BG_RAMP_STAGES: dict[str, int] = {
    "canary": 100,
    "day1": 500,
    "day2": 1000,
    "ramp2000": 2000,
    "ramp3000": 3000,
}
BG_RAMP_ORDER: tuple[str, ...] = ("canary", "day1", "day2", "ramp2000", "ramp3000")
_BG_5XX_TRIP_THRESHOLD: int = 3
_BG_BLOCK_STATUSES: frozenset[int] = frozenset({403, 429})

# 백그라운드 purpose 레지스트리 (2-v F1 — 코덱스 적대검증 실결함 반영).
# `background_allowance()` 의 소프트캡은 "백그라운드가 실제로 쓴 최근24h
# 호출수" 를 차감해야 한다 — 이 집합이 그 "무엇이 백그라운드냐"의 단일
# 판정 기준이다. 규약: **prefix 매칭** — `kream_api_calls.purpose` 가
# 이 집합의 어느 원소와 정확히 같거나 `"{원소}:"` 로 시작하면 백그라운드로
# 센다 (예: "bootstrap_light" 자체는 물론 세션 초기화 계측용
# "bootstrap_light:session_init" 도 포함 — 그 GET 도 같은 배치가 유발한
# 실제 크림 히트이므로 소프트캡 소비에 포함되는 게 맞다).
# 새 백그라운드 소비자 추가 시 여기 등록만 하면 자동으로 카운트된다.
# 현재 등록: bootstrap_cold_volumes_light.py(PURPOSE="bootstrap_light",
# --mode recheck 도 동일 PURPOSE 재사용) · drain_collect_queue.py
# (PURPOSE="collect_queue_drain").
BG_PURPOSE_PREFIXES: frozenset[str] = frozenset({"bootstrap_light", "collect_queue_drain"})

_BG_STATE_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS kream_bg_budget_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    stage TEXT NOT NULL DEFAULT 'canary',
    stage_entered_at TEXT NOT NULL DEFAULT (datetime('now')),
    circuit_tripped INTEGER NOT NULL DEFAULT 0,
    circuit_reason TEXT,
    manual_resume_required INTEGER NOT NULL DEFAULT 0
)
"""

# path별 연속 5xx/timeout 카운터 — 프로세스 전역(페이서와 동일 스코프).
# 트립된 순간 DB(circuit_tripped)에 영속화되므로 프로세스 재기동 후에도
# 재개는 manual_resume() 를 통해서만 가능.
_bg_consecutive_failures: dict[str, int] = {}

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
    async with async_connect(settings.db_path) as db:
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
        async with async_connect(settings.db_path) as db:
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


# =============================================================================
# 백그라운드 예산 브로커 (조각 2-0)
# =============================================================================


class KreamCircuitTripped(RuntimeError):
    """백그라운드 크림 서킷 트립 상태 — manual_resume() 호출 전까지 전면 거부."""


class KreamBackgroundBudgetExceeded(RuntimeError):
    """백그라운드 소프트캡/라이브 예약분 소진 — 이번 호출 불허."""


async def _ensure_bg_state(db) -> None:
    """`kream_bg_budget_state` 싱글턴 행 보장 (기존 CREATE IF NOT EXISTS 관례)."""
    await db.execute(_BG_STATE_SCHEMA)
    await db.execute(
        "INSERT OR IGNORE INTO kream_bg_budget_state (id, stage, stage_entered_at) "
        "VALUES (1, 'canary', datetime('now'))"
    )


async def get_ramp_state() -> dict:
    """현재 램프/서킷 상태 스냅샷 (DB 조회, 없으면 canary 기본값으로 생성)."""
    async with async_connect(settings.db_path) as db:
        await _ensure_bg_state(db)
        await db.commit()
        cur = await db.execute(
            "SELECT stage, stage_entered_at, circuit_tripped, circuit_reason, "
            "manual_resume_required FROM kream_bg_budget_state WHERE id = 1"
        )
        row = await cur.fetchone()
    return {
        "stage": row["stage"],
        "stage_entered_at": row["stage_entered_at"],
        "circuit_tripped": bool(row["circuit_tripped"]),
        "circuit_reason": row["circuit_reason"],
        "manual_resume_required": bool(row["manual_resume_required"]),
    }


def ramp_soft_cap(stage: str) -> int:
    """단계별 고정 소프트캡. 알 수 없는 단계는 가장 보수적인 canary 로 취급."""
    return BG_RAMP_STAGES.get(stage, BG_RAMP_STAGES["canary"])


async def current_soft_cap() -> int:
    """현재 램프 단계의 소프트캡."""
    state = await get_ramp_state()
    return ramp_soft_cap(state["stage"])


async def advance_ramp_stage() -> str:
    """다음 램프 단계로 **수동** 승급.

    시간 경과만으로 자동 승급하지 않는다 — 2-4 라이브 배치가 "이상 없음"을
    확인한 뒤에만 호출하는 명시적 API. 서킷 트립 상태에서는 승급 불가
    (먼저 `manual_resume()` 로 해제해야 함). 이미 최종 단계(ramp3000)면 그대로 유지.
    """
    state = await get_ramp_state()
    if state["circuit_tripped"]:
        raise KreamCircuitTripped(
            f"서킷 트립 상태({state['circuit_reason']}) — manual_resume() 먼저 필요"
        )
    stage = state["stage"]
    idx = BG_RAMP_ORDER.index(stage) if stage in BG_RAMP_ORDER else 0
    if idx >= len(BG_RAMP_ORDER) - 1:
        return stage
    next_stage = BG_RAMP_ORDER[idx + 1]
    async with async_connect(settings.db_path) as db:
        await _ensure_bg_state(db)
        await db.execute(
            "UPDATE kream_bg_budget_state SET stage = ?, stage_entered_at = datetime('now') "
            "WHERE id = 1",
            (next_stage,),
        )
        await db.commit()
    logger.info("KREAM 백그라운드 램프 승급: %s → %s", stage, next_stage)
    return next_stage


def _like_escape(value: str) -> str:
    r"""LIKE 패턴에서 리터럴로 다뤄야 할 문자 이스케이프 (`ESCAPE '\'` 전제).

    2-vr M1 — 등록 purpose 는 `bootstrap_light` 처럼 밑줄을 포함하는데, SQL
    LIKE 에서 `_` 는 "임의의 1문자" 와일드카드다. 이스케이프하지 않으면
    `bootstrapXlight:...` 같은 무관한 purpose 까지 백그라운드로 오집계된다.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _bg_purpose_sql() -> tuple[str, list[str]]:
    """`BG_PURPOSE_PREFIXES` 매칭 조건식 + 바인딩 파라미터.

    규약: purpose 가 등록 원소와 **정확히 같거나** `"{원소}:"` 로 시작.
    등록이 비어 있으면 항상 거짓인 식("0")을 돌려준다.
    """
    if not BG_PURPOSE_PREFIXES:
        return "0", []
    clauses = []
    params: list[str] = []
    for prefix in sorted(BG_PURPOSE_PREFIXES):
        clauses.append("purpose = ? OR purpose LIKE ? ESCAPE '\\'")
        params.extend([prefix, f"{_like_escape(prefix)}:%"])
    return " OR ".join(clauses), params


async def _count_24h_total_and_background() -> tuple[int, int]:
    """최근 24h (전체 호출수, 백그라운드 호출수) — **단일 SELECT 스냅샷**.

    2-vr M2 — 전체/백그라운드를 각각 따로 조회하면 그 사이 라이브 봇이 새 행을
    커밋했을 때 서로 다른 시점을 본 두 수치로 예약분을 계산하게 된다. 한 문장
    안에서 세면 그런 창 자체가 없고, 백그라운드 호출마다 열던 DB 커넥션도
    2회 → 1회로 준다.
    """
    where, params = _bg_purpose_sql()
    async with async_connect(settings.db_path) as db:
        cur = await db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN ({where}) THEN 1 ELSE 0 END), 0) "
            f"FROM kream_api_calls WHERE ts >= datetime('now','-1 day')",
            params,
        )
        row = await cur.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)


async def _count_background_used_24h() -> int:
    """최근 24h `BG_PURPOSE_PREFIXES` 에 매칭되는 호출수 (소프트캡 소비량).

    라이브 경로(purpose 가 bg 접두사와 무관 — "manual"/"v3_delta" 등)는
    이 집계에서 제외된다 — 소프트캡은 백그라운드 소비만 차감하고, 라이브
    사용량은 하드캡(`_count_last_24h`) 쪽에서만 영향을 준다.
    """
    _total, used_bg = await _count_24h_total_and_background()
    return used_bg


async def background_allowance() -> int:
    """이번 순간 백그라운드가 쓸 수 있는 호출 허용치.

    = max(0, min(현재 단계 소프트캡 - 최근24h 백그라운드 사용량,
                 10000(하드캡) - 최근24h 전체사용량 - 1000(라이브 예약분)))

    두 축을 모두 만족해야 한다: ① 소프트캡은 "이 램프 단계에서 백그라운드가
    실제로 이미 쓴 만큼" 을 차감해 계속 소진되게 하고(2-v F1 — 이전에는
    소프트캡이 소비량과 무관한 상수라서 카나리 100 단계에서도 수천 건이
    허용되는 실결함이 있었다), ② 예약분은 라이브 알림 경로를 위해 항상
    남겨둔다. 어느 쪽이든 음수가 나오면 0 으로 클램프.
    """
    used_total, used_bg = await _count_24h_total_and_background()
    soft = await current_soft_cap()
    soft_room = soft - used_bg
    reserved_room = BUDGET - used_total - BG_LIVE_RESERVE
    return max(0, min(soft_room, reserved_room))


def _is_retryable_failure(status: int) -> bool:
    """5xx 또는 timeout(관례상 상태코드 0 으로 표현)."""
    return status == 0 or 500 <= status < 600


async def _trip_circuit(reason: str, *, manual_resume_required: bool) -> None:
    async with async_connect(settings.db_path) as db:
        await _ensure_bg_state(db)
        await db.execute(
            "UPDATE kream_bg_budget_state SET circuit_tripped = 1, circuit_reason = ?, "
            "manual_resume_required = ? WHERE id = 1",
            (reason, int(manual_resume_required)),
        )
        await db.commit()
    logger.critical(
        "KREAM 백그라운드 서킷 트립: %s (manual_resume_required=%s)", reason, manual_resume_required
    )


async def report_block(status: int, *, path: str = "default") -> None:
    """호출 실패 관측 보고 — 서킷브레이커 판정.

    - 403/429: **즉시** 전면 트립 + `manual_resume_required=1` (재시도로 예산
      소모 금지가 스펙 — 자동 급속 재개 없음).
    - 동일 `path` 5xx/timeout(status=0) 연속 3회: 트립(냉각) — 마찬가지로
      `manual_resume()` 로만 해제.
    - 그 외 상태코드는 무시 (오용 방지, 트립하지 않음).
    """
    if status in _BG_BLOCK_STATUSES:
        _bg_consecutive_failures.pop(path, None)
        await _trip_circuit(f"blocked_status_{status}", manual_resume_required=True)
        return
    if _is_retryable_failure(status):
        count = _bg_consecutive_failures.get(path, 0) + 1
        _bg_consecutive_failures[path] = count
        if count >= _BG_5XX_TRIP_THRESHOLD:
            await _trip_circuit(f"5xx_streak:{path}:{count}", manual_resume_required=False)
        return
    # 2xx/기타 — report_block 호출 자체가 오용이므로 무시


def report_success(path: str = "default") -> None:
    """성공 응답 관측 시 해당 path 의 연속 실패 카운터 리셋 (2-1/2-4 배선용)."""
    _bg_consecutive_failures.pop(path, None)


async def manual_resume() -> None:
    """서킷 수동 재개 — 403/429·5xx 트립 모두 이 함수로만 해제된다.

    circuit_tripped/circuit_reason/manual_resume_required 초기화 + 프로세스
    전역 연속 실패 카운터 리셋. 자동 복귀 경로는 없다(스펙).
    """
    _bg_consecutive_failures.clear()
    async with async_connect(settings.db_path) as db:
        await _ensure_bg_state(db)
        await db.execute(
            "UPDATE kream_bg_budget_state SET circuit_tripped = 0, circuit_reason = NULL, "
            "manual_resume_required = 0 WHERE id = 1"
        )
        await db.commit()
    logger.info("KREAM 백그라운드 서킷 수동 재개")


async def is_circuit_tripped() -> bool:
    """현재 서킷 트립 여부."""
    state = await get_ramp_state()
    return state["circuit_tripped"]


# =============================================================================
# 배치 상호배제 잠금 (2-v F3 — 코덱스 적대검증 실결함)
#
# 페이서(kream_pacer)는 "프로세스 내" 전역 싱글턴이다. bootstrap_cold_volumes_
# light.py 와 drain_collect_queue.py 를 별도 프로세스로 동시에 실행하면 각자
# 페이서를 가져 간격 보장이 프로세스 경계를 넘어서지 못하고, `background_
# allowance()` 판정도 두 프로세스가 동시에 조회하면 같은 잔여량을 두 번 볼 수
# 있어(비원자) 예약분 경계를 넘길 수 있다. 크로스 프로세스로 페이서 자체를
# 공유하는 대신(2-0 스코프 밖으로 명시 확정) — "배치 동시 실행 자체를 DB 잠금
# 으로 막는다" 로 문제를 소거한다. `volume_job_lease`(2-1) 와 동일한
# INSERT/UPDATE-WHERE 원자 idiom.
# =============================================================================

_BATCH_LOCK_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS kream_bg_batch_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    lock_owner TEXT,
    lock_until REAL
)
"""

DEFAULT_BATCH_LOCK_TTL_SEC: float = float(
    os.getenv("KREAM_BG_BATCH_LOCK_TTL_SEC", str(2 * 3600))
)  # 2h — 크래시 대비 자동 만료
# TTL 은 "크래시 후 얼마나 빨리 되찾느냐" 만 정한다. 실행 중인 배치는 아래
# 하트비트가 TTL/N 주기로 갱신하므로 아무리 오래 돌아도 만료되지 않는다
# (램프 3000단계 × 페이서 3s ≈ 2.9h > TTL 2h 였다 — 갱신 없이는 실행 도중
# 잠금이 풀려 F3 가 무력화된다).
_BATCH_LOCK_RENEW_DIVISOR: int = 4


class KreamBatchLockHeld(RuntimeError):
    """다른 owner 가 이미 백그라운드 배치 실행 잠금을 보유 중 — 동시 실행 거부."""


class KreamBatchLockLost(RuntimeError):
    """실행 도중 잠금을 다른 owner 에게 빼앗겼다 — 신규 백그라운드 호출 거부.

    2-vr F3-1(리뷰 Important): 하트비트가 상실을 감지해도 경고 로그만 남기면
    본문이 그대로 완주해, F3 가 닫으려던 "두 배치 동시 실행" 이 그대로 재발한다.
    이제 상실 시점 이후의 `acquire_background()` 진입을 막아 배치가 다음 호출에서
    우아하게 멈춘다(진행 중이던 lease/DB 반영을 중간에 끊지는 않는다).
    """


# 프로세스당 배치 1개 전제 — 하트비트(별도 태스크)가 세우고 본문 태스크가 읽는다.
_batch_lock_lost: bool = False


def batch_lock_lost() -> bool:
    """이 프로세스의 배치가 실행 도중 잠금을 잃었는지."""
    return _batch_lock_lost


async def _ensure_batch_lock_schema(db) -> None:
    await db.execute(_BATCH_LOCK_SCHEMA)
    await db.execute(
        "INSERT OR IGNORE INTO kream_bg_batch_lock (id, lock_owner, lock_until) "
        "VALUES (1, NULL, NULL)"
    )


async def try_acquire_batch_lock(
    owner: str, *, ttl_seconds: float = DEFAULT_BATCH_LOCK_TTL_SEC
) -> bool:
    """배치 실행 잠금 원자 획득 시도.

    이미 다른 owner 가 보유 중이고 안 만료됐으면 실패(False). 비어있거나
    (`lock_until IS NULL`) 만료됐으면(`lock_until < now`) 이 owner 가 획득
    (True). 같은 owner 의 재호출(재진입)도 True — idempotent.
    단일 UPDATE 문 안에서 조건과 갱신이 함께 일어나 SQLite 의 writer 직렬화
    (WAL busy_timeout)에 기대어 프로세스 간에도 원자적이다.
    """
    now = time.time()
    lock_until = now + ttl_seconds
    async with async_connect(settings.db_path) as db:
        await _ensure_batch_lock_schema(db)
        await db.commit()
        await db.execute(
            """
            UPDATE kream_bg_batch_lock
            SET lock_owner = ?, lock_until = ?
            WHERE id = 1 AND (lock_until IS NULL OR lock_until < ? OR lock_owner = ?)
            """,
            (owner, lock_until, now, owner),
        )
        await db.commit()
        cur = await db.execute("SELECT lock_owner FROM kream_bg_batch_lock WHERE id = 1")
        row = await cur.fetchone()
    return bool(row) and row["lock_owner"] == owner


async def release_batch_lock(owner: str) -> None:
    """보유 중인 배치 잠금 해제. 다른 owner 가 이미 보유 중이면 손대지 않는다
    (만료 후 다른 owner 가 선점한 경우 그 lease 를 실수로 풀지 않기 위함)."""
    async with async_connect(settings.db_path) as db:
        await _ensure_batch_lock_schema(db)
        await db.execute(
            "UPDATE kream_bg_batch_lock SET lock_owner = NULL, lock_until = NULL "
            "WHERE id = 1 AND lock_owner = ?",
            (owner,),
        )
        await db.commit()


async def _renew_batch_lock_loop(owner: str, ttl_seconds: float, interval: float) -> None:
    """실행 중인 배치의 잠금을 주기적으로 연장 (하트비트).

    같은 owner 의 재획득은 idempotent 하게 `lock_until` 을 밀어주므로
    `try_acquire_batch_lock` 을 그대로 재사용한다. DB 일시 오류는 무시하고
    다음 주기에 재시도한다(잠금을 잃었다는 증거가 아니다).

    갱신이 "다른 owner 가 선점" 으로 실패하면(=이미 동시 실행 상태)
    `_batch_lock_lost` 를 세워 이후 `acquire_background()` 진입을 거부시킨다
    (2-vr F3-1). 본문을 강제로 죽이지는 않는다 — 진행 중인 lease/DB 반영은
    끝내고 다음 크림 호출 시도에서 우아하게 멈춘다.
    """
    global _batch_lock_lost
    while True:
        await asyncio.sleep(interval)
        try:
            still_mine = await try_acquire_batch_lock(owner, ttl_seconds=ttl_seconds)
        except Exception as exc:  # DB 일시 오류 — 다음 주기에 재시도
            logger.warning("배치 잠금 갱신 실패(계속 진행): %s", exc)
            continue
        if not still_mine:
            _batch_lock_lost = True
            logger.error(
                "배치 잠금을 다른 owner 에게 빼앗김 — 신규 백그라운드 호출 차단 (owner=%s)",
                owner,
            )
            return  # 더 갱신해도 의미 없다 — 소유권은 이미 넘어갔다


@asynccontextmanager
async def batch_run_lock(
    owner: str,
    *,
    ttl_seconds: float = DEFAULT_BATCH_LOCK_TTL_SEC,
    renew_interval: float | None = None,
) -> AsyncIterator[None]:
    """배치 스크립트 본문 전체를 감싸는 상호배제 컨텍스트매니저.

    사용 (스크립트 main()):
        owner = f"bootstrap-{os.getpid()}"
        async with batch_run_lock(owner):
            ... 배치 본문(acquire_background 호출 포함) ...

    이미 다른 owner 가 보유 중이면 `KreamBatchLockHeld` 를 던지고 즉시
    종료한다(예산/페이서 소비 없음 — 잠금 판정 자체는 크림 호출이 아니다).
    정상 종료든 예외든 `finally` 에서 반드시 해제. 크래시로 해제가 누락돼도
    `ttl_seconds`(기본 2h) 경과 후 자동 만료돼 다음 실행이 되찾을 수 있다.

    본문이 도는 동안에는 하트비트 태스크가 `ttl_seconds/4`(기본 30분) 주기로
    잠금을 연장한다 — 배치가 TTL 보다 오래 걸려도(램프 상위 단계에서는 정상)
    실행 도중 만료돼 다른 배치가 끼어드는 일이 없다. 그럼에도 잠금을 잃으면
    (이벤트루프 장기 블로킹 등) `acquire_background()` 가 `KreamBatchLockLost`
    로 거부해 배치가 다음 호출에서 멈춘다(2-vr F3-1).
    """
    global _batch_lock_lost
    acquired = await try_acquire_batch_lock(owner, ttl_seconds=ttl_seconds)
    if not acquired:
        raise KreamBatchLockHeld(
            f"백그라운드 배치 잠금 보유 중(owner={owner} 아님) — 동시 실행 거부"
        )
    _batch_lock_lost = False
    interval = (
        renew_interval
        if renew_interval is not None
        else max(1.0, ttl_seconds / _BATCH_LOCK_RENEW_DIVISOR)
    )
    renewer = asyncio.create_task(_renew_batch_lock_loop(owner, ttl_seconds, interval))
    try:
        yield
    finally:
        renewer.cancel()
        with suppress(asyncio.CancelledError):
            await renewer
        await release_batch_lock(owner)
        _batch_lock_lost = False


@asynccontextmanager
async def acquire_background(purpose: str) -> AsyncIterator[None]:
    """백그라운드 크림 호출 통합 진입점 — 서킷 확인 → 예산 판정 → 페이서 대기.

    사용:
        async with acquire_background("bootstrap_light"):
            await kream_client.get_products(...)   # kream_purpose 자동 태깅됨

    재시도도 이 컨텍스트매니저를 다시 통과해야 하며, 매번 서킷/예산을 재확인하고
    페이서 차례를 다시 기다린다 — 재시도로 페이서·쿼터를 우회할 수 없다.
    예산 초과/서킷 트립 시에는 페이서를 소비하기 전에 즉시 예외를 던진다
    (거부될 호출로 대기 시간을 낭비하지 않기 위함).
    """
    if _batch_lock_lost:
        # 2-vr F3-1: 이 프로세스는 배치 잠금을 잃었다 = 다른 배치가 동시에 돌고
        # 있다. 페이서·예산 판정이 프로세스 경계를 못 넘으므로 신규 호출 거부.
        raise KreamBatchLockLost(
            "배치 잠금 상실 — 다른 배치가 동시 실행 중, 신규 백그라운드 호출 거부"
        )
    state = await get_ramp_state()
    if state["circuit_tripped"]:
        raise KreamCircuitTripped(
            f"서킷 트립 상태({state['circuit_reason']}) — manual_resume() 필요"
        )
    allowance = await background_allowance()
    if allowance <= 0:
        raise KreamBackgroundBudgetExceeded(
            f"백그라운드 예산 소진 (stage={state['stage']} soft={ramp_soft_cap(state['stage'])})"
        )
    await get_pacer().wait_turn()
    with kream_purpose(purpose):
        yield
