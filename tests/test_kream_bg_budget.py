"""백그라운드 KREAM 예산 브로커 — 램프/예약분/서킷/통합진입점 테스트 (조각 2-0).

기존 kream_api_calls 스키마(test_dashboard_health.py 관례)를 임시 DB 에 만들고
settings.db_path 를 몽키패치한다. 페이서는 실제 sleep 이 걸리지 않도록 스파이로
교체(monkeypatch kream_budget.get_pacer) — 예산/서킷 판정 자체는 실제 시간과 무관.
"""

from __future__ import annotations

import asyncio
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


def _insert_calls_last_24h(
    db_path: str, n: int, purpose: str = "test", *, age_hours: float = 0.0
) -> None:
    """`age_hours` 만큼 과거 타임스탬프로 n 건 빠르게 삽입 (recursive CTE).

    기본값(purpose="test", age_hours=0)은 "24h 이내 · 백그라운드 아님" 이라
    하드캡 계산에만 잡히고 소프트캡 소비에는 안 잡힌다(2-v F1).
    """
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
        SELECT datetime('now', ?), '/test', 'GET', 200, 10, ? FROM seq
        """,
        (f"-{age_hours} hours", purpose),
    )
    conn.commit()
    conn.close()


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    """`predicate()` 가 참이 될 때까지 폴링. 시간 초과 시 즉시 실패.

    하트비트처럼 별도 태스크가 상태를 바꾸는 것을 고정 sleep 으로 기다리면
    부하 상황에서 간헐 실패한다(2026-07-25 실측 — 전역 안전망 도입 후 흔들림).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"{timeout}s 안에 조건이 충족되지 않았다: {predicate}")


def _set_tripped_at(db_path: str, value: float | None) -> None:
    """서킷 트립 시각을 임의로 조작 — 냉각 경과/레거시(NULL) 상황 재현용."""
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE kream_bg_budget_state SET tripped_at = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _reset_bg_circuit_counters():
    """모듈 전역 연속실패 카운터 + 잠금상실 플래그 — 테스트 간 누적 방지."""
    kb._bg_consecutive_failures.clear()
    kb._batch_lock_lost = False
    kb._bg_state_tripped_at_ready.clear()
    yield
    kb._bg_consecutive_failures.clear()
    kb._batch_lock_lost = False
    kb._bg_state_tripped_at_ready.clear()


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


# ---------------------------------------------------------------------------
# 2-v F1 — 소프트캡이 "이미 쓴 백그라운드 사용량" 을 차감한다
#
# 이전에는 소프트캡이 소비량과 무관한 상수라 canary(100) 단계에서도 24h 동안
# 수천 건이 허용됐다(코덱스 적대검증 Critical). 아래 테스트들은 램프 단계가
# 실제 상한으로 기능하는지를 고정한다.
# ---------------------------------------------------------------------------


async def test_background_allowance_deducts_background_usage(bg_db):
    # canary soft=100, 백그라운드가 이미 40 소비 → 잔여 60
    _insert_calls_last_24h(bg_db, 40, "bootstrap_light")
    assert await kb.background_allowance() == 60


async def test_background_allowance_counts_session_init_prefix(bg_db):
    """`{purpose}:session_init`(2-v F2 계측) 도 같은 배치가 유발한 실 크림 히트다."""
    _insert_calls_last_24h(bg_db, 30, "bootstrap_light")
    _insert_calls_last_24h(bg_db, 10, "bootstrap_light:session_init")
    assert await kb.background_allowance() == 60


async def test_background_allowance_counts_all_registered_bg_purposes(bg_db):
    _insert_calls_last_24h(bg_db, 25, "bootstrap_light")
    _insert_calls_last_24h(bg_db, 25, "collect_queue_drain")
    assert await kb.background_allowance() == 50


