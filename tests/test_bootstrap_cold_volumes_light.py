"""bootstrap_cold_volumes_light 정식화 회귀 테스트 (조각 2-1).

전부 mock — 실 크림 호출 0 (라이브는 2-4). `settings.db_path` 를 tmp_path 파일로
몽키패치해 `src.core.kream_budget`(2-0 브로커) 가 같은 물리 DB 파일을 참조하게
한다 — `acquire_background`/`report_block` 등은 자체적으로 `async_connect`를
새로 열기 때문에 `:memory:` 로는 상태가 공유되지 않는다(test_kream_bg_budget.py
관례와 동일).
"""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

import scripts.bootstrap_cold_volumes_light as boot
import src.core.kream_budget as kb
from src.models.database import Database

# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bg_circuit_counters():
    """모듈 전역 연속실패 카운터 — 테스트 간 누적 방지 (test_kream_bg_budget.py 관례)."""
    kb._bg_consecutive_failures.clear()
    yield
    kb._bg_consecutive_failures.clear()


@pytest.fixture
async def db(tmp_path, monkeypatch):
    db_path = tmp_path / "bootstrap_test.db"
    from src.config import settings

    monkeypatch.setattr(settings, "db_path", str(db_path), raising=True)

    db_manager = Database(str(db_path))
    await db_manager.connect()
    yield db_manager.db
    await db_manager.close()


class _SpyPacer:
    """acquire_background 가 호출하는 get_pacer() 자리에 주입 — 실 sleep 없음."""

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


async def _insert_kream_product(
    db, product_id: str, model_number: str = "M1", *, volume_attempt_count: int = 0,
) -> None:
    await db.execute(
        """
        INSERT INTO kream_products (product_id, name, model_number, volume_attempt_count)
        VALUES (?, ?, ?, ?)
        """,
        (product_id, f"상품 {product_id}", model_number, volume_attempt_count),
    )
    await db.commit()


async def _get_row(db, product_id: str) -> dict:
    cursor = await db.execute(
        "SELECT * FROM kream_products WHERE product_id = ?", (product_id,)
    )
    row = await cursor.fetchone()
    return dict(row)


