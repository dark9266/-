"""크림 cold-tier 거래량 부트스트랩 (light, 1 호출/상품, 정식화 — 조각 2-1).

기존 버전(4 호출/상품, `bootstrap_cold_volumes.py`)이 9시간 + 캡 35% 를 썼던
것을, screens API `_trades_from_screens_api` 파싱 로직 1 호출로 줄인 light
버전을 이번에 정식화했다. 이전 light 버전(커밋 a6189a2) 대비 달라진 점:

    1. **2-0 배선 필수** — 모든 실호출은 `acquire_background(purpose)` 를
       거친다(페이서 + 백그라운드 예산 + 서킷 통합, 커밋 9f6c046). 재시도도
       이 진입점을 다시 통과해야 하므로 우회 불가. 기존 `--sleep` 인자는
       제거 — 캐이서(2.5~3.5s) 가 유일한 간격 제어 계층("한 계층에서만 제어").
    2. **상태 4종 분리** (1c 정확성 원칙과 동일한 정신 — "UNKNOWN != 0"):
       `success_positive` / `success_zero`(거래영역 정상 파싱 + 확인된 0건) /
       `retryable`(타임아웃·차단·파싱실패) / `quarantined`(404/410 — 삭제·
       비공개). `last_volume_check` 는 성공 2종에서만 갱신 — 실패는 attempt
       계열 컬럼(`last_volume_attempt_at`/`volume_attempt_count`/
       `next_volume_attempt_at`/`last_volume_error`, 신설)만 갱신한다.
    5. **cold 재검사 정책** (조각 2-3, `src.core.volume_tier`) — 성공 2종은
       `next_volume_check_at`(다음 정기 재검사 시각, TTL 매트릭스 + 결정적
       ±10% 지터)도 함께 기록한다. `next_volume_attempt_at`(retryable/
       quarantined 재시도 축)과는 **별개 축** — 혼동 금지. retryable 백오프도
       `retry_backoff(attempt_count)`(1회 6h/2회 24h/3회+ 7일)로 대체했다
       (기존 `RETRYABLE_BACKOFF_HOURS=6` 고정값 제거).
    6. **`--mode recheck`** — 기존 `--mode bootstrap`(기본, `last_volume_check
       IS NULL`) 과 별개로, `next_volume_check_at <= now` 인 행(정기 재검사
       마감 도래분)을 `next_volume_check_at ASC` 정렬로 대상 삼는다. lease·
       상태모델·2-0 배선은 완전히 동일 — `fetch_targets(mode=...)` 만 분기.
    3. **chunk 원자 lease** (`volume_job_lease` 소테이블, kream_products 오염
       방지) — at-least-once + idempotent upsert 전제. 외부 GET 과 로컬
       commit 사이 exactly-once 는 불가능하므로, 같은 상품이 두 번 처리돼도
       최종 상태는 일관되게 수렴하도록 설계한다(멱등 UPDATE).
    4. **티어 중앙화** — `src.core.volume_tier.tier_for_volume()` 한 곳에서만
       hot/warm/cold/unknown 판정.

⚠️ **원샷 배치 전제**: `report_block()`(2-0, `src.core.kream_budget`)의 5xx/
timeout 연속 실패 카운터는 프로세스 인메모리다. 이 스크립트를 재시작하면
그 카운터는 리셋된다 — "직전 실행에서 이미 2번 실패했으니 이번엔 1번만 더
실패해도 트립" 같은 누적은 프로세스 경계를 넘지 않는다. 한 번 실행해 끝까지
돌리거나(원샷), 재실행 시 처음부터 스트릭을 다시 센다는 전제로 설계됐다.

실 크림 호출은 이 조각(2-1)의 TDD 스코프 밖이다(전부 mock) — 라이브 실행은
조각 2-4(카나리 + 램프).

옵션:
    --sources thenorthface,nbkorea  특정 어댑터만 (콤마구분). 미지정 시 전 어댑터.
    --dry-run                       대상/예산/ETA 만 출력 (네트워크 0).
    --limit 100                     디버그용. 처음 N건만 처리.
    --chunk-size 50                 lease 취득 chunk 크기.
    --lease-ttl 300                 lease 유효시간(초).
    --owner NAME                    lease owner 식별자 (미지정 시 PID 기반 자동생성).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # src.core.kream_budget import 전에 호출 — KREAM_DAILY_CAP 반영

import aiosqlite  # noqa: E402

from src.config import settings  # noqa: E402
from src.core.kream_budget import (  # noqa: E402
    KreamBackgroundBudgetExceeded,
    KreamBatchLockHeld,
    KreamBatchLockLost,
    KreamBudgetExceeded,
    KreamCircuitTripped,
    KreamConfigUnsafe,
    KreamLocalDropStorm,
    acquire_background,
    background_allowance,
    batch_run_lock,
    current_purpose,
    current_soft_cap,
    get_usage,
    report_block,
    report_success,
)
from src.core.kream_pacer import JITTER_MAX_SEC, MIN_INTERVAL_SEC  # noqa: E402
from src.core.volume_tier import (  # noqa: E402
    compute_next_volume_check_at,
    retry_backoff,
    tier_for_volume,
)
from src.crawlers.kream import kream_crawler  # noqa: E402
from src.models.database import Database  # noqa: E402

PURPOSE = "bootstrap_light"
SCREENS_PATH = "screens_products"

# 결과 상태 4종 (1c 원칙과 동일 — UNKNOWN/파싱실패를 확정 실패/영구 0 으로 단정 금지)
SUCCESS_POSITIVE = "success_positive"
SUCCESS_ZERO = "success_zero"
RETRYABLE = "retryable"
QUARANTINED = "quarantined"

_QUARANTINE_STATUSES = frozenset({404, 410})
_BLOCK_STATUSES = frozenset({403, 429})

# 모드 2종 (조각 2-3): bootstrap = 미확인(최초) / recheck = 정기 재검사 마감 도래분
MODE_BOOTSTRAP = "bootstrap"
MODE_RECHECK = "recheck"

QUARANTINE_DAYS = 90
DEFAULT_LEASE_TTL_SECONDS = 300.0
DEFAULT_CHUNK_SIZE = 50


# ─── 대상 조회 ──────────────────────────────────────────────────────────────

SQL_TARGET_BASE = """
    SELECT DISTINCT kp.product_id AS product_id,
                     kp.model_number AS model_number,
                     kp.volume_attempt_count AS volume_attempt_count
    FROM retail_products rp
    JOIN kream_products kp ON kp.model_number = rp.model_number
    WHERE kp.model_number != ''
      AND kp.last_volume_check IS NULL
      AND (kp.next_volume_attempt_at IS NULL OR kp.next_volume_attempt_at <= datetime('now'))