async def test_background_allowance_ignores_live_purposes_for_soft_cap(bg_db):
    """라이브 purpose 는 소프트캡을 갉아먹지 않는다 (하드캡 쪽에만 영향)."""
    _insert_calls_last_24h(bg_db, 50, "v3_delta")
    _insert_calls_last_24h(bg_db, 50, "manual")
    assert await kb.background_allowance() == 100


async def test_background_allowance_ignores_unregistered_prefix_lookalike(bg_db):
    """레지스트리 규약 = 정확일치 또는 `"{원소}:"` 접두. 유사 문자열은 미포함."""
    _insert_calls_last_24h(bg_db, 40, "bootstrap_light_extra")
    assert await kb.background_allowance() == 100


async def test_background_allowance_zero_when_soft_cap_fully_consumed(bg_db):
    _insert_calls_last_24h(bg_db, 100, "collect_queue_drain")
    assert await kb.background_allowance() == 0


async def test_background_allowance_clamps_to_zero_when_bg_overshoots(bg_db):
    """소프트캡을 넘겨 쓴 상태(재시작·경합)에서도 음수가 아니라 0."""
    _insert_calls_last_24h(bg_db, 130, "bootstrap_light")
    assert await kb.background_allowance() == 0


async def test_background_allowance_ignores_background_calls_older_than_24h(bg_db):
    _insert_calls_last_24h(bg_db, 100, "bootstrap_light", age_hours=25)
    assert await kb.background_allowance() == 100


async def test_background_allowance_soft_cap_grows_with_ramp_stage(bg_db):
    """램프 상향 시 잔여도 함께 늘어난다 — 차감이 단계별로 올바르게 적용."""
    _insert_calls_last_24h(bg_db, 80, "bootstrap_light")
    assert await kb.background_allowance() == 20
    await kb.advance_ramp_stage()  # day1 = 500
    assert await kb.background_allowance() == 420


async def test_acquire_background_refused_when_soft_cap_consumed(bg_db, spy_pacer):
    """하드캡·예약분은 여유로워도 소프트캡 소진이면 백그라운드 호출 거부."""
    _insert_calls_last_24h(bg_db, 100, "bootstrap_light")
    with pytest.raises(kb.KreamBackgroundBudgetExceeded):
        async with kb.acquire_background("bootstrap_light"):
            pass
    assert spy_pacer.wait_turn_calls == 0


async def test_acquire_background_allowed_just_below_soft_cap(bg_db, spy_pacer):
    """경계값 — 99건 소비 시점에는 아직 1건 허용된다(오프바이원 방어)."""
    _insert_calls_last_24h(bg_db, 99, "bootstrap_light")
    async with kb.acquire_background("bootstrap_light"):
        pass
    assert spy_pacer.wait_turn_calls == 1


# ---------------------------------------------------------------------------
# 2-v F3 — 배치 상호배제 잠금 (프로세스 간 동시 실행 거부)
# ---------------------------------------------------------------------------


async def test_batch_lock_refuses_second_owner_while_held(bg_db):
    assert await kb.try_acquire_batch_lock("owner-A") is True
    assert await kb.try_acquire_batch_lock("owner-B") is False


async def test_batch_lock_reacquire_by_same_owner_is_idempotent(bg_db):
    assert await kb.try_acquire_batch_lock("owner-A") is True
    assert await kb.try_acquire_batch_lock("owner-A") is True


async def test_batch_lock_acquirable_after_ttl_expiry(bg_db):
    """크래시로 해제가 누락돼도 TTL 경과 후 다음 실행이 되찾는다."""
    assert await kb.try_acquire_batch_lock("owner-A", ttl_seconds=-1) is True
    assert await kb.try_acquire_batch_lock("owner-B") is True


async def test_batch_lock_released_allows_other_owner(bg_db):
    assert await kb.try_acquire_batch_lock("owner-A") is True
    await kb.release_batch_lock("owner-A")
    assert await kb.try_acquire_batch_lock("owner-B") is True


