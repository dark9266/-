"""백그라운드 KREAM 예산 브로커 — 램프/예약분/서킷/통합진입점 테스트 (조각 2-0).

기존 kream_api_calls 스키마(test_dashboard_health.py 관례)를 임시 DB 에 만들고
settings.db_path 를 몽키패치한다. 페이서는 실제 sleep 이 걸리지 않도록 스파이로
교체(monkeypatch kream_budget.get_pacer) — 예산/서킷 판정 자체는 실제 시간과 무관.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core import kream_budget as kb

pytestmark = pytest.mark.asyncio


def _create_kream_api_calls(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kream_api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            endpoint TEXT NOT NULL,
            method TEXT NOT NULL,
            status INTEGER,
            latency_ms INTEGER,
            purpose TEXT
        );
        """
    )
    conn.commit()


def _insert_calls_last_24h(db_path: str, n: int) -> None:
    """최근 24h 이내 타임스탬프로 n 건 빠르게 삽입 (recursive CTE)."""
    if n <= 0:
        return
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"""
        WITH RECURSIVE seq(x) AS (
            SELECT 1
            UNION ALL
            SELECT x + 1 FROM seq WHERE x < {n}
        )
        INSERT INTO kream_api_calls (ts, endpoint, method, status, latency_ms, purpose)
        SELECT datetime('now'), '/test', 'GET', 200, 10, 'test' FROM seq
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _reset_bg_circuit_counters():
    """모듈 전역 연속실패 카운터 — 테스트 간 누적되지 않도록 매번 리셋."""
    kb._bg_consecutive_failures.clear()
    yield
    kb._bg_consecutive_failures.clear()


@pytest.fixture
def bg_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bg_budget.db"
    conn = sqlite3.connect(str(db_path))
    _create_kream_api_calls(conn)
    conn.close()

    from src.config import settings

    monkeypatch.setattr(settings, "db_path", str(db_path), raising=True)
    yield str(db_path)


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


# ---------------------------------------------------------------------------
# 램프 소프트캡 고정값
# ---------------------------------------------------------------------------


async def test_ramp_soft_cap_fixed_values():
    assert kb.ramp_soft_cap("canary") == 100
    assert kb.ramp_soft_cap("day1") == 500
    assert kb.ramp_soft_cap("day2") == 1000
    assert kb.ramp_soft_cap("ramp2000") == 2000
    assert kb.ramp_soft_cap("ramp3000") == 3000
    # 알 수 없는 단계 — 가장 보수적인 canary 로 취급
    assert kb.ramp_soft_cap("unknown_stage") == 100


async def test_default_stage_is_canary(bg_db):
    state = await kb.get_ramp_state()
    assert state["stage"] == "canary"
    assert state["circuit_tripped"] is False
    assert state["manual_resume_required"] is False


async def test_advance_ramp_stage_moves_forward_and_persists(bg_db):
    assert await kb.advance_ramp_stage() == "day1"
    assert await kb.advance_ramp_stage() == "day2"
    assert await kb.advance_ramp_stage() == "ramp2000"
    assert await kb.advance_ramp_stage() == "ramp3000"
    # 최종 단계에서는 그대로 유지 (에러 없이 idempotent)
    assert await kb.advance_ramp_stage() == "ramp3000"

    state = await kb.get_ramp_state()
    assert state["stage"] == "ramp3000"


async def test_advance_ramp_stage_blocked_while_circuit_tripped(bg_db):
    await kb.report_block(403)
    with pytest.raises(kb.KreamCircuitTripped):
        await kb.advance_ramp_stage()


# ---------------------------------------------------------------------------
# 예약분 — 하드캡/라이브 예약분 침범 불가
# ---------------------------------------------------------------------------


async def test_background_allowance_baseline_uses_soft_cap(bg_db):
    # used=0 → min(soft=100, 10000-0-1000=9000) = 100
    assert await kb.background_allowance() == 100


async def test_background_allowance_clamps_to_zero_near_hard_cap(bg_db):
    # 24h 사용량 9,100 → min(soft, 10000-9100-1000) = min(soft, -100) → 0
    _insert_calls_last_24h(bg_db, 9100)
    allowance = await kb.background_allowance()
    assert allowance == 0


async def test_background_allowance_clamp_dominates_regardless_of_soft_cap(bg_db):
    # 소프트캡을 최대(ramp3000=3000)까지 올려도 예약분 침범 계산이 여전히 지배적
    for _ in range(4):
        await kb.advance_ramp_stage()
    assert await kb.current_soft_cap() == 3000

    _insert_calls_last_24h(bg_db, 9100)
    allowance = await kb.background_allowance()
    assert allowance == 0


async def test_background_allowance_partial_soft_cap_limited(bg_db):
    # used=8500 → reserved_room = 10000-8500-1000 = 500 → min(soft=100, 500) = 100
    _insert_calls_last_24h(bg_db, 8500)
    assert await kb.background_allowance() == 100


# ---------------------------------------------------------------------------
# 서킷브레이커
# ---------------------------------------------------------------------------


async def test_report_block_403_trips_with_manual_resume_required(bg_db):
    await kb.report_block(403)
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True
    assert state["manual_resume_required"] is True
    assert "403" in state["circuit_reason"]
    assert await kb.is_circuit_tripped() is True


async def test_report_block_429_trips_same_as_403(bg_db):
    await kb.report_block(429)
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True
    assert state["manual_resume_required"] is True


async def test_report_block_5xx_below_threshold_does_not_trip(bg_db):
    await kb.report_block(500, path="p1")
    await kb.report_block(502, path="p1")
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is False


async def test_report_block_5xx_streak_of_three_trips_circuit(bg_db):
    await kb.report_block(500, path="p1")
    await kb.report_block(500, path="p1")
    await kb.report_block(503, path="p1")
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True
    assert "p1" in state["circuit_reason"]


async def test_report_block_5xx_streak_is_per_path(bg_db):
    await kb.report_block(500, path="p1")
    await kb.report_block(500, path="p1")
    await kb.report_block(500, path="p2")  # 다른 path — p1 카운트에 안 섞임
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is False


async def test_report_success_resets_streak_counter(bg_db):
    await kb.report_block(500, path="p1")
    await kb.report_block(500, path="p1")
    kb.report_success("p1")
    await kb.report_block(500, path="p1")  # 리셋됐으므로 이번이 1회째
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is False


async def test_manual_resume_clears_state_and_counters(bg_db, spy_pacer):
    await kb.report_block(403)
    assert await kb.is_circuit_tripped() is True

    await kb.manual_resume()
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is False
    assert state["circuit_reason"] is None
    assert state["manual_resume_required"] is False

    # 재개 후에는 acquire_background 정상 동작
    async with kb.acquire_background("test_purpose"):
        pass
    assert spy_pacer.wait_turn_calls == 1


async def test_manual_resume_required_only_cleared_by_manual_resume(bg_db):
    """report_success 는 카운터만 리셋할 뿐 이미 트립된 서킷을 풀지 않는다."""
    await kb.report_block(403)
    kb.report_success("default")
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True


# ---------------------------------------------------------------------------
# acquire_background — 통합 진입점
# ---------------------------------------------------------------------------


async def test_acquire_background_raises_when_circuit_tripped(bg_db, spy_pacer):
    await kb.report_block(403)
    with pytest.raises(kb.KreamCircuitTripped):
        async with kb.acquire_background("bootstrap_light"):
            pass
    # 서킷 거부는 페이서 소비 전에 즉시 일어남
    assert spy_pacer.wait_turn_calls == 0


async def test_acquire_background_raises_when_budget_exhausted(bg_db, spy_pacer):
    _insert_calls_last_24h(bg_db, 9100)
    with pytest.raises(kb.KreamBackgroundBudgetExceeded):
        async with kb.acquire_background("bootstrap_light"):
            pass
    assert spy_pacer.wait_turn_calls == 0


async def test_acquire_background_tags_purpose(bg_db, spy_pacer):
    async with kb.acquire_background("bootstrap_light"):
        assert kb.current_purpose() == "bootstrap_light"
    # 블록을 벗어나면 태그 원복
    assert kb.current_purpose() == "manual"


async def test_acquire_background_retry_consumes_pacer_each_time(bg_db, spy_pacer):
    """재시도(2회 연속 acquire)가 페이서를 우회하지 못하고 매번 소비한다."""
    async with kb.acquire_background("bootstrap_light"):
        pass
    async with kb.acquire_background("bootstrap_light"):
        pass
    assert spy_pacer.wait_turn_calls == 2