"""

# 조각 2-3 — 정기 재검사 대상(성공 이력 있음, next_volume_check_at 마감 도래분).
# `last_volume_check IS NULL`(미확인, bootstrap 모드)과 겹치지 않는다 —
# recheck 는 `next_volume_check_at` 이 채워진(=한 번 이상 성공 확인된) 행만 대상.
# 2-3r F1: `next_volume_attempt_at` 백오프 가드를 SQL_TARGET_BASE 와 동일하게
# 유지 — 쓰기(축 분리, `_apply_state`)는 그대로 두되 선별(SELECT)만 양쪽 축을
# 모두 존중해야 한다. 이 가드가 없으면 recheck 성공 이력이 있는 상품이 실패
# (retryable/quarantined)해도 `next_volume_check_at`(과거로 고정된 채) 이
# 매 recheck 실행마다 재선택되어 retry_backoff/quarantine 90일이 무력화되고,
# 403 으로 굳은 상품이 ASC 맨 앞이면 배치가 매번 즉시 전면 중단(영구 정체)된다.
SQL_TARGET_RECHECK = """
    SELECT DISTINCT kp.product_id AS product_id,
                     kp.model_number AS model_number,
                     kp.volume_attempt_count AS volume_attempt_count
    FROM retail_products rp
    JOIN kream_products kp ON kp.model_number = rp.model_number
    WHERE kp.model_number != ''
      AND kp.next_volume_check_at IS NOT NULL
      AND kp.next_volume_check_at <= datetime('now')
      AND (kp.next_volume_attempt_at IS NULL OR kp.next_volume_attempt_at <= datetime('now'))