async def test_batch_lock_release_by_non_owner_is_noop(bg_db):
    """만료 후 다른 owner 가 선점한 잠금을 늦게 끝난 이전 owner 가 풀지 못한다."""
    assert await kb.try_acquire_batch_lock("owner-A") is True
    await kb.release_batch_lock("owner-B")
    assert await kb.try_acquire_batch_lock("owner-C") is False


async def test_batch_run_lock_context_raises_when_held_by_other(bg_db):
    await kb.try_acquire_batch_lock("owner-A")
    with pytest.raises(kb.KreamBatchLockHeld):
        async with kb.batch_run_lock("owner-B"):
            pytest.fail("잠금 보유 중인데 본문이 실행되면 안 된다")


async def test_batch_run_lock_context_releases_on_success(bg_db):
    async with kb.batch_run_lock("owner-A"):
        assert await kb.try_acquire_batch_lock("owner-B") is False
    assert await kb.try_acquire_batch_lock("owner-B") is True


async def test_batch_run_lock_context_releases_on_exception(bg_db):
    with pytest.raises(RuntimeError):
        async with kb.batch_run_lock("owner-A"):
            raise RuntimeError("배치 실패")
    assert await kb.try_acquire_batch_lock("owner-B") is True


async def test_batch_run_lock_heartbeat_survives_ttl_expiry(bg_db):
    """TTL 보다 오래 도는 배치도 실행 중에는 잠금을 잃지 않는다.

    램프 상위 단계(3000건 × 페이서 ~3s)는 기본 TTL(2h)을 넘길 수 있다 —
    하트비트가 없으면 실행 도중 만료돼 다른 배치가 끼어든다.
    """
    async with kb.batch_run_lock("owner-A", ttl_seconds=0.2, renew_interval=0.05):
        await asyncio.sleep(0.5)  # TTL 의 2.5배 경과
        assert await kb.try_acquire_batch_lock("owner-B") is False


async def test_batch_run_lock_heartbeat_stops_after_exit(bg_db):
    """본문 종료 후 하트비트 태스크가 남아 잠금을 되살리지 않는다."""
    async with kb.batch_run_lock("owner-A", ttl_seconds=0.2, renew_interval=0.05):
        pass
    assert await kb.try_acquire_batch_lock("owner-B", ttl_seconds=5) is True
    await asyncio.sleep(0.15)  # 유령 하트비트가 있었다면 이 사이에 뺏어갔을 것
    assert await kb.try_acquire_batch_lock("owner-B", ttl_seconds=5) is True


async def test_batch_lock_lost_blocks_further_background_calls(bg_db, spy_pacer):
    """2-vr F3-1 — 실행 중 잠금을 빼앗기면 신규 백그라운드 호출을 거부한다.

    갱신 주기가 TTL 보다 길게 밀린 상황(이벤트루프 장기 블로킹 등)을 재현:
    만료 직후 owner-B 가 선점 → 하트비트가 상실을 감지 → 이후 호출 차단.
    """
    # 갱신 주기(0.15) > TTL(0.05) — 하트비트가 밀려 만료가 먼저 오는 상황을 만든다
    async with kb.batch_run_lock("owner-A", ttl_seconds=0.05, renew_interval=0.15):
        await asyncio.sleep(0.08)  # TTL 경과
        assert await kb.try_acquire_batch_lock("owner-B", ttl_seconds=5) is True
        # 고정 sleep 은 부하 상황에서 흔들린다 — 조건 충족까지 폴링(상한 5s)
        await _wait_until(kb.batch_lock_lost, timeout=5.0)

        assert kb.batch_lock_lost() is True
        with pytest.raises(kb.KreamBatchLockLost):
            async with kb.acquire_background("bootstrap_light"):
                pytest.fail("잠금 상실 후 백그라운드 호출이 통과하면 안 된다")
        assert spy_pacer.wait_turn_calls == 0  # 페이서 소비 전에 거부

    # 컨텍스트 종료 시 플래그 원복 + 남의 잠금은 건드리지 않는다
    assert kb.batch_lock_lost() is False
    assert await kb.try_acquire_batch_lock("owner-C") is False


