"""drain_collect_queue 회귀 테스트 (조각 2-2).

전부 mock — 실 크림 호출 0 (라이브는 2-4). `settings.db_path` 를 tmp_path 파일로
몽키패치해 `src.core.kream_budget`(2-0 브로커) 가 같은 물리 DB 파일을 참조하게
한다 — `acquire_background`/`report_block` 등은 자체적으로 `async_connect`를
새로 열기 때문에 `:memory:` 로는 상태가 공유되지 않는다(test_bootstrap_cold_
volumes_light.py 관례와 동일).
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

# ⚠️ 스크립트 모듈은 임포트 시점에 `load_dotenv()`를 호출한다 — 2-1 테스트
# 파일과 동일한 이유로 세션 최초 임포트 시 no-op 처리 (test_bootstrap_cold_
# volumes_light.py 관례 참조).
with patch("dotenv.load_dotenv", lambda *a, **kw: None):
    import scripts.drain_collect_queue as drain

import src.core.kream_budget as kb
from src.crawlers.kream import kream_crawler
from src.models.database import Database

# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bg_circuit_counters():
    kb._bg_consecutive_failures.clear()
    kb._batch_lock_lost = False
    yield
    kb._bg_consecutive_failures.clear()
    kb._batch_lock_lost = False


@pytest.fixture
async def db(tmp_path, monkeypatch):
    db_path = tmp_path / "drain_test.db"
    from src.config import settings

    monkeypatch.setattr(settings, "db_path", str(db_path), raising=True)

    db_manager = Database(str(db_path))
    await db_manager.connect()
    yield db_manager.db
    await db_manager.close()


class _SpyPacer:
    def __init__(self) -> None:
        self.wait_turn_calls = 0

    async def wait_turn(self) -> float:
        self.wait_turn_calls += 1
        return 0.0


@pytest.fixture
def spy_pacer(monkeypatch):
    spy = _SpyPacer()
    monkeypatch.setattr(kb, "get_pacer", lambda: spy)
    return spy


async def _insert_queue(
    db, model_number: str, *, attempts: int = 0, added_at: str | None = None,
    status: str = "pending", dormant: int = 0,
) -> None:
    await db.execute(
        """
        INSERT INTO kream_collect_queue
            (model_number, attempts, status, dormant, added_at)
        VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
        """,
        (model_number, attempts, status, dormant, added_at),
    )
    await db.commit()


async def _insert_kream_product(db, product_id: str, model_number: str) -> None:
    await db.execute(
        "INSERT INTO kream_products (product_id, name, model_number) VALUES (?, ?, ?)",
        (product_id, f"상품 {product_id}", model_number),
    )
    await db.commit()


async def _insert_retail(db, source: str, product_id: str, model_number: str) -> None:
    await db.execute(
        "INSERT INTO retail_products (source, product_id, name, model_number) "
        "VALUES (?, ?, 'x', ?)",
        (source, product_id, model_number),
    )
    await db.commit()


async def _get_queue_row(db, model_number: str) -> dict:
    cursor = await db.execute(
        "SELECT * FROM kream_collect_queue WHERE model_number = ?", (model_number,)
    )
    row = await cursor.fetchone()
    return dict(row)


def _search_items(product_id: str, model_number: str) -> list[dict]:
    return [{"product_id": product_id, "model_number": model_number, "name": "x"}]


# ---------------------------------------------------------------------------
# 1. canonical_model_key + 로컬 재대조 (네트워크 0)
# ---------------------------------------------------------------------------


def test_canonical_model_key_normalizes_case_space_hyphen():
    assert drain.canonical_model_key("dq 8423-100") == drain.canonical_model_key("DQ8423100")


async def test_local_rematch_marks_found_without_search(db):
    await _insert_kream_product(db, "P1", "DQ8423-100")
    await _insert_queue(db, "DQ8423 100")

    result = await drain.local_rematch(db, dry_run=False)

    assert result == {"checked": 1, "found": 1}
    row = await _get_queue_row(db, "DQ8423 100")
    assert row["status"] == "found"
    assert row["found_product_id"] == "P1"


async def test_local_rematch_leaves_unmatched_pending(db):
    await _insert_kream_product(db, "P1", "DQ8423-100")
    await _insert_queue(db, "ZZ9999-000")

    result = await drain.local_rematch(db, dry_run=False)

    assert result == {"checked": 1, "found": 0}
    row = await _get_queue_row(db, "ZZ9999-000")
    assert row["status"] == "pending"
    # canonical_model_key 는 검색 dedup 을 위해 미매칭 행에도 채워둔다
    assert row["canonical_model_key"]


async def test_local_rematch_dry_run_does_not_mutate_db(db):
    await _insert_kream_product(db, "P1", "DQ8423-100")
    await _insert_queue(db, "DQ8423-100")

    result = await drain.local_rematch(db, dry_run=True)

    assert result == {"checked": 1, "found": 1}
    row = await _get_queue_row(db, "DQ8423-100")
    assert row["status"] == "pending"  # dry-run — 부작용 0
    assert row["found_product_id"] in ("", None)


async def test_local_rematch_excludes_dormant(db):
    await _insert_kream_product(db, "P1", "DQ8423-100")
    await _insert_queue(db, "DQ8423-100", dormant=1)

    result = await drain.local_rematch(db, dry_run=False)
    assert result == {"checked": 0, "found": 0}


# ---------------------------------------------------------------------------
# 2. 항목당 검색 1회 제한 / canonical key 중복행 dedup
# ---------------------------------------------------------------------------


async def test_group_search_targets_dedups_by_canonical_key(db):
    await _insert_queue(db, "DQ8423-100", added_at="2026-01-01 00:00:00")
    await _insert_queue(db, "DQ8423 100", added_at="2026-01-02 00:00:00")
    await drain.local_rematch(db, dry_run=False)  # canonical_model_key 채우기

    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)

    assert len(targets) == 1
    assert set(targets[0].group_model_numbers) == {"DQ8423-100", "DQ8423 100"}


async def test_process_group_searches_representative_once(db, spy_pacer, monkeypatch):
    await _insert_queue(db, "DQ8423-100")
    await _insert_queue(db, "DQ8423 100")
    await drain.local_rematch(db, dry_run=False)

    fetch_mock = AsyncMock(return_value=(200, []))
    monkeypatch.setattr(drain, "_fetch_search_raw", fetch_mock)

    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)
    assert len(targets) == 1

    await drain._process_group(db, targets[0])

    assert fetch_mock.await_count == 1


async def test_apply_result_found_updates_all_group_rows(db):
    await _insert_queue(db, "DQ8423-100")
    await _insert_queue(db, "DQ8423 100")
    await drain.local_rematch(db, dry_run=False)

    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)
    result = drain.ClassifyResult(drain.FOUND, product_id="P9")
    await drain._apply_result(db, targets[0], result)

    for model in ("DQ8423-100", "DQ8423 100"):
        row = await _get_queue_row(db, model)
        assert row["status"] == "found"
        assert row["found_product_id"] == "P9"


# ---------------------------------------------------------------------------
# 3. attempts — 정상 no-match 만 증가 / transport 실패는 미증가
# ---------------------------------------------------------------------------


def test_classify_found_exact_match():
    items = _search_items("P1", "DQ8423-100")
    result = drain._classify_search_result(200, items, drain.canonical_model_key("DQ8423-100"))
    assert result.state == drain.FOUND
    assert result.product_id == "P1"


def test_classify_no_match_when_no_exact_hit():
    items = _search_items("P1", "DQ0000-000")
    result = drain._classify_search_result(200, items, drain.canonical_model_key("DQ8423-100"))
    assert result.state == drain.NO_MATCH


def test_classify_retryable_on_parse_failure():
    result = drain._classify_search_result(200, None, "KEY")
    assert result.state == drain.RETRYABLE
    assert result.error == "parse_fail"


def test_classify_retryable_on_timeout_status_zero():
    result = drain._classify_search_result(0, None, "KEY")
    assert result.state == drain.RETRYABLE
    assert result.error == "timeout"


def test_classify_retryable_on_5xx():
    result = drain._classify_search_result(503, None, "KEY")
    assert result.state == drain.RETRYABLE
    assert result.immediate_stop is False


def test_classify_retryable_immediate_stop_on_403():
    result = drain._classify_search_result(403, None, "KEY")
    assert result.state == drain.RETRYABLE
    assert result.immediate_stop is True


def test_classify_retryable_immediate_stop_on_429():
    result = drain._classify_search_result(429, None, "KEY")
    assert result.immediate_stop is True


async def test_apply_result_no_match_increments_attempts_only(db):
    await _insert_queue(db, "M1")
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    target = drain.group_search_targets(rows)[0]

    await drain._apply_result(db, target, drain.ClassifyResult(drain.NO_MATCH))

    row = await _get_queue_row(db, "M1")
    assert row["attempts"] == 1
    assert row["status"] == "pending"
    assert row["next_search_at"] is not None


async def test_apply_result_retryable_does_not_increment_attempts(db):
    await _insert_queue(db, "M1", attempts=1)
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    target = drain.group_search_targets(rows)[0]

    await drain._apply_result(
        db, target, drain.ClassifyResult(drain.RETRYABLE, error="http_500")
    )

    row = await _get_queue_row(db, "M1")
    assert row["attempts"] == 1  # 변화 없음
    assert row["next_search_at"] is not None  # 재시도는 연기됨


# ---------------------------------------------------------------------------
# 4. 재검색 스케줄 1→7d / 2→30d / 3→dormant, dormant 는 대상 제외
# ---------------------------------------------------------------------------


async def test_schedule_first_no_match_defers_seven_days(db):
    await _insert_queue(db, "M1", attempts=0)
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    target = drain.group_search_targets(rows)[0]
    await drain._apply_result(db, target, drain.ClassifyResult(drain.NO_MATCH))

    cursor = await db.execute(
        "SELECT julianday(next_search_at) - julianday('now') AS days_out, dormant "
        "FROM kream_collect_queue WHERE model_number = 'M1'"
    )
    row = await cursor.fetchone()
    assert 6 < row["days_out"] < 8
    assert row["dormant"] == 0


async def test_schedule_second_no_match_defers_thirty_days(db):
    await _insert_queue(db, "M1", attempts=1)
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    target = drain.group_search_targets(rows)[0]
    await drain._apply_result(db, target, drain.ClassifyResult(drain.NO_MATCH))

    cursor = await db.execute(
        "SELECT julianday(next_search_at) - julianday('now') AS days_out, dormant "
        "FROM kream_collect_queue WHERE model_number = 'M1'"
    )
    row = await cursor.fetchone()
    assert 29 < row["days_out"] < 31
    assert row["dormant"] == 0


async def test_schedule_third_no_match_goes_dormant(db):
    await _insert_queue(db, "M1", attempts=2)
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    target = drain.group_search_targets(rows)[0]
    await drain._apply_result(db, target, drain.ClassifyResult(drain.NO_MATCH))

    row = await _get_queue_row(db, "M1")
    assert row["attempts"] == 3
    assert row["dormant"] == 1


async def test_dormant_excluded_from_search_targets(db):
    await _insert_queue(db, "M1", attempts=3, dormant=1)
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    assert rows == []


# ---------------------------------------------------------------------------
# 5. 우선순위 정렬 — retail 증거 > 최근추가(오래된 backlog 는 뒤로)
# ---------------------------------------------------------------------------


async def test_priority_order_retail_evidence_first_then_recent(db):
    await _insert_queue(db, "OLD-NO-RETAIL", added_at="2026-01-01 00:00:00")
    await _insert_queue(db, "NEW-NO-RETAIL", added_at="2026-06-01 00:00:00")
    await _insert_queue(db, "OLD-WITH-RETAIL", added_at="2026-02-01 00:00:00")
    await _insert_retail(db, "musinsa", "r1", "OLD-WITH-RETAIL")
    await drain.local_rematch(db, dry_run=False)

    rows = await drain.fetch_search_targets(db)
    order = [r["model_number"] for r in rows]

    assert order == ["OLD-WITH-RETAIL", "NEW-NO-RETAIL", "OLD-NO-RETAIL"]


# ---------------------------------------------------------------------------
# 6. 403 즉시종료 / 하드캡 우아한 중단 / 서킷 트립 시 거부
# ---------------------------------------------------------------------------


async def test_run_batch_403_triggers_report_block_and_stops(db, spy_pacer, monkeypatch):
    await _insert_queue(db, "M1", added_at="2026-01-01 00:00:00")
    await _insert_queue(db, "M2", added_at="2026-01-02 00:00:00")
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)

    fetch_mock = AsyncMock(return_value=(403, None))
    monkeypatch.setattr(drain, "_fetch_search_raw", fetch_mock)

    summary = await drain.run_batch(db, targets, run_cap=100)

    assert summary.search_calls_made == 1
    assert summary.stopped_reason == "blocked_http_403"
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True


async def test_run_batch_refuses_when_circuit_already_tripped(db, spy_pacer):
    await _insert_queue(db, "M1")
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)

    await kb.report_block(403)  # 사전 트립

    summary = await drain.run_batch(db, targets, run_cap=100)

    assert summary.search_calls_made == 0
    assert summary.stopped_reason == "circuit_tripped"


async def test_run_batch_kream_hard_cap_stops_gracefully(db, spy_pacer, monkeypatch):
    await _insert_queue(db, "M1", added_at="2026-01-01 00:00:00")
    await _insert_queue(db, "M2", added_at="2026-01-02 00:00:00")
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)

    fetch_mock = AsyncMock(side_effect=kb.KreamBudgetExceeded("10000/10000"))
    monkeypatch.setattr(drain, "_fetch_search_raw", fetch_mock)

    summary = await drain.run_batch(db, targets, run_cap=100)

    assert summary.search_calls_made == 0
    assert summary.stopped_reason == "kream_hard_cap"
    assert fetch_mock.await_count == 1


async def test_e2e_kream_403_stops_run_batch_immediately(db, spy_pacer, monkeypatch):
    """2-2r F2 — `kream_crawler.fetch_search_status()`(실제 kream.py 공개 API)가
    403 을 돌려주면 `_fetch_search_raw`(내부 위임) → `_classify_search_result`
    → `run_batch` 까지 이어져 즉시 전면 중단(immediate_stop)되는지 e2e(mock).

    `drain._fetch_search_raw` 가 아니라 `kream_crawler.fetch_search_status` 를
    직접 패치해 실제 배선(2-2r F1)이 끊어지지 않았는지 확인한다.
    """
    await _insert_queue(db, "M1", added_at="2026-01-01 00:00:00")
    await _insert_queue(db, "M2", added_at="2026-01-02 00:00:00")
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)

    monkeypatch.setattr(
        kream_crawler, "fetch_search_status", AsyncMock(return_value=(403, None))
    )

    summary = await drain.run_batch(db, targets, run_cap=100)

    assert summary.search_calls_made == 1
    assert summary.stopped_reason == "blocked_http_403"
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True
    assert state["manual_resume_required"] is True


# ---------------------------------------------------------------------------
# 7. budget-share 상한 도달 → 우아한 종료 + 수율 요약
# ---------------------------------------------------------------------------


async def test_run_batch_stops_at_run_cap(db, spy_pacer, monkeypatch):
    await _insert_queue(db, "M1", added_at="2026-01-01 00:00:00")
    await _insert_queue(db, "M2", added_at="2026-01-02 00:00:00")
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)

    fetch_mock = AsyncMock(return_value=(200, []))
    monkeypatch.setattr(drain, "_fetch_search_raw", fetch_mock)

    summary = await drain.run_batch(db, targets, run_cap=1)

    assert summary.search_calls_made == 1
    assert summary.stopped_reason == "run_budget_exhausted"


async def test_run_batch_zero_run_cap_stops_immediately(db, spy_pacer, monkeypatch):
    await _insert_queue(db, "M1")
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)

    fetch_mock = AsyncMock(return_value=(200, []))
    monkeypatch.setattr(drain, "_fetch_search_raw", fetch_mock)

    summary = await drain.run_batch(db, targets, run_cap=0)

    assert summary.search_calls_made == 0
    assert summary.stopped_reason == "run_budget_exhausted"
    fetch_mock.assert_not_called()


async def test_compute_run_budget_applies_share(db):
    # `db` 픽스처로 settings.db_path 를 tmp 로 격리 — 예산 조회가 운영 DB 를
    # 읽지 않게 한다(전역 안전망이 2026-07-25 에 발견한 격리 누락).
    with patch.object(kb, "background_allowance", AsyncMock(return_value=100)):
        cap = await drain.compute_run_budget(budget_share=0.2)
    assert cap == 20


# ---------------------------------------------------------------------------
# 8. dry-run — 네트워크 0 + 수치
# ---------------------------------------------------------------------------


async def test_dry_run_report_zero_network_and_accurate(db, monkeypatch):
    fetch_mock = AsyncMock()
    monkeypatch.setattr(drain, "_fetch_search_raw", fetch_mock)

    await _insert_kream_product(db, "P1", "M1")
    await _insert_queue(db, "M1")  # 로컬 재대조로 소거될 예정
    await _insert_queue(db, "M2")
    await _insert_queue(db, "M2 ")  # M2 와 canonical key 동일 (dedup)

    report = await drain.build_dry_run_report(db, budget_share=0.2)

    assert report["local_checked"] == 3
    assert report["local_would_find"] == 1
    assert report["pending_after_local"] == 2
    assert report["search_targets_unique"] == 1  # M2/M2 dedup
    assert report["search_targets_rows"] == 2
    assert report["run_cap"] == 20  # canary soft cap 100 * 0.2
    fetch_mock.assert_not_called()

    # dry-run 은 부작용 0 — 로컬 재대조도 실제로 반영되지 않아야 함
    row = await _get_queue_row(db, "M1")
    assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# 9. 수율 요약 — found 행수 / 실 검색호출수
# ---------------------------------------------------------------------------


async def test_run_summary_yield_found_rows_over_search_calls(db, spy_pacer, monkeypatch):
    await _insert_queue(db, "M1", added_at="2026-01-01 00:00:00")
    await _insert_queue(db, "M1 ", added_at="2026-01-02 00:00:00")  # 같은 canonical key
    await _insert_queue(db, "M2", added_at="2026-01-03 00:00:00")
    await drain.local_rematch(db, dry_run=False)
    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)
    assert len(targets) == 2  # M1 그룹(2행) + M2(1행)

    async def _fake_fetch(model_number: str):
        if drain.canonical_model_key(model_number) == drain.canonical_model_key("M1"):
            return 200, _search_items("P1", "M1")
        return 200, []

    monkeypatch.setattr(drain, "_fetch_search_raw", AsyncMock(side_effect=_fake_fetch))

    summary = await drain.run_batch(db, targets, run_cap=100)

    assert summary.search_calls_made == 2
    assert summary.counts[drain.FOUND] == 2  # M1 그룹 2행 모두 found
    assert summary.counts[drain.NO_MATCH] == 1


# ---------------------------------------------------------------------------
# 10. 2-v F3 — 배치 동시 실행 거부 배선
#
# bootstrap 배치와 동시에 돌면 페이서/예약분 보장이 프로세스 경계를 넘지 못한다.
# main() 이 `batch_run_lock` 을 통과해야만 `run_batch` 에 들어가는지 고정.
# ---------------------------------------------------------------------------


def _one_target() -> "drain.SearchTarget":
    return drain.SearchTarget(
        canonical_key="M1", representative_model_number="M1", group_model_numbers=("M1",)
    )


async def test_main_refuses_to_run_while_other_batch_holds_lock(db, monkeypatch):
    await kb.try_acquire_batch_lock("bootstrap-999")

    fake_run = AsyncMock()
    monkeypatch.setattr(sys, "argv", ["drain_collect_queue.py"])
    monkeypatch.setattr(
        drain, "local_rematch", AsyncMock(return_value={"checked": 0, "found": 0})
    )
    monkeypatch.setattr(drain, "fetch_search_targets", AsyncMock(return_value=[]))
    monkeypatch.setattr(drain, "group_search_targets", lambda rows: [_one_target()])
    monkeypatch.setattr(drain, "compute_run_budget", AsyncMock(return_value=10))
    monkeypatch.setattr(drain, "run_batch", fake_run)

    rc = await drain.main()

    assert rc == 1
    fake_run.assert_not_awaited()


async def test_main_releases_lock_after_run(db, spy_pacer, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["drain_collect_queue.py"])
    monkeypatch.setattr(
        drain, "local_rematch", AsyncMock(return_value={"checked": 0, "found": 0})
    )
    monkeypatch.setattr(drain, "fetch_search_targets", AsyncMock(return_value=[]))
    monkeypatch.setattr(drain, "group_search_targets", lambda rows: [_one_target()])
    monkeypatch.setattr(drain, "compute_run_budget", AsyncMock(return_value=10))
    monkeypatch.setattr(drain, "run_batch", AsyncMock(return_value=drain.RunSummary()))
    monkeypatch.setattr(
        drain.kream_crawler, "ensure_session_ready", AsyncMock(return_value=200)
    )

    rc = await drain.main()

    assert rc == 0
    assert await kb.try_acquire_batch_lock("bootstrap-999") is True


async def test_run_batch_stops_when_batch_lock_lost(db, spy_pacer, monkeypatch):
    """2-vr F3-1 — 실행 중 잠금 상실이 감지되면 다음 검색 호출에서 우아하게 중단."""
    await _insert_queue(db, "M1")
    rows = await drain.fetch_search_targets(db)
    targets = drain.group_search_targets(rows)
    assert targets

    monkeypatch.setattr(kb, "_batch_lock_lost", True)
    fake_fetch = AsyncMock(return_value=(200, []))
    monkeypatch.setattr(drain, "_fetch_search_raw", fake_fetch)

    summary = await drain.run_batch(db, targets, run_cap=100)

    assert summary.stopped_reason == "batch_lock_lost"
    assert summary.search_calls_made == 0
    fake_fetch.assert_not_awaited()
    assert spy_pacer.wait_turn_calls == 0


# ---------------------------------------------------------------------------
# 2-v2 F2 — 세션 워밍
# ---------------------------------------------------------------------------


async def test_warm_session_goes_through_acquire_background(db, spy_pacer, monkeypatch):
    monkeypatch.setattr(
        drain.kream_crawler, "ensure_session_ready", AsyncMock(return_value=200)
    )

    assert await drain.warm_session() == 200
    assert spy_pacer.wait_turn_calls == 1


async def test_main_stops_without_batch_when_session_init_blocked(db, spy_pacer, monkeypatch):
    fake_run = AsyncMock()
    monkeypatch.setattr(sys, "argv", ["drain_collect_queue.py"])
    monkeypatch.setattr(
        drain, "local_rematch", AsyncMock(return_value={"checked": 0, "found": 0})
    )
    monkeypatch.setattr(drain, "fetch_search_targets", AsyncMock(return_value=[]))
    monkeypatch.setattr(drain, "group_search_targets", lambda rows: [_one_target()])
    monkeypatch.setattr(drain, "compute_run_budget", AsyncMock(return_value=10))
    monkeypatch.setattr(drain, "run_batch", fake_run)
    monkeypatch.setattr(
        drain.kream_crawler, "ensure_session_ready", AsyncMock(return_value=429)
    )

    rc = await drain.main()

    assert rc == 1
    fake_run.assert_not_awaited()
    assert await kb.is_circuit_tripped() is True
