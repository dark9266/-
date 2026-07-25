"""크림 미등재 신상품 collect_queue 드레인 배치 (조각 2-2).

`kream_collect_queue`(22.8k 행, pending 21.5k)를 정리한다. 순서:

    0. **로컬 재대조** (네트워크 0) — 큐의 정규화 모델번호를 현재
       `kream_products` 와 재대조해, 그 사이 등재된 항목을 검색 호출 없이
       `found` 로 정리한다.
    1. **canonical_model_key 중복 검색 방지** — 같은 모델을 여러 소싱처가
       올려도(다른 URL) 대표 1행만 검색하고 결과를 전 행에 반영한다.
    2. **우선순위 정렬** — retail 증거(최근 관측) > 최근 추가·갱신 > 오래된
       backlog. dormant 는 대상 제외.
    3. **재검색 스케줄** — `attempts`는 "정상 검색 응답에서 exact match
       없음"일 때만 증가한다(transport 실패는 미증가, `next_search_at`만
       연기). 1회차 → +7일, 2회차 → +30일, 3회차 → dormant=1.

검색은 `kream_crawler.fetch_search_status()`(2-2r F1, kream.py 공개 API —
`fetch_screens_status()` 2-1r F3 와 동일 정신의 단일시도 raw 경로)를 통해
수행한다. 애초 브리프는 `collector.collect_pending()`이 쓰던 `_request(max_
retries=1)` 경로 재사용을 명시했으나, 리뷰(2-2r F1)에서 그 경로가 403 시
쿠키 재초기화용 미계측 GET 을 추가로 보내고 429 는 지수 백오프로 반복
sleep하며 결국 403/429/5xx/timeout 을 상태코드 없이 None 으로 뭉개
"차단 1회 = 즉시 전면 중단"(2-0) 계약을 깬다는 점이 확인되어 정정됐다.
호출 직전 2-0 `acquire_background(purpose)`를 통과시켜 페이서·백그라운드
예산·서킷브레이커를 우회 불가능하게 배선하는 것은 그대로다
(`scripts/bootstrap_cold_volumes_light.py` 2-1 배선 표준과 동일 패턴).

⚠️ **원샷 배치 전제**: `report_block()`의 5xx/timeout 연속 실패 카운터는
프로세스 인메모리다 — bootstrap 스크립트 docstring과 동일 전제.

실 크림 호출은 이 조각(2-2)의 TDD 스코프 밖이다(전부 mock) — 라이브 실행은
조각 2-4(카나리 + 램프).

옵션:
    --dry-run              로컬 재대조 예상치 + 검색 대상/예산만 출력 (네트워크 0).
    --budget-share 0.2      이번 런이 쓸 수 있는 백그라운드 배분 비율 (기본 20%).
    --limit 100             디버그용. 검색 대상 처음 N행만 조회.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # src.core.kream_budget import 전에 호출 — KREAM_DAILY_CAP 반영

import aiosqlite  # noqa: E402

from src.config import settings  # noqa: E402
from src.core.kream_budget import (  # noqa: E402
    KreamBackgroundBudgetExceeded,
    KreamBatchLockHeld,
    KreamBudgetExceeded,
    KreamCircuitTripped,
    acquire_background,
    background_allowance,
    batch_run_lock,
    current_purpose,
    current_soft_cap,
    get_usage,
    report_block,
    report_success,
)
from src.crawlers.kream import kream_crawler  # noqa: E402
from src.models.database import Database  # noqa: E402

PURPOSE = "collect_queue_drain"
SEARCH_PATH = "search_products"
DEFAULT_BUDGET_SHARE = 0.2

# 결과 상태 3종 — bootstrap(2-1)의 4종 상태모델과 같은 정신(1c 원칙: 응답
# 없음/파싱실패를 확정 실패로 단정 금지). FOUND 는 크림에 exact match 발견,
# NO_MATCH 는 정상 응답인데 일치 없음(attempts 증가 대상), RETRYABLE 은
# transport 실패(차단·5xx·timeout·파싱실패 — attempts 미증가).
FOUND = "found"
NO_MATCH = "no_match"
RETRYABLE = "retryable"

_BLOCK_STATUSES = frozenset({403, 429})

TRANSPORT_RETRY_BACKOFF_HOURS = 6
RESEARCH_SCHEDULE_DAYS = {1: 7, 2: 30}
DORMANT_AT_ATTEMPTS = 3


# ─── canonical key (collector._key 와 동일 규칙) ───────────────────────────


def canonical_model_key(model_number: str) -> str:
    """모델번호 정규화 + strip 키 — `collector.collect_pending._key` 와 동일 규칙."""
    from src.matcher import normalize_model_number

    return normalize_model_number(model_number or "").replace(" ", "").replace("-", "")


# ─── 단계 0: 로컬 재대조 (네트워크 0) ───────────────────────────────────────


async def _classify_local_matches(db: aiosqlite.Connection) -> tuple[list, dict[str, str]]:
    """pending(비dormant) 큐 행 전체 + 그중 `kream_products` 재대조로 즉시
    확정 가능한 항목({model_number: product_id}) 을 네트워크 0 으로 계산.

    `local_rematch`(적용/미리보기 공용)과 `build_dry_run_report`(미리보기
    전용, DB 미변경) 양쪽에서 재사용 — 로직 중복 방지.
    """
    cursor = await db.execute(
        "SELECT model_number FROM kream_collect_queue WHERE status = 'pending' AND dormant = 0"
    )
    pending_rows = await cursor.fetchall()
    if not pending_rows:
        return [], {}

    kp_cursor = await db.execute(
        "SELECT product_id, model_number FROM kream_products WHERE model_number != ''"
    )
    kp_rows = await kp_cursor.fetchall()
    key_to_pid: dict[str, str] = {}
    for r in kp_rows:
        key = canonical_model_key(r["model_number"])
        if key:
            key_to_pid.setdefault(key, r["product_id"])

    matches: dict[str, str] = {}
    for row in pending_rows:
        model_number = row["model_number"]
        key = canonical_model_key(model_number)
        pid = key_to_pid.get(key)
        if pid:
            matches[model_number] = pid

    return pending_rows, matches


async def local_rematch(db: aiosqlite.Connection, *, dry_run: bool = False) -> dict:
    """큐의 pending(비dormant) 항목을 현 `kream_products` 와 재대조.

    검색 호출 없이(네트워크 0) 이미 등재된 항목을 `found` 로 정리한다.
    `dry_run=True` 면 집계만 하고 DB 를 전혀 바꾸지 않는다(부작용 0 원칙 —
    dry-run 이 실제로는 상태를 바꿔버리면 "미리보기"라는 이름과 어긋난다).
    미매칭 행도 `canonical_model_key` 를 채워둬(dry_run=False 한정) 이후 검색
    dedup 단계에서 재계산 없이 바로 쓸 수 있게 한다.
    """
    pending_rows, matches = await _classify_local_matches(db)
    if not pending_rows:
        return {"checked": 0, "found": 0}

    if not dry_run:
        for row in pending_rows:
            model_number = row["model_number"]
            key = canonical_model_key(model_number)
            pid = matches.get(model_number)
            if pid:
                await db.execute(
                    """
                    UPDATE kream_collect_queue SET
                        status = 'found', found_product_id = ?, canonical_model_key = ?,
                        last_attempt_at = CURRENT_TIMESTAMP
                    WHERE model_number = ?
                    """,
                    (pid, key, model_number),
                )
            else:
                await db.execute(
                    "UPDATE kream_collect_queue SET canonical_model_key = ? WHERE model_number = ?",
                    (key, model_number),
                )
        await db.commit()

    return {"checked": len(pending_rows), "found": len(matches)}


# ─── 우선순위 정렬 + canonical key dedup ────────────────────────────────────

SQL_SEARCH_TARGETS = """
    SELECT q.model_number, q.canonical_model_key, q.attempts, q.added_at,
           EXISTS(
               SELECT 1 FROM retail_products rp
               WHERE rp.model_number = q.model_number
                  OR rp.model_number = q.canonical_model_key
           ) AS has_retail_evidence
    FROM kream_collect_queue q
    WHERE q.status = 'pending'
      AND q.dormant = 0
      AND (q.next_search_at IS NULL OR q.next_search_at <= datetime('now'))
    ORDER BY has_retail_evidence DESC, q.added_at DESC