async def test_batch_lock_lost_flag_cleared_on_next_acquisition(bg_db, spy_pacer):
    """다음 실행이 잠금을 정상 획득하면 이전 런의 상실 상태를 끌고 가지 않는다."""
    kb._batch_lock_lost = True
    async with kb.batch_run_lock("owner-A"):
        assert kb.batch_lock_lost() is False
        async with kb.acquire_background("bootstrap_light"):
            pass
    assert spy_pacer.wait_turn_calls == 1


# ---------------------------------------------------------------------------
# 2-vr M1/M2 — 접두 매칭 규약(밑줄 와일드카드) · 단일 스냅샷 집계
# ---------------------------------------------------------------------------


async def test_background_count_does_not_treat_underscore_as_wildcard(bg_db):
    """`bootstrap_light` 의 `_` 는 LIKE 와일드카드가 아니라 리터럴이어야 한다."""
    _insert_calls_last_24h(bg_db, 40, "bootstrapXlight:session_init")
    assert await kb._count_background_used_24h() == 0
    assert await kb.background_allowance() == 100


async def test_background_count_still_matches_real_prefixes(bg_db):
    """이스케이프가 정상 매칭까지 죽이지 않는다 (과잉 수정 방어)."""
    _insert_calls_last_24h(bg_db, 5, "bootstrap_light")
    _insert_calls_last_24h(bg_db, 5, "bootstrap_light:session_init")
    _insert_calls_last_24h(bg_db, 5, "collect_queue_drain:session_init")
    assert await kb._count_background_used_24h() == 15


async def test_count_24h_total_and_background_single_snapshot(bg_db):
    """전체/백그라운드를 한 문장에서 센다 — 두 수치가 같은 스냅샷."""
    _insert_calls_last_24h(bg_db, 7, "bootstrap_light")
    _insert_calls_last_24h(bg_db, 3, "v3_delta")
    _insert_calls_last_24h(bg_db, 5, "bootstrap_light", age_hours=25)  # 24h 밖

    total, bg = await kb._count_24h_total_and_background()
    assert (total, bg) == (10, 7)


# ---------------------------------------------------------------------------
# 2-v2 (코덱스 수렴 라운드) — 잠금 토큰 · 페이서 대기 중 상실
# ---------------------------------------------------------------------------


async def test_batch_run_lock_same_owner_label_two_runs_are_exclusive(bg_db):
    """같은 `--owner` 라벨을 두 프로세스에 줘도 동시 실행은 거부된다 (2-v2 F3).

    `--owner` 는 resume 를 위해 재사용하도록 만들어진 lease 라벨이다. 이걸
    그대로 잠금 owner 로 쓰면 재진입 규칙을 타고 둘 다 잠금을 얻어버렸다.
    """
    async with kb.batch_run_lock("bootstrap-fixed-label"):
        with pytest.raises(kb.KreamBatchLockHeld):
            async with kb.batch_run_lock("bootstrap-fixed-label"):
                pytest.fail("같은 라벨의 두 번째 실행이 잠금을 얻으면 안 된다")


async def test_batch_run_lock_same_label_release_does_not_free_other_run(bg_db):
    """먼저 끝난 실행이 같은 라벨의 다른 실행 잠금을 풀어버리지 않는다."""
    async with kb.batch_run_lock("bootstrap-fixed-label"):
        with pytest.raises(kb.KreamBatchLockHeld):
            async with kb.batch_run_lock("bootstrap-fixed-label"):
                pass
        # 두 번째(거부된) 실행의 컨텍스트 종료가 첫 실행의 잠금을 건드리면 안 된다
        assert await kb.try_acquire_batch_lock("outsider") is False