def _screens_payload(sales_prices: list[int]) -> dict:
    """screens API 정상 응답 골격 — sales 리스트만 변주 (날짜는 항상 '방금').

    volume_7d 판정이 "지금 - N일" 윈도로 계산되므로, 고정된 과거 날짜를 쓰면
    테스트 실행 시점에 따라 7일 밖으로 밀려나 항상 실패할 수 있다.
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "transaction_history": {
            "sales": [
                {"price": p, "date_created": now, "product_option": {"key": "270"}}
                for p in sales_prices
            ],
            "asks": [],
            "bids": [],
        }
    }


# ---------------------------------------------------------------------------
# 1. 상태 4종 분기 (_classify_response)
# ---------------------------------------------------------------------------


def test_classify_success_positive():
    data = _screens_payload([100000, 105000])
    result = boot._classify_response(200, data, "P1")
    assert result.state == boot.SUCCESS_POSITIVE
    assert result.volume_7d > 0
    assert result.immediate_stop is False


def test_classify_success_zero_confirmed_empty_sales():
    """transaction_history 는 정상 파싱됐지만 sales 가 진짜로 빈 경우 — success_zero."""
    data = _screens_payload([])
    result = boot._classify_response(200, data, "P1")
    assert result.state == boot.SUCCESS_ZERO
    assert result.volume_7d == 0


def test_classify_retryable_on_parse_failure():
    """200 인데 transaction_history 자체를 못 찾음 — 구조 이상, retryable(파싱실패)."""
    data = {"unexpected": "shape"}
    result = boot._classify_response(200, data, "P1")
    assert result.state == boot.RETRYABLE
    assert result.error == "parse_fail"


def test_classify_quarantined_on_404():
    result = boot._classify_response(404, None, "P1")
    assert result.state == boot.QUARANTINED
    assert result.error == "http_404"


def test_classify_quarantined_on_410():
    result = boot._classify_response(410, None, "P1")
    assert result.state == boot.QUARANTINED


def test_classify_retryable_on_5xx():
    result = boot._classify_response(503, None, "P1")
    assert result.state == boot.RETRYABLE
    assert result.error == "http_503"
    assert result.immediate_stop is False


def test_classify_retryable_on_timeout_status_zero():
    result = boot._classify_response(0, None, "P1")
    assert result.state == boot.RETRYABLE
    assert result.error == "timeout"


def test_classify_retryable_immediate_stop_on_403():
    result = boot._classify_response(403, None, "P1")
    assert result.state == boot.RETRYABLE
    assert result.immediate_stop is True


def test_classify_retryable_immediate_stop_on_429():
    result = boot._classify_response(429, None, "P1")
    assert result.immediate_stop is True


# ---------------------------------------------------------------------------
# 2. last_volume_check 는 성공 2종에서만 갱신
# ---------------------------------------------------------------------------


async def test_apply_state_success_positive_updates_last_volume_check(db):
    await _insert_kream_product(db, "P1")
    result = boot.ClassifyResult(boot.SUCCESS_POSITIVE, volume_7d=3, volume_30d=10)
    await boot._apply_state(db, "P1", result)

    row = await _get_row(db, "P1")
    assert row["last_volume_check"] is not None
    assert row["volume_7d"] == 3
    assert row["refresh_tier"] == "warm"
    assert row["volume_attempt_count"] == 1
    assert row["next_volume_attempt_at"] is None
    assert row["last_volume_error"] is None


async def test_apply_state_success_zero_updates_last_volume_check(db):
    await _insert_kream_product(db, "P1")
    result = boot.ClassifyResult(boot.SUCCESS_ZERO, volume_7d=0, volume_30d=0)
    await boot._apply_state(db, "P1", result)

    row = await _get_row(db, "P1")
    assert row["last_volume_check"] is not None
    assert row["refresh_tier"] == "cold"


async def test_apply_state_retryable_does_not_touch_last_volume_check(db):
    await _insert_kream_product(db, "P1")
    result = boot.ClassifyResult(boot.RETRYABLE, error="parse_fail")
    await boot._apply_state(db, "P1", result)

    row = await _get_row(db, "P1")
    assert row["last_volume_check"] is None
    assert row["last_volume_attempt_at"] is not None
    assert row["volume_attempt_count"] == 1
    assert row["last_volume_error"] == "parse_fail"
    assert row["next_volume_attempt_at"] is not None


async def test_apply_state_quarantined_sets_next_attempt_far_future(db):
    await _insert_kream_product(db, "P1")
    result = boot.ClassifyResult(boot.QUARANTINED, error="http_404")
    await boot._apply_state(db, "P1", result)

    row = await _get_row(db, "P1")
    assert row["last_volume_check"] is None
    assert row["last_volume_error"] == "http_404"

    cursor = await db.execute(
        "SELECT julianday(next_volume_attempt_at) - julianday('now') AS days_out "
        "FROM kream_products WHERE product_id = ?",
        ("P1",),
    )
    days_out = (await cursor.fetchone())["days_out"]
    assert days_out > 80  # ~90일 뒤


# ---------------------------------------------------------------------------
# 3. lease — 동시 두 owner 못 잡음 / 만료 후 재취득
# ---------------------------------------------------------------------------


async def test_try_acquire_lease_blocks_second_owner_while_active(db):
    now = time.time()
    assert await boot.try_acquire_lease(db, "P1", "owner-a", 300, now=now) is True
    assert await boot.try_acquire_lease(db, "P1", "owner-b", 300, now=now + 1) is False


async def test_try_acquire_lease_allows_reacquire_by_same_owner(db):
    now = time.time()
    assert await boot.try_acquire_lease(db, "P1", "owner-a", 300, now=now) is True
    assert await boot.try_acquire_lease(db, "P1", "owner-a", 300, now=now + 1) is True


async def test_try_acquire_lease_allows_after_expiry(db):
    now = time.time()
    assert await boot.try_acquire_lease(db, "P1", "owner-a", 300, now=now) is True
    # owner-b 아직 만료 전 — 실패
    assert await boot.try_acquire_lease(db, "P1", "owner-b", 300, now=now + 100) is False
    # 300초 지나 만료 — owner-b 재취득 가능
    assert await boot.try_acquire_lease(db, "P1", "owner-b", 300, now=now + 301) is True


async def test_acquire_lease_chunk_same_chunk_two_owners(db):
    """같은 chunk(리스트) 를 두 owner 가 동시에 못 잡는다."""
    now = time.time()
    chunk = ["P1", "P2", "P3"]

    got_a = await boot.acquire_lease_chunk(db, chunk, "owner-a", 300, now=now)
    assert set(got_a) == {"P1", "P2", "P3"}

    got_b = await boot.acquire_lease_chunk(db, chunk, "owner-b", 300, now=now + 1)
    assert got_b == []

    # 만료 후 owner-b 재취득 가능
    got_b_after_expiry = await boot.acquire_lease_chunk(db, chunk, "owner-b", 300, now=now + 301)
    assert set(got_b_after_expiry) == {"P1", "P2", "P3"}


# ---------------------------------------------------------------------------
# 4. idempotent 재처리
# ---------------------------------------------------------------------------


async def test_process_one_idempotent_success_positive(db, spy_pacer, monkeypatch):
    await _insert_kream_product(db, "P1")
    payload = _screens_payload([100000, 100000, 100000, 100000, 100000])
    monkeypatch.setattr(boot, "_fetch_screens_raw", AsyncMock(return_value=(200, payload)))

    result1 = await boot._process_one(db, "P1")
    row1 = await _get_row(db, "P1")

    result2 = await boot._process_one(db, "P1")
    row2 = await _get_row(db, "P1")

    assert result1.state == result2.state == boot.SUCCESS_POSITIVE
    assert row1["volume_7d"] == row2["volume_7d"]
    assert row1["refresh_tier"] == row2["refresh_tier"]
    # attempt_count 는 매 처리마다 증가 — 재처리 자체는 안전(멱등 UPDATE, 값 붕괴 없음)
    assert row2["volume_attempt_count"] == row1["volume_attempt_count"] + 1


async def test_process_one_idempotent_quarantined(db, spy_pacer, monkeypatch):
    await _insert_kream_product(db, "P1")
    monkeypatch.setattr(boot, "_fetch_screens_raw", AsyncMock(return_value=(404, None)))

    await boot._process_one(db, "P1")
    row1 = await _get_row(db, "P1")
    await boot._process_one(db, "P1")
    row2 = await _get_row(db, "P1")

    assert row1["last_volume_error"] == row2["last_volume_error"] == "http_404"
    assert row1["last_volume_check"] is None
    assert row2["last_volume_check"] is None


# ---------------------------------------------------------------------------
# 5. 2-0 배선 — acquire_background 경유 / 403 즉시종료 / 서킷 트립 시 거부
# ---------------------------------------------------------------------------


async def test_process_one_goes_through_acquire_background(db, spy_pacer, monkeypatch):
    await _insert_kream_product(db, "P1")
    monkeypatch.setattr(
        boot, "_fetch_screens_raw", AsyncMock(return_value=(200, _screens_payload([])))
    )
    await boot._process_one(db, "P1")
    assert spy_pacer.wait_turn_calls == 1


async def test_run_batch_403_triggers_report_block_and_stops_immediately(
    db, spy_pacer, monkeypatch
):
    await _insert_kream_product(db, "P1")
    await _insert_kream_product(db, "P2", model_number="M2")

    fetch_mock = AsyncMock(return_value=(403, None))
    monkeypatch.setattr(boot, "_fetch_screens_raw", fetch_mock)

    summary = await boot.run_batch(db, ["P1", "P2"], owner="test-owner")

    assert summary.processed == 1  # P2 는 처리되지 않음(즉시 종료)
    assert summary.stopped_reason == "blocked_http_403"
    assert fetch_mock.await_count == 1

    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True
    assert state["manual_resume_required"] is True


async def test_run_batch_refuses_when_circuit_already_tripped(db, spy_pacer):
    await _insert_kream_product(db, "P1")
    await kb.report_block(403)  # 사전에 이미 트립된 상태로 시작

    summary = await boot.run_batch(db, ["P1"], owner="test-owner")

    assert summary.processed == 0
    assert summary.stopped_reason == "circuit_tripped"


async def test_run_batch_5xx_streak_of_three_trips_and_stops(db, spy_pacer, monkeypatch):
    for pid in ("P1", "P2", "P3", "P4"):
        await _insert_kream_product(db, pid, model_number=pid)

    fetch_mock = AsyncMock(return_value=(500, None))
    monkeypatch.setattr(boot, "_fetch_screens_raw", fetch_mock)

    summary = await boot.run_batch(db, ["P1", "P2", "P3", "P4"], owner="test-owner")

    # 3번째 5xx 에서 서킷이 트립되고, 4번째 acquire_background 가 즉시 거부
    assert summary.processed == 3
    assert summary.counts[boot.RETRYABLE] == 3
    assert summary.stopped_reason == "circuit_tripped"

    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True


async def test_run_batch_success_resets_streak_and_continues(db, spy_pacer, monkeypatch):
    for pid in ("P1", "P2", "P3"):
        await _insert_kream_product(db, pid, model_number=pid)

    # 500, 200(성공, 스트릭 리셋), 500 — 스트릭이 리셋되므로 트립 안 됨
    responses = [(500, None), (200, _screens_payload([])), (500, None)]
    fetch_mock = AsyncMock(side_effect=responses)
    monkeypatch.setattr(boot, "_fetch_screens_raw", fetch_mock)

    summary = await boot.run_batch(db, ["P1", "P2", "P3"], owner="test-owner")

    assert summary.processed == 3
    assert summary.stopped_reason is None
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is False


async def test_run_batch_budget_exhausted_stops(db, spy_pacer, monkeypatch):
    """백그라운드 소프트캡 소진 — acquire_background 가 즉시 거부.

    `acquire_background` 내부는 `kream_budget` 모듈 자신의 전역 이름으로
    `background_allowance` 를 참조하므로, `boot.background_allowance` 가
    아니라 `kb.background_allowance` 를 패치해야 실제로 적용된다.
    """
    await _insert_kream_product(db, "P1")
    monkeypatch.setattr(kb, "background_allowance", AsyncMock(return_value=0))

    summary = await boot.run_batch(db, ["P1"], owner="test-owner")

    assert summary.processed == 0
    assert summary.stopped_reason == "budget_exhausted"


# ---------------------------------------------------------------------------
# 6. fetch_targets — 대상 SQL
# ---------------------------------------------------------------------------


async def test_fetch_targets_excludes_already_checked(db):
    await _insert_kream_product(db, "P1", model_number="M1")
    await db.execute(
        "UPDATE kream_products SET last_volume_check = CURRENT_TIMESTAMP WHERE product_id = 'P1'"
    )
    await db.execute(
        "INSERT INTO retail_products (source, product_id, name, model_number) "
        "VALUES ('musinsa', 'r1', 'x', 'M1')"
    )
    await db.commit()

    rows = await boot.fetch_targets(db)
    assert [r["product_id"] for r in rows] == []


async def test_fetch_targets_excludes_future_retry(db):
    await _insert_kream_product(db, "P1", model_number="M1")
    await db.execute(
        "UPDATE kream_products SET next_volume_attempt_at = datetime('now', '+1 day') "
        "WHERE product_id = 'P1'"
    )
    await db.execute(
        "INSERT INTO retail_products (source, product_id, name, model_number) "
        "VALUES ('musinsa', 'r1', 'x', 'M1')"
    )
    await db.commit()

    rows = await boot.fetch_targets(db)
    assert [r["product_id"] for r in rows] == []


async def test_fetch_targets_includes_due_retry(db):
    await _insert_kream_product(db, "P1", model_number="M1")
    await db.execute(
        "UPDATE kream_products SET next_volume_attempt_at = datetime('now', '-1 hour') "
        "WHERE product_id = 'P1'"
    )
    await db.execute(
        "INSERT INTO retail_products (source, product_id, name, model_number) "
        "VALUES ('musinsa', 'r1', 'x', 'M1')"
    )
    await db.commit()

    rows = await boot.fetch_targets(db)
    assert [r["product_id"] for r in rows] == ["P1"]


async def test_fetch_targets_source_filter(db):
    await _insert_kream_product(db, "P1", model_number="M1")
    await _insert_kream_product(db, "P2", model_number="M2")
    await db.execute(
        "INSERT INTO retail_products (source, product_id, name, model_number) "
        "VALUES ('musinsa', 'r1', 'x', 'M1')"
    )
    await db.execute(
        "INSERT INTO retail_products (source, product_id, name, model_number) "
        "VALUES ('nike', 'r2', 'x', 'M2')"
    )
    await db.commit()

    rows = await boot.fetch_targets(db, sources=("nike",))
    assert [r["product_id"] for r in rows] == ["P2"]


# ---------------------------------------------------------------------------
# 7. dry-run — 네트워크 0 + 수치 정확성
# ---------------------------------------------------------------------------


async def test_dry_run_report_accuracy_and_zero_network(db, monkeypatch):
    fetch_mock = AsyncMock()
    monkeypatch.setattr(boot, "_fetch_screens_raw", fetch_mock)

    await _insert_kream_product(db, "P1", model_number="M1", volume_attempt_count=0)
    await _insert_kream_product(db, "P2", model_number="M2", volume_attempt_count=2)
    for pid, model in (("P1", "M1"), ("P2", "M2")):
        await db.execute(
            "INSERT INTO retail_products (source, product_id, name, model_number) "
            "VALUES ('musinsa', ?, 'x', ?)",
            (pid, model),
        )
    await db.execute(
        "INSERT INTO kream_collect_queue (model_number) VALUES ('M1')"
    )
    await db.commit()

    rows = await boot.fetch_targets(db)
    report = await boot.build_dry_run_report(db, rows)

    assert report["unique_targets"] == 2
    assert report["already_attempted_before"] == 1  # P2 만 volume_attempt_count>0
    assert report["calls_best_case"] == 2
    assert report["calls_expected"] == 3  # 2 + 1
    assert report["calls_worst_case"] == 5  # 2 + 1*3
    assert report["collect_queue_overlap"] == 1  # M1 만 겹침
    assert report["kream_used_24h"] == 0
    assert report["bg_soft_cap"] == 100  # canary 기본값
    fetch_mock.assert_not_called()


async def test_dry_run_reflects_kream_usage(db, monkeypatch, tmp_path):
    """kream_api_calls 에 기존 호출이 있으면 dry-run 수치에 반영."""
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "bootstrap_test.db"))
    conn.execute(
        "INSERT INTO kream_api_calls (ts, endpoint, method, status, latency_ms, purpose) "
        "VALUES (datetime('now'), '/x', 'GET', 200, 10, 'test')"
    )
    conn.commit()
    conn.close()

    rows = await boot.fetch_targets(db)
    report = await boot.build_dry_run_report(db, rows)
    assert report["kream_used_24h"] == 1
    assert report["kream_hard_remaining"] == report["kream_hard_cap"] - 1