"""


async def fetch_search_targets(db: aiosqlite.Connection, *, limit: int | None = None) -> list:
    """검색 대상 조회 — ① retail 증거 있는 모델 ② 최근 추가·갱신 ③ 오래된 backlog.

    ORDER BY has_retail_evidence DESC, added_at DESC 하나로 세 우선순위를
    동시에 만족한다: 최근순 정렬이 자연스럽게 가장 오래된 backlog를 맨 뒤에
    둔다. dormant/미도래 재시도(next_search_at)는 WHERE 절에서 이미 제외.
    """
    sql = SQL_SEARCH_TARGETS
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    cursor = await db.execute(sql)
    return await cursor.fetchall()


@dataclass(frozen=True)
class SearchTarget:
    """canonical_model_key 로 묶인 중복 큐 행 그룹 — 대표 1행만 검색."""

    canonical_key: str
    representative_model_number: str
    group_model_numbers: tuple[str, ...]


def group_search_targets(rows) -> list[SearchTarget]:
    """`fetch_search_targets` 결과를 canonical_model_key 기준으로 그룹핑.

    입력 rows 가 이미 우선순위 정렬돼 있으므로, 각 key 의 첫 등장 행이
    대표(최우선 순위)가 되고 group 처리 순서도 원래 우선순위를 보존한다.
    """
    order: list[str] = []
    groups: dict[str, list[str]] = {}
    for row in rows:
        model_number = row["model_number"]
        key = row["canonical_model_key"] or canonical_model_key(model_number)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(model_number)

    return [
        SearchTarget(
            canonical_key=key,
            representative_model_number=groups[key][0],
            group_model_numbers=tuple(groups[key]),
        )
        for key in order
    ]


# ─── 상태 분류 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassifyResult:
    """단일 검색 결과 — 상태 3종 + 발견 시 product_id + 즉시중단 여부."""

    state: str
    product_id: str = ""
    error: str = ""
    immediate_stop: bool = False


def _classify_search_result(
    status: int, items: list[dict] | None, target_key: str
) -> ClassifyResult:
    """HTTP 상태 + 검색 결과 리스트 → 상태 3종 분류 (순수 함수, mock 친화적).

    403/429 는 즉시 종료(재시도 금지)가 스펙이므로 RETRYABLE + immediate_stop.
    5xx/timeout(status=0)/파싱실패(items=None)는 재시도 대상이지만 attempts
    는 늘리지 않는다(transport 실패 ≠ "검색했는데 없더라"). 정상 응답에서
    canonical key 가 정확히 일치하는 항목이 없으면 NO_MATCH.
    """
    if status in _BLOCK_STATUSES:
        return ClassifyResult(RETRYABLE, error=f"http_{status}", immediate_stop=True)
    if status == 0 or 500 <= status < 600:
        return ClassifyResult(RETRYABLE, error=("timeout" if status == 0 else f"http_{status}"))
    if not (200 <= status < 300):
        return ClassifyResult(RETRYABLE, error=f"http_{status}")
    if items is None:
        return ClassifyResult(RETRYABLE, error="parse_fail")

    for item in items:
        if canonical_model_key(str(item.get("model_number", ""))) == target_key:
            return ClassifyResult(FOUND, product_id=str(item.get("product_id", "")))
    return ClassifyResult(NO_MATCH)


# ─── 실호출 (mock 대상 — 2-2 TDD 스코프는 실호출 0) ────────────────────────


async def _fetch_search_raw(model_number: str) -> tuple[int, list[dict] | None]:
    """크림 검색 1회 시도 — `kream_crawler.fetch_search_status()`(2-2r F1, kream.py
    공개 API) 위임.

    `kream_crawler._request()`는 403 시 쿠키 재초기화용 미계측 GET 을 추가로
    보내고 429 는 지수 백오프로 반복 sleep 하며, 결국 403/429/5xx/timeout
    전부를 상태코드 없이 None 으로 뭉갠다 — 백그라운드 배치의 "차단 1회 =
    즉시 전면 중단"(2-0) 계약이 이 경로로는 성립하지 않는다(리뷰 F1). 그래서
    `fetch_screens_status()`(2-1r F3)와 동일한 정신으로 kream.py 에 검색 전용
    단일시도 공개 API 를 추가했고, 이 함수는 그 결과를 그대로 전달한다 —
    private 파싱 헬퍼(`_extract_page_data`/`_extract_listing_products`)에
    더 이상 직접 의존하지 않는다.

    ⚠️ 이 함수는 2-2/2-2r 테스트 전부에서 monkeypatch 로 대체된다(실호출 0).
    실제 네트워크 경로 검증은 조각 2-4(라이브 카나리) 스코프.
    """
    return await kream_crawler.fetch_search_status(model_number, purpose=current_purpose())


# ─── DB 상태 반영 (idempotent UPDATE, 그룹 전체 반영) ──────────────────────


async def _apply_result(
    db: aiosqlite.Connection, target: SearchTarget, result: ClassifyResult
) -> None:
    """분류 결과를 canonical_model_key 그룹 전체 행에 반영.

    FOUND: 그룹 전 행을 found 로 정리. NO_MATCH: 각 행 자신의 attempts+1을
    기준으로 재검색 스케줄 계산(행별 attempts 이력이 달라도 정확). RETRYABLE:
    attempts 는 손대지 않고 next_search_at 만 짧게 연기.
    """
    key = target.canonical_key
    if result.state == FOUND:
        await db.execute(
            """
            UPDATE kream_collect_queue SET
                status = 'found', found_product_id = ?,
                last_search_at = CURRENT_TIMESTAMP, last_attempt_at = CURRENT_TIMESTAMP
            WHERE canonical_model_key = ?
            """,
            (result.product_id, key),
        )
    elif result.state == NO_MATCH:
        await db.execute(
            """
            UPDATE kream_collect_queue SET
                attempts = attempts + 1,
                last_search_at = CURRENT_TIMESTAMP, last_attempt_at = CURRENT_TIMESTAMP,
                next_search_at = CASE
                    WHEN attempts + 1 >= ? THEN NULL
                    WHEN attempts + 1 = 2 THEN datetime('now', '+30 days')
                    ELSE datetime('now', '+7 days')
                END,
                dormant = CASE WHEN attempts + 1 >= ? THEN 1 ELSE 0 END
            WHERE canonical_model_key = ?
            """,
            (DORMANT_AT_ATTEMPTS, DORMANT_AT_ATTEMPTS, key),
        )
    else:  # RETRYABLE
        await db.execute(
            """
            UPDATE kream_collect_queue SET
                last_search_at = CURRENT_TIMESTAMP,
                next_search_at = datetime('now', ?)
            WHERE canonical_model_key = ?
            """,
            (f"+{TRANSPORT_RETRY_BACKOFF_HOURS} hours", key),
        )
    await db.commit()


# ─── 단일 그룹 처리 (2-0 배선) ──────────────────────────────────────────────


async def _process_group(db: aiosqlite.Connection, target: SearchTarget) -> ClassifyResult:
    """acquire_background 경유 대표 1회 검색 → 분류 → report_block/success → DB 반영.

    `KreamCircuitTripped`/`KreamBackgroundBudgetExceeded`/`KreamBudgetExceeded`
    는 그대로 전파 — `run_batch` 가 잡아 배치를 종료시킨다(bootstrap 2-1 관례).
    """
    async with acquire_background(PURPOSE):
        status, items = await _fetch_search_raw(target.representative_model_number)

    result = _classify_search_result(status, items, target.canonical_key)

    if status in _BLOCK_STATUSES:
        await report_block(status, path=SEARCH_PATH)
    elif result.state == RETRYABLE:
        await report_block(status, path=SEARCH_PATH)
    else:
        report_success(SEARCH_PATH)

    await _apply_result(db, target, result)
    return result


# ─── 배치 실행 ──────────────────────────────────────────────────────────────


@dataclass
class RunSummary:
    counts: dict = field(default_factory=lambda: {FOUND: 0, NO_MATCH: 0, RETRYABLE: 0})
    search_calls_made: int = 0
    stopped_reason: str | None = None


async def run_batch(
    db: aiosqlite.Connection, targets: list[SearchTarget], *, run_cap: int
) -> RunSummary:
    """그룹 단위 순차 처리. 서킷/예산/차단(403·429)/run_cap 시 즉시 중단."""
    summary = RunSummary()

    for target in targets:
        if summary.search_calls_made >= run_cap:
            summary.stopped_reason = "run_budget_exhausted"
            break

        try:
            result = await _process_group(db, target)
        except KreamCircuitTripped:
            summary.stopped_reason = "circuit_tripped"
            break
        except KreamBackgroundBudgetExceeded:
            summary.stopped_reason = "budget_exhausted"
            break
        except KreamBudgetExceeded:
            summary.stopped_reason = "kream_hard_cap"
            break

        summary.search_calls_made += 1
        summary.counts[result.state] += len(target.group_model_numbers)

        if result.immediate_stop:
            summary.stopped_reason = f"blocked_{result.error}"
            break

    return summary


# ─── 예산 (백그라운드 배분의 20%) ───────────────────────────────────────────


async def compute_run_budget(budget_share: float = DEFAULT_BUDGET_SHARE) -> int:
    """이번 런 상한 = floor(현재 백그라운드 허용치 * budget_share).

    bootstrap(2-1)이 80% 를 쓰는 전제하에 이 배치는 나머지 20% 만 쓴다
    (계획서 "첫 500건 표본" 배분 재조정 근거).
    """
    allowance = await background_allowance()
    return math.floor(allowance * budget_share)


# ─── dry-run (네트워크 0) ───────────────────────────────────────────────────


async def build_dry_run_report(
    db: aiosqlite.Connection, *, budget_share: float = DEFAULT_BUDGET_SHARE
) -> dict:
    """dry-run 확장 출력 계산 — 네트워크 호출 0 (DB 조회 + 순수 계산만)."""
    local = await local_rematch(db, dry_run=True)

    # dry-run 은 DB 를 바꾸지 않으므로 fetch_search_targets 는 여전히 로컬
    # 재대조로 소거될 행까지 pending 으로 돌려준다 — "재대조 이후 실제 검색
    # 대상"을 미리보기하려면 그 행들을 파이썬 레벨에서 제외해야 한다.
    _, would_match = await _classify_local_matches(db)
    rows = await fetch_search_targets(db)
    rows = [r for r in rows if r["model_number"] not in would_match]
    targets = group_search_targets(rows)

    usage = await get_usage()
    soft_cap = await current_soft_cap()
    allowance = await background_allowance()
    run_cap = math.floor(allowance * budget_share)

    return {
        "local_checked": local["checked"],
        "local_would_find": local["found"],
        "pending_after_local": local["checked"] - local["found"],
        "search_targets_unique": len(targets),
        "search_targets_rows": sum(len(t.group_model_numbers) for t in targets),
        "kream_used_24h": usage["used"],
        "kream_hard_cap": usage["cap"],
        "kream_hard_remaining": usage["remaining"],
        "bg_soft_cap": soft_cap,
        "bg_soft_remaining": allowance,
        "run_budget_share": budget_share,
        "run_cap": run_cap,
    }


# ─── CLI ────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="크림 collect_queue 드레인 배치 (조각 2-2)",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="로컬 재대조 예상치 + 검색 대상/예산만 출력. 네트워크 호출 0.")
    p.add_argument("--budget-share", type=float, default=DEFAULT_BUDGET_SHARE,
                   help=f"백그라운드 배분 중 이번 런 상한 비율 (기본 {DEFAULT_BUDGET_SHARE}).")
    p.add_argument("--limit", type=int, default=None,
                   help="디버그용. 검색 대상 처음 N그룹만 조회.")
    return p.parse_args()


async def main() -> int:
    args = _parse_args()

    db_manager = Database(settings.db_path)
    await db_manager.connect()
    db = db_manager.db

    try:
        if args.dry_run:
            report = await build_dry_run_report(db, budget_share=args.budget_share)
            print("[drain-queue] DRY RUN (2-0 배선, 네트워크 0)")
            print(
                f"  로컬 재대조         : 확인 {report['local_checked']:,} / "
                f"검색없이 발견 {report['local_would_find']:,} "
                f"(재대조 후 잔여 {report['pending_after_local']:,})"
            )
            print(
                f"  검색 대상(dedup 후) : {report['search_targets_unique']:,}그룹 "
                f"(행 {report['search_targets_rows']:,})"
            )
            print(
                f"  KREAM 24h 사용량    : {report['kream_used_24h']:,} / "
                f"{report['kream_hard_cap']:,} (하드캡 잔여 {report['kream_hard_remaining']:,})"
            )
            print(
                f"  백그라운드 소프트캡 : {report['bg_soft_cap']:,} "
                f"(이번 순간 잔여 {report['bg_soft_remaining']:,})"
            )
            print(
                f"  이번 런 예산 상한   : {report['run_cap']:,} "
                f"(백그라운드 배분의 {report['run_budget_share']:.0%})"
            )
            print()
            print("  실 실행 시: 위 인자에서 --dry-run 빼고 재실행")
            return 0

        local = await local_rematch(db, dry_run=False)
        print(
            f"[drain-queue] 로컬 재대조 | 확인 {local['checked']:,} | "
            f"검색없이 발견 {local['found']:,}"
        )

        rows = await fetch_search_targets(db, limit=args.limit)
        targets = group_search_targets(rows)
        if not targets:
            print("[drain-queue] 검색 대상 없음 — 종료")
            return 0

        run_cap = await compute_run_budget(args.budget_share)
        total_rows = sum(len(t.group_model_numbers) for t in targets)
        print(
            f"[drain-queue] 검색 대상 {len(targets):,}그룹 (행 {total_rows:,}) | "
            f"이번 런 예산 상한 {run_cap:,}회"
        )

        # 2-v(F3): 다른 백그라운드 배치(bootstrap_cold_volumes_light.py 등)가
        # 동시에 실행 중이면 페이서/예약분이 프로세스 경계를 넘어 공유되지
        # 않아 간격·예산 보장이 깨진다 — DB 잠금으로 동시 실행 자체를 거부한다.
        owner = f"drain-{os.getpid()}"
        try:
            async with batch_run_lock(owner):
                summary = await run_batch(db, targets, run_cap=run_cap)
        except KreamBatchLockHeld as exc:
            print(f"[drain-queue] 배치 잠금 보유 중 — 동시 실행 거부: {exc}")
            return 1

        print()
        print(
            f"[drain-queue] 종료 | 검색호출 {summary.search_calls_made:,} | "
            f"발견행 {summary.counts[FOUND]:,} / 미발견행 {summary.counts[NO_MATCH]:,} / "
            f"재시도대기행 {summary.counts[RETRYABLE]:,}"
        )
        if summary.search_calls_made:
            yield_rate = summary.counts[FOUND] / summary.search_calls_made
            print(
                f"[drain-queue] 수율(발견행/실검색호출) {yield_rate:.2%} — "
                "bootstrap 수율과 비교해 배분 재조정 근거로 기록"
            )
        if summary.stopped_reason:
            print(
                f"[drain-queue] 중단 사유: {summary.stopped_reason} — "
                "재실행 시 자동 resume (next_search_at 도래분만)"
            )
        return 0
    finally:
        await db_manager.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