async def test_acquire_background_rechecks_lock_loss_after_pacer_wait(bg_db, monkeypatch):
    """페이서 대기 중에 상실이 감지되면 yield 전에 다시 막는다 (2-v2 F4)."""

    class _LosingPacer:
        def __init__(self) -> None:
            self.wait_turn_calls = 0

        async def wait_turn(self) -> float:
            self.wait_turn_calls += 1
            kb._batch_lock_lost = True  # 대기 도중 하트비트가 상실 감지
            return 0.0

    pacer = _LosingPacer()
    monkeypatch.setattr(kb, "get_pacer", lambda: pacer)

    with pytest.raises(kb.KreamBatchLockLost):
        async with kb.acquire_background("bootstrap_light"):
            pytest.fail("잠금 상실 후 크림 호출이 나가면 안 된다")
    assert pacer.wait_turn_calls == 1


async def test_batch_lock_does_not_consume_budget_or_pacer(bg_db, spy_pacer):
    """잠금 판정 자체는 크림 호출이 아니다 — 예산 소비/페이서 대기 0."""
    before = await kb.background_allowance()
    async with kb.batch_run_lock("owner-A"):
        pass
    assert await kb.background_allowance() == before
    assert spy_pacer.wait_turn_calls == 0


# ---------------------------------------------------------------------------
# 5xx 트립 냉각 후 자동 재개 (2026-07-28 사장 승인)
#
# 크림 서버측 일시 장애(실측: 성공률 99.7% 창에서 500 3연속)가 배치 창 전체를
# 죽이고 사람이 개입할 때까지 복구가 안 되던 문제. 5xx 트립
# (`manual_resume_required=0`) 에 한해 냉각 경과 후 acquire 게이트에서 1회
# 자동 재개한다. 403/429(=1) 는 기존대로 manual 전용.
# ---------------------------------------------------------------------------


async def _trip_5xx(path: str = "p1") -> None:
    for status in (500, 502, 503):
        await kb.report_block(status, path=path)


async def test_trip_circuit_records_tripped_at(bg_db):
    """5xx 트립이 트립 시각을 남긴다 — 냉각 판정의 근거."""
    import time as _time

    before = _time.time()
    await _trip_5xx()
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True
    assert isinstance(state["tripped_at"], float)
    assert before <= state["tripped_at"] <= _time.time()


async def test_block_trip_also_records_tripped_at(bg_db):
    """403 트립도 시각은 남긴다(진단용) — 자동 재개 여부는 별개 플래그가 결정."""
    await kb.report_block(403)
    state = await kb.get_ramp_state()
    assert isinstance(state["tripped_at"], float)
    assert state["manual_resume_required"] is True


async def test_5xx_trip_within_cooldown_still_refuses(bg_db, spy_pacer):
    """냉각 미경과 — 여전히 거부하고 남은 냉각 시간을 알린다."""
    await _trip_5xx()
    with pytest.raises(kb.KreamCircuitTripped) as exc:
        async with kb.acquire_background("bootstrap_light"):
            pytest.fail("냉각 중에는 백그라운드 호출이 나가면 안 된다")
    assert "냉각" in str(exc.value)
    assert spy_pacer.wait_turn_calls == 0
    assert (await kb.get_ramp_state())["circuit_tripped"] is True


async def test_5xx_trip_auto_resumes_after_cooldown(bg_db, spy_pacer):
    """냉각 경과 → acquire 진입 성공 + DB 서킷 상태·연속실패 카운터 전부 초기화."""
    import time as _time

    await _trip_5xx()
    assert kb._bg_consecutive_failures.get("p1") == 3
    _set_tripped_at(bg_db, _time.time() - kb.BG_5XX_COOLDOWN_SEC - 1)

    async with kb.acquire_background("bootstrap_light"):
        pass

    assert spy_pacer.wait_turn_calls == 1
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is False
    assert state["circuit_reason"] is None
    assert state["manual_resume_required"] is False
    assert state["tripped_at"] is None
    assert kb._bg_consecutive_failures == {}