"""


async def fetch_targets(
    db: aiosqlite.Connection,
    *,
    sources: tuple[str, ...] | None = None,
    limit: int | None = None,
    mode: str = MODE_BOOTSTRAP,
) -> list:
    """대상 조회 — `mode` 에 따라 두 축 중 하나.

    - `mode="bootstrap"`(기본): retail-matched cold + 미확인/재시도 마감 도래분
      (`last_volume_check IS NULL`, `next_volume_attempt_at` 이 없거나 지남).
    - `mode="recheck"`(조각 2-3): 정기 재검사 마감 도래분만
      (`next_volume_check_at <= now`), `next_volume_check_at ASC` 정렬 —
      오래 대기한 것부터 처리해 매달 특정 시점에 몰리지 않게 한다(지터와 함께).
    """
    is_recheck = mode == MODE_RECHECK
    sql = SQL_TARGET_RECHECK if is_recheck else SQL_TARGET_BASE
    if sources:
        placeholders = ",".join(["?"] * len(sources))
        sql += f" AND rp.source IN ({placeholders})"
        params: tuple = tuple(sources)
    else:
        params = ()
    if is_recheck:
        sql += " ORDER BY kp.next_volume_check_at ASC"
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    cursor = await db.execute(sql, params)
    return await cursor.fetchall()


# ─── chunk 원자 lease (volume_job_lease) ───────────────────────────────────


async def try_acquire_lease(
    db: aiosqlite.Connection,
    product_id: str,
    owner: str,
    ttl_seconds: float,
    *,
    now: float | None = None,
) -> bool:
    """단일 product_id lease 원자 취득 시도.

    이미 다른 owner 가 보유 중이고 아직 안 만료됐으면 실패(False).
    만료됐거나(lease_until < now) 아예 없으면 이 owner 가 획득(True).
    같은 owner 의 재호출(재진입)도 True — idempotent.
    """
    if now is None:
        now = time.time()
    lease_until = now + ttl_seconds
    await db.execute(
        """
        INSERT INTO volume_job_lease (product_id, lease_owner, lease_until)
        VALUES (?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            lease_owner = excluded.lease_owner,
            lease_until = excluded.lease_until
        WHERE volume_job_lease.lease_until < ?
           OR volume_job_lease.lease_owner = ?
        """,
        (product_id, owner, lease_until, now, owner),
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT lease_owner FROM volume_job_lease WHERE product_id = ?",
        (product_id,),
    )
    row = await cursor.fetchone()
    return bool(row) and row["lease_owner"] == owner


async def acquire_lease_chunk(
    db: aiosqlite.Connection,
    product_ids: list[str],
    owner: str,
    ttl_seconds: float,
    *,
    now: float | None = None,
) -> list[str]:
    """chunk(리스트) 단위로 각 product_id lease 시도 — 획득한 것만 반환."""
    if now is None:
        now = time.time()
    acquired = []
    for pid in product_ids:
        if await try_acquire_lease(db, pid, owner, ttl_seconds, now=now):
            acquired.append(pid)
    return acquired


# ─── 상태 분류 (success_zero vs retryable 구분) ────────────────────────────


@dataclass(frozen=True)
class ClassifyResult:
    """단일 상품 처리 결과 — 상태 4종 + 볼륨/에러/즉시중단 여부."""

    state: str
    volume_7d: int | None = None
    volume_30d: int | None = None
    error: str = ""
    immediate_stop: bool = False


def _classify_response(status: int, stats: dict | None, product_id: str) -> ClassifyResult:
    """HTTP 상태 + (이미 파싱된) 거래 통계 → 상태 4종 분류 (순수 함수, mock 친화적).

    `stats` 는 `kream_crawler.fetch_screens_status()`(2-1r F3, kream.py 공개
    API)가 이미 거래내역까지 파싱해 돌려준 결과다 — 스크립트는 raw screens
    body 를 직접 들여다보지 않는다(private 계약 제거).
    2xx 인데 `transaction_history` 구조를 못 찾은 경우 `stats=None` 이 온다
    (구조 이상 — "확인된 0건"과 구분해야 함, 1c "UNKNOWN != 0" 원칙과 동일한
    정신). 구조는 찾았지만 거래가 없으면 0값 dict(성공 zero) 가 온다.
    """
    if status in _QUARANTINE_STATUSES:
        return ClassifyResult(QUARANTINED, error=f"http_{status}")
    if status in _BLOCK_STATUSES:
        # 403/429 — report_block 후 배치 즉시 종료(재시도 금지)가 스펙.
        return ClassifyResult(RETRYABLE, error=f"http_{status}", immediate_stop=True)
    if status == 0 or 500 <= status < 600:
        return ClassifyResult(
            RETRYABLE, error=("timeout" if status == 0 else f"http_{status}")
        )
    if not (200 <= status < 300):
        return ClassifyResult(RETRYABLE, error=f"http_{status}")

    if not isinstance(stats, dict):
        return ClassifyResult(RETRYABLE, error="parse_fail")

    volume_7d = stats.get("volume_7d", 0) or 0
    volume_30d = stats.get("volume_30d", 0) or 0
    if volume_7d > 0 or volume_30d > 0:
        return ClassifyResult(SUCCESS_POSITIVE, volume_7d, volume_30d)
    return ClassifyResult(SUCCESS_ZERO, volume_7d, volume_30d)


# ─── 실호출 (mock 대상 — 2-1 TDD 스코프는 실호출 0) ────────────────────────


async def _fetch_screens_raw(product_id: str) -> tuple[int, dict | None]:
    """screens API 단일 시도(재시도 없음) — 상태코드 + 파싱된 거래 통계 반환.

    `kream_crawler.fetch_screens_status()`(2-1r F3, kream.py 공개 API) 를
    그대로 위임한다 — 밑줄 붙은 private 속성(`_get_session`/`_build_api_auth_
    headers`/모듈 상수 등)에 더 이상 의존하지 않는다. 재시도/백오프 없음,
    check_budget/record_call 은 kream.py 쪽에서 `_request()` 관례대로 수행.

    ⚠️ 이 함수는 2-1 테스트 전부에서 monkeypatch 로 대체된다(실호출 0). 실제
    네트워크 경로 검증은 조각 2-4(라이브 카나리) 스코프.
    """
    return await kream_crawler.fetch_screens_status(product_id, purpose=current_purpose())


# ─── DB 상태 반영 (idempotent UPDATE) ──────────────────────────────────────


async def _current_attempt_count(db: aiosqlite.Connection, product_id: str) -> int:
    """실패 백오프 계산용 — 이번 시도 전 기존 `volume_attempt_count` 조회."""
    cursor = await db.execute(
        "SELECT COALESCE(volume_attempt_count, 0) AS c FROM kream_products WHERE product_id = ?",
        (product_id,),
    )
    row = await cursor.fetchone()
    return int(row["c"]) if row else 0


async def _apply_state(db: aiosqlite.Connection, product_id: str, result: ClassifyResult) -> None:
    """분류 결과를 kream_products 에 반영. 성공 2종만 last_volume_check 갱신.

    성공 2종은 `next_volume_check_at`(조각 2-3, 정기 재검사 축)도 함께 기록—
    `next_volume_attempt_at`(retryable/quarantined 재시도 축)과는 별개다.
    `tier_for_volume(result.volume_7d)` — `or 0` 로 coalesce 하지 않는다.
    volume_7d 가 None 이면 'cold' 가 아니라 'unknown' 으로 정직하게 기록해야
    한다(2-1 리뷰 Minor, 2-3 이관 — "UNKNOWN != 0" 원칙).
    """
    if result.state in (SUCCESS_POSITIVE, SUCCESS_ZERO):
        tier = tier_for_volume(result.volume_7d)
        next_check_at = compute_next_volume_check_at(
            product_id, result.volume_7d, has_retail_match=True,
        )
        await db.execute(
            """
            UPDATE kream_products SET
                volume_7d = ?,
                volume_30d = ?,
                refresh_tier = ?,
                last_volume_check = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP,
                last_volume_attempt_at = CURRENT_TIMESTAMP,
                volume_attempt_count = COALESCE(volume_attempt_count, 0) + 1,
                next_volume_attempt_at = NULL,
                last_volume_error = NULL,
                next_volume_check_at = ?
            WHERE product_id = ?
            """,
            (
                result.volume_7d or 0,
                result.volume_30d or 0,
                tier,
                next_check_at.strftime("%Y-%m-%d %H:%M:%S"),
                product_id,
            ),
        )
    elif result.state == RETRYABLE:
        attempt_count = await _current_attempt_count(db, product_id) + 1
        backoff_seconds = int(retry_backoff(attempt_count).total_seconds())
        await db.execute(
            """
            UPDATE kream_products SET
                last_volume_attempt_at = CURRENT_TIMESTAMP,
                volume_attempt_count = COALESCE(volume_attempt_count, 0) + 1,
                next_volume_attempt_at = datetime('now', ?),
                last_volume_error = ?
            WHERE product_id = ?
            """,
            (f"+{backoff_seconds} seconds", result.error, product_id),
        )
    elif result.state == QUARANTINED:
        await db.execute(
            """
            UPDATE kream_products SET
                last_volume_attempt_at = CURRENT_TIMESTAMP,
                volume_attempt_count = COALESCE(volume_attempt_count, 0) + 1,
                next_volume_attempt_at = datetime('now', ?),
                last_volume_error = ?
            WHERE product_id = ?
            """,
            (f"+{QUARANTINE_DAYS} days", result.error, product_id),
        )
    await db.commit()


# ─── 단일 상품 처리 (2-0 배선) ──────────────────────────────────────────────


# ─── 세션 워밍 (2-v2 F2) ────────────────────────────────────────────────────


SESSION_INIT_PATH = "session_init"


async def warm_session() -> int | None:
    """루프 진입 전에 세션 초기화 GET 을 **자기 몫의 broker 거래**로 소비한다.

    2-v2 F2(코덱스 수렴): 이걸 하지 않으면 배치 첫 raw 호출이 허가 1회로
    초기화 GET + 본 GET 두 건을 내보내, 잔여 허용치가 1일 때 소프트캡·
    예약분·페이싱을 정확히 1건 초과한다. 워밍 이후에는 세션이 초기화된
    상태라 이후 호출은 항상 1거래 = 1요청이다.

    반환: 초기화 GET 의 상태코드(이미 초기화됐으면 추가 요청 0회).
    """
    async with acquire_background(PURPOSE):
        return await kream_crawler.ensure_session_ready()


async def _process_one(db: aiosqlite.Connection, product_id: str) -> ClassifyResult:
    """acquire_background 경유 1회 시도 → 상태분류 → report_block/success → DB 반영.

    `KreamCircuitTripped`/`KreamBackgroundBudgetExceeded`/`KreamBudgetExceeded`
    는 그대로 전파한다 — `run_batch` 가 잡아서 배치를 종료시킨다(예산/서킷
    우회 방지). `KreamBudgetExceeded` 는 라이브 하드캡(10k/24h, 백그라운드
    소프트캡과 별개) 초과 시 `check_budget()`(실제 fetch 구현 내부)이 던진다 —
    잡지 않으면 트레이스백으로 프로세스가 죽는다(2-1 리뷰 F1).
    """
    async with acquire_background(PURPOSE):
        status, data = await _fetch_screens_raw(product_id)

    result = _classify_response(status, data, product_id)

    if status in _BLOCK_STATUSES:
        await report_block(status, path=SCREENS_PATH)
    elif result.state == RETRYABLE:
        await report_block(status, path=SCREENS_PATH)
    else:
        report_success(SCREENS_PATH)

    await _apply_state(db, product_id, result)
    return result


# ─── 배치 실행 ──────────────────────────────────────────────────────────────


@dataclass
class RunSummary:
    counts: dict = field(
        default_factory=lambda: {
            SUCCESS_POSITIVE: 0, SUCCESS_ZERO: 0, RETRYABLE: 0, QUARANTINED: 0,
        }
    )
    stopped_reason: str | None = None
    processed: int = 0


async def run_batch(
    db: aiosqlite.Connection,
    product_ids: list[str],
    *,
    owner: str = "bootstrap_light",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    lease_ttl: float = DEFAULT_LEASE_TTL_SECONDS,
) -> RunSummary:
    """chunk 단위 lease → 순차 처리. 서킷/예산/차단(403·429) 시 즉시 중단."""
    summary = RunSummary()

    for i in range(0, len(product_ids), chunk_size):
        chunk = product_ids[i : i + chunk_size]
        leased = await acquire_lease_chunk(db, chunk, owner, lease_ttl)

        for pid in leased:
            try:
                result = await _process_one(db, pid)
            except KreamBatchLockLost:
                # 2-vr F3-1: 실행 중 잠금 상실 = 다른 배치와 동시 실행 상태.
                # 진행분은 이미 DB 에 반영됐고, 여기서 즉시 멈춘다.
                summary.stopped_reason = "batch_lock_lost"
                break
            except KreamCircuitTripped:
                summary.stopped_reason = "circuit_tripped"
                break
            except KreamBackgroundBudgetExceeded:
                summary.stopped_reason = "budget_exhausted"
                break
            except KreamBudgetExceeded:
                # 라이브 하드캡(10k/24h) 초과 — 백그라운드 소프트캡/서킷과 별개.
                # 잡지 않으면 트레이스백으로 죽는다(2-1 리뷰 F1) — 우아하게 중단.
                summary.stopped_reason = "kream_hard_cap"
                break

            summary.counts[result.state] += 1
            summary.processed += 1

            if result.immediate_stop:
                summary.stopped_reason = f"blocked_{result.error}"
                break

        if summary.stopped_reason:
            break

    return summary


# ─── dry-run (네트워크 0) ───────────────────────────────────────────────────


async def _collect_queue_overlap_count(db: aiosqlite.Connection, model_numbers: list[str]) -> int:
    """대상 모델번호 중 kream_collect_queue 에도 올라와 있는 고유 모델번호 수."""
    model_numbers = [m for m in model_numbers if m]
    if not model_numbers:
        return 0
    placeholders = ",".join(["?"] * len(model_numbers))
    cursor = await db.execute(
        f"SELECT COUNT(DISTINCT model_number) FROM kream_collect_queue "
        f"WHERE model_number IN ({placeholders})",
        model_numbers,
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def build_dry_run_report(db: aiosqlite.Connection, rows: list) -> dict:
    """dry-run 확장 출력 계산 — 네트워크 호출 0 (DB 조회만).

    "재시도 포함 최선/예상/최악 호출수" 는 실측 성공률이 없는 상태의 휴리스틱:
    - best  : 대상 전부 이번 1회 시도에서 확정(성공 또는 격리) — unique_targets
    - expected : 과거 이미 한 번 이상 실패해 재시도로 잡힌 대상은 평균 1회 더
      필요하다고 가정 — unique_targets + already_attempted
    - worst : 그 재시도-이력 대상들이 최대 3회 더 필요하다고 가정(보수적 상한)
    실측 보장이 아니라 계획 수립용 참고치임을 출력에도 명시한다.
    """
    unique_targets = len(rows)
    model_numbers = [r["model_number"] for r in rows]
    already_attempted = sum(1 for r in rows if (r["volume_attempt_count"] or 0) > 0)

    usage = await get_usage()
    soft_cap = await current_soft_cap()
    allowance = await background_allowance()

    calls_best_case = unique_targets
    calls_expected = unique_targets + already_attempted
    calls_worst_case = unique_targets + already_attempted * 3

    avg_interval = MIN_INTERVAL_SEC + JITTER_MAX_SEC / 2
    eta_seconds_expected = calls_expected * avg_interval

    dup_count = await _collect_queue_overlap_count(db, model_numbers)

    return {
        "unique_targets": unique_targets,
        "already_attempted_before": already_attempted,
        "kream_used_24h": usage["used"],
        "kream_hard_cap": usage["cap"],
        "kream_hard_remaining": usage["remaining"],
        "bg_soft_cap": soft_cap,
        "bg_soft_remaining": allowance,
        "calls_best_case": calls_best_case,
        "calls_expected": calls_expected,
        "calls_worst_case": calls_worst_case,
        "eta_seconds_expected": eta_seconds_expected,
        "collect_queue_overlap": dup_count,
    }


def _format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}분"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}시간 {rem}분"


# ─── CLI ────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="크림 cold-tier 거래량 부트스트랩 (light, 정식화 — 조각 2-1)",
    )
    p.add_argument(
        "--mode", type=str, choices=(MODE_BOOTSTRAP, MODE_RECHECK), default=MODE_BOOTSTRAP,
        help=f"'{MODE_BOOTSTRAP}'(기본, 미확인 최초 조회) 또는 "
             f"'{MODE_RECHECK}'(조각 2-3, 정기 재검사 마감 도래분).",
    )
    p.add_argument("--sources", type=str, default=None,
                   help="콤마구분 어댑터(소싱처) 이름. 미지정 시 전 어댑터 매칭.")
    p.add_argument("--dry-run", action="store_true",
                   help="대상/예산/ETA 만 출력. 네트워크 호출 0.")
    p.add_argument("--limit", type=int, default=None,
                   help="디버그용. 처음 N건만 처리.")
    p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                   help=f"lease 취득 chunk 크기 (기본 {DEFAULT_CHUNK_SIZE}).")
    p.add_argument("--lease-ttl", type=float, default=DEFAULT_LEASE_TTL_SECONDS,
                   help=f"lease 유효시간(초, 기본 {DEFAULT_LEASE_TTL_SECONDS:.0f}).")
    p.add_argument("--owner", type=str, default=None,
                   help="lease owner 식별자 (미지정 시 PID 기반 자동생성).")
    return p.parse_args()


async def main() -> int:
    args = _parse_args()
    sources = (
        tuple(s.strip() for s in args.sources.split(",") if s.strip())
        if args.sources else None
    )
    owner = args.owner or f"bootstrap-{os.getpid()}"

    db_manager = Database(settings.db_path)
    await db_manager.connect()
    db = db_manager.db

    label = "recheck" if args.mode == MODE_RECHECK else "bootstrap"

    try:
        rows = await fetch_targets(db, sources=sources, limit=args.limit, mode=args.mode)

        if args.dry_run:
            report = await build_dry_run_report(db, rows)
            print(f"[bootstrap-light:{label}] DRY RUN (2-0 배선/lease/상태모델, 네트워크 0)")
            print(f"  고유 대상 수         : {report['unique_targets']:,}")
            print(f"  기존 재시도 대상     : {report['already_attempted_before']:,}")
            print(
                f"  KREAM 24h 사용량     : {report['kream_used_24h']:,} / "
                f"{report['kream_hard_cap']:,} (하드캡 잔여 {report['kream_hard_remaining']:,})"
            )
            print(
                f"  백그라운드 소프트캡  : {report['bg_soft_cap']:,} "
                f"(이번 순간 잔여 {report['bg_soft_remaining']:,})"
            )
            print(
                f"  예상 호출(최선/예상/최악, 휴리스틱): {report['calls_best_case']:,} / "
                f"{report['calls_expected']:,} / {report['calls_worst_case']:,}"
            )
            print(f"  페이싱 기준 예상 소요: {_format_duration(report['eta_seconds_expected'])}")
            print(f"  collect_queue 중복   : {report['collect_queue_overlap']:,}")
            print()
            print("  실 실행 시: 위 인자에서 --dry-run 빼고 재실행")
            return 0

        if not rows:
            print(f"[bootstrap-light:{label}] 대상 없음 — 종료 (이미 갱신됐거나 매칭 0건)")
            return 0

        product_ids = [r["product_id"] for r in rows]

        # 2-v(F3): 다른 백그라운드 배치(drain_collect_queue.py 등)가 동시에
        # 실행 중이면 페이서/예약분이 프로세스 경계를 넘어 공유되지 않아
        # 간격·예산 보장이 깨진다 — DB 잠금으로 동시 실행 자체를 거부한다.
        try:
            async with batch_run_lock(owner):
                print(
                    f"[bootstrap-light:{label}] 시작 | 대상 {len(product_ids):,}건 | "
                    f"owner={owner}"
                )
                try:
                    init_status = await warm_session()
                except (
                    KreamCircuitTripped,
                    KreamBackgroundBudgetExceeded,
                    KreamBudgetExceeded,
                    KreamBatchLockLost,
                    KreamConfigUnsafe,
                    KreamLocalDropStorm,
                ) as exc:
                    # 워밍 자체가 서킷/예산/잠금/로컬폭주로 거부 — 트레이스백 대신 우아한 종료
                    print(f"[bootstrap-light:{label}] 세션 워밍 거부 — 종료: {exc}")
                    return 1
                if init_status in _BLOCK_STATUSES:
                    await report_block(init_status, path=SESSION_INIT_PATH)
                    print(
                        f"[bootstrap-light:{label}] 세션 초기화가 차단됨"
                        f"(status={init_status}) — 본 요청 없이 종료"
                    )
                    return 1
                summary = await run_batch(
                    db, product_ids, owner=owner,
                    chunk_size=args.chunk_size, lease_ttl=args.lease_ttl,
                )
        except KreamBatchLockHeld as exc:
            print(f"[bootstrap-light:{label}] 배치 잠금 보유 중 — 동시 실행 거부: {exc}")
            return 1

        print()
        print(
            f"[bootstrap-light:{label}] 종료 | 처리 {summary.processed}/{len(product_ids)} | "
            f"양성 {summary.counts[SUCCESS_POSITIVE]} / 확인0 {summary.counts[SUCCESS_ZERO]} / "
            f"재시도대기 {summary.counts[RETRYABLE]} / 격리 {summary.counts[QUARANTINED]}"
        )
        if summary.stopped_reason:
            print(
                f"[bootstrap-light] 중단 사유: {summary.stopped_reason} — "
                "재실행 시 자동 resume (lease 만료 후, next_volume_attempt_at 도래분만)"
            )
        return 0
    finally:
        await db_manager.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