async def test_auto_resume_honors_cooldown_constant(bg_db, spy_pacer):
    """냉각 길이는 모듈 상수를 **호출 시점에** 읽는다.

    0 으로 낮춰 "즉시 재개" 를 기대 동작으로 고정하면 안 된다(그건 서킷 무력화
    다 — 하한 300s 는 `clamp_cooldown_config` 가 강제한다). 짧은 양수 냉각으로
    "기준을 실제로 참조한다" 만 고정한다.
    """
    import time as _time

    await _trip_5xx()
    _set_tripped_at(bg_db, _time.time() - 6.0)
    # 기본 냉각(1800s)이라면 아직 거부돼야 하는 시점
    with pytest.raises(kb.KreamCircuitTripped):
        async with kb.acquire_background("bootstrap_light"):
            pass

    kb.BG_5XX_COOLDOWN_SEC = 5.0
    try:
        async with kb.acquire_background("bootstrap_light"):
            pass
    finally:
        kb.BG_5XX_COOLDOWN_SEC = kb.clamp_cooldown_config(kb._RAW_BG_5XX_COOLDOWN)
    assert spy_pacer.wait_turn_calls == 1


# ---------------------------------------------------------------------------
# 냉각 상수 설정 방어 (코드리뷰 Important #1)
#
# 안전을 느슨하게 만드는 노브는 바닥이 필수다 — env 로 냉각을 0/60 으로 낮추면
# 5xx 서킷이 사실상 무력화된다(=60 이면 창당 4콜 × 1,440창 = 5,760콜/일).
# ---------------------------------------------------------------------------


async def test_parse_positive_float_accepts_valid_value(monkeypatch):
    monkeypatch.setenv("KREAM_TEST_FLOAT", "900.5")
    assert kb._parse_positive_float("KREAM_TEST_FLOAT", 1800.0) == 900.5


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "1e", "nan"])
async def test_parse_positive_float_rejects_invalid(monkeypatch, raw):
    """파싱 불가·0 이하·NaN 은 None — 설정 오류로 보고된다(조용한 폴백 금지)."""
    monkeypatch.setenv("KREAM_TEST_FLOAT", raw)
    assert kb._parse_positive_float("KREAM_TEST_FLOAT", 1800.0) is None


@pytest.mark.parametrize("raw", [None, "", "   "])
async def test_parse_positive_float_missing_uses_default(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("KREAM_TEST_FLOAT", raising=False)
    else:
        monkeypatch.setenv("KREAM_TEST_FLOAT", raw)
    assert kb._parse_positive_float("KREAM_TEST_FLOAT", 1800.0) == 1800.0


async def test_cooldown_clamped_to_floor():
    """하한 미만 설정은 바닥(300s)으로 끌어올린다 — 캡이 낮추는 방향만 허용되는 것과 대칭."""
    assert kb.BG_5XX_COOLDOWN_FLOOR_SEC == 300.0
    assert kb.clamp_cooldown_config(60.0) == 300.0
    assert kb.clamp_cooldown_config(0.5) == 300.0
    assert kb.clamp_cooldown_config(300.0) == 300.0


async def test_cooldown_clamp_keeps_valid_and_falls_back_on_none():
    assert kb.clamp_cooldown_config(3600.0) == 3600.0
    # 파싱 실패(None) → 기본값 폴백 (그리고 아래 config_error 가 별도로 보고한다)
    assert kb.clamp_cooldown_config(None) == kb.BG_5XX_COOLDOWN_DEFAULT_SEC == 1800.0


async def test_effective_cooldown_never_below_floor():
    """이 프로세스에 적용된 유효값도 하한 아래일 수 없다."""
    assert kb.BG_5XX_COOLDOWN_SEC >= kb.BG_5XX_COOLDOWN_FLOOR_SEC


async def test_config_error_reports_unparsable_cooldown(monkeypatch):
    """파싱 실패는 기존 설정오류 경로로 원문과 함께 보고된다(워커 시작 거부)."""
    monkeypatch.setenv("KREAM_BG_5XX_COOLDOWN_SEC", "삼십분")
    error = kb.kream_budget_config_error(10000, 1000, None)
    assert error is not None
    assert "KREAM_BG_5XX_COOLDOWN_SEC" in error
    assert "삼십분" in error


async def test_config_error_none_when_cooldown_valid():
    assert kb.kream_budget_config_error(10000, 1000, 1800.0) is None


# ---------------------------------------------------------------------------
# 자동 재개 CAS (코드리뷰 Important #3)
#
# 스냅샷 관측과 clear UPDATE 사이 await 경계에서 다른 프로세스의 403 트립이
# 착지하면, 무조건 wipe 는 "403 은 자동 재개 없음" 스펙을 뚫어버린다.
# ---------------------------------------------------------------------------


async def test_cas_clear_succeeds_on_matching_snapshot(bg_db):
    await _trip_5xx()
    observed = (await kb.get_ramp_state())["tripped_at"]
    assert await kb._try_clear_circuit_cas(observed) is True
    assert (await kb.get_ramp_state())["circuit_tripped"] is False


async def test_cas_clear_refuses_when_tripped_at_moved(bg_db):
    """관측 이후 재트립(시각 변경)이 있었으면 지우지 않는다."""
    await _trip_5xx()
    observed = (await kb.get_ramp_state())["tripped_at"]
    await kb._trip_circuit("5xx_streak:p2:3", manual_resume_required=False)
    assert await kb._try_clear_circuit_cas(observed) is False
    assert (await kb.get_ramp_state())["circuit_tripped"] is True


async def test_cas_clear_refuses_manual_resume_required(bg_db):
    """403 트립은 CAS 로도 지워지지 않는다 — 시각이 일치해도."""
    await kb.report_block(403)
    observed = (await kb.get_ramp_state())["tripped_at"]
    assert await kb._try_clear_circuit_cas(observed) is False
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True
    assert state["manual_resume_required"] is True


async def test_auto_resume_loses_race_to_concurrent_403_trip(bg_db, spy_pacer, monkeypatch):
    """스냅샷 후 clear 직전에 403 트립이 착지 — 자동 재개가 그걸 지우면 안 된다."""
    import time as _time

    await _trip_5xx()
    _set_tripped_at(bg_db, _time.time() - kb.BG_5XX_COOLDOWN_SEC - 1)
    real_cas = kb._try_clear_circuit_cas

    async def _cas_with_race(tripped_at: float) -> bool:
        # 다른 프로세스가 403 을 받아 트립시킨 순간 (await 경계 재현)
        await kb.report_block(403)
        return await real_cas(tripped_at)

    monkeypatch.setattr(kb, "_try_clear_circuit_cas", _cas_with_race)

    with pytest.raises(kb.KreamCircuitTripped) as exc:
        async with kb.acquire_background("bootstrap_light"):
            pytest.fail("403 트립이 자동 재개로 지워지면 안 된다")

    assert "manual_resume()" in str(exc.value)
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is True
    assert state["manual_resume_required"] is True  # 403 트립 생존
    assert spy_pacer.wait_turn_calls == 0


async def test_auto_resume_cas_failure_leaves_failure_counters(bg_db, spy_pacer, monkeypatch):
    """CAS 실패 시엔 재개하지 않았으므로 연속실패 카운터도 건드리지 않는다."""
    import time as _time

    await _trip_5xx()
    _set_tripped_at(bg_db, _time.time() - kb.BG_5XX_COOLDOWN_SEC - 1)

    async def _cas_always_lost(tripped_at: float) -> bool:
        return False

    monkeypatch.setattr(kb, "_try_clear_circuit_cas", _cas_always_lost)

    with pytest.raises(kb.KreamCircuitTripped):
        async with kb.acquire_background("bootstrap_light"):
            pass
    assert kb._bg_consecutive_failures.get("p1") == 3
    assert spy_pacer.wait_turn_calls == 0


async def test_auto_resume_does_not_bypass_budget(bg_db, spy_pacer):
    """자동 재개는 서킷만 푼다 — 예산 판정은 그대로 통과해야 한다."""
    import time as _time

    _insert_calls_last_24h(bg_db, 100, "bootstrap_light")  # 소프트캡 소진
    await _trip_5xx()
    _set_tripped_at(bg_db, _time.time() - kb.BG_5XX_COOLDOWN_SEC - 1)

    with pytest.raises(kb.KreamBackgroundBudgetExceeded):
        async with kb.acquire_background("bootstrap_light"):
            pytest.fail("예산 소진 상태에서 호출이 나가면 안 된다")
    assert spy_pacer.wait_turn_calls == 0


async def test_403_trip_never_auto_resumes_even_after_cooldown(bg_db, spy_pacer):
    """403/429 는 냉각이 아무리 지나도 자동 재개 금지 — 기존 스펙 유지."""
    import time as _time

    await kb.report_block(403)
    _set_tripped_at(bg_db, _time.time() - kb.BG_5XX_COOLDOWN_SEC * 100)

    with pytest.raises(kb.KreamCircuitTripped) as exc:
        async with kb.acquire_background("bootstrap_light"):
            pytest.fail("403 트립은 자동 재개되면 안 된다")
    assert "manual_resume()" in str(exc.value)
    assert spy_pacer.wait_turn_calls == 0
    assert (await kb.get_ramp_state())["circuit_tripped"] is True


async def test_legacy_trip_without_tripped_at_requires_manual_resume(bg_db, spy_pacer):
    """마이그레이션 전 트립(tripped_at NULL)은 냉각 판정 불가 → manual 전용(안전 기본값)."""
    await _trip_5xx()
    _set_tripped_at(bg_db, None)

    with pytest.raises(kb.KreamCircuitTripped) as exc:
        async with kb.acquire_background("bootstrap_light"):
            pytest.fail("트립 시각 미상이면 자동 재개하면 안 된다")
    assert "manual_resume()" in str(exc.value)
    assert spy_pacer.wait_turn_calls == 0
    assert (await kb.get_ramp_state())["circuit_tripped"] is True


async def test_manual_resume_clears_tripped_at(bg_db):
    """수동 재개는 트립 시각도 지운다 — 잔여값이 다음 트립 판정을 오염시키지 않게."""
    await _trip_5xx()
    assert (await kb.get_ramp_state())["tripped_at"] is not None

    await kb.manual_resume()
    state = await kb.get_ramp_state()
    assert state["circuit_tripped"] is False
    assert state["tripped_at"] is None


async def test_legacy_bg_state_table_gets_tripped_at_column(tmp_path, monkeypatch):
    """`tripped_at` 없는 기존 DB 도 마이그레이션으로 그대로 동작한다."""
    db_path = tmp_path / "legacy_bg.db"
    conn = sqlite3.connect(str(db_path))
    _create_kream_api_calls(conn)
    conn.executescript(
        """
        CREATE TABLE kream_bg_budget_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            stage TEXT NOT NULL DEFAULT 'canary',
            stage_entered_at TEXT NOT NULL DEFAULT (datetime('now')),
            circuit_tripped INTEGER NOT NULL DEFAULT 0,
            circuit_reason TEXT,
            manual_resume_required INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO kream_bg_budget_state
            (id, stage, stage_entered_at, circuit_tripped, circuit_reason,
             manual_resume_required)
        VALUES (1, 'day2', datetime('now'), 1, '5xx_streak:p1:3', 0);
        """
    )
    conn.commit()
    conn.close()

    from src.config import settings

    monkeypatch.setattr(settings, "db_path", str(db_path), raising=True)

    state = await kb.get_ramp_state()
    assert state["stage"] == "day2"  # 기존 행이 재생성으로 덮이지 않는다
    assert state["circuit_tripped"] is True
    assert state["tripped_at"] is None  # 레거시 트립 → 자동 재개 불가
