"""Orchestrator 테스트 (Phase 2.3c).

덤프 → 매칭 → 수익 → 알림 체인 + 체크포인트 복구 + 스로틀 게이트 커버리지.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from src.core.call_throttle import CallThrottle
from src.core.checkpoint_store import CheckpointStore
from src.core.event_bus import (
    AlertSent,
    CandidateMatched,
    CatalogDumped,
    EventBus,
    ProfitFound,
)
from src.core.orchestrator import Orchestrator


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "ckpt.db"
    s = CheckpointStore(str(db_path))
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def _throttle(rate: float = 6000.0, burst: int = 100) -> CallThrottle:
    """기본: 매우 넉넉한 스로틀."""
    return CallThrottle(rate_per_min=rate, burst=burst)


def _sample_catalog() -> CatalogDumped:
    return CatalogDumped(source="musinsa", product_count=1, dumped_at=123.0)


def _sample_candidate(model_no: str = "CW2288-111") -> CandidateMatched:
    return CandidateMatched(
        source="musinsa",
        kream_product_id=1,
        model_no=model_no,
        retail_price=100000,
        size="270",
        url="https://example.com/p/1",
    )


def _sample_profit(model_no: str = "CW2288-111") -> ProfitFound:
    return ProfitFound(
        source="musinsa",
        kream_product_id=1,
        model_no=model_no,
        size="270",
        retail_price=100000,
        kream_sell_price=150000,
        net_profit=40000,
        roi=0.4,
        signal="STRONG_BUY",
        volume_7d=10,
        url="https://example.com/p/1",
    )


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01):
    """비동기 조건 대기 헬퍼."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ----------------------------------------------------------------------
# 1) happy path: 덤프 → 매칭 → 수익 → 알림 완주
# ----------------------------------------------------------------------
async def test_happy_path_full_chain(bus, store):
    throttle = _throttle()
    orch = Orchestrator(bus, store, throttle)

    alerts: list[AlertSent] = []

    async def catalog_handler(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        yield _sample_candidate("A-1")
        yield _sample_candidate("A-2")

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        return _sample_profit(event.model_no)

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        sent = AlertSent(
            alert_id=len(alerts) + 1,
            kream_product_id=event.kream_product_id,
            signal=event.signal,
            fired_at=999.0,
        )
        alerts.append(sent)
        return sent

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        await bus.publish(_sample_catalog())

        assert await _wait_until(lambda: len(alerts) == 2)

        stats = orch.stats()
        assert stats["catalog_processed"] == 1
        assert stats["candidate_processed"] == 2
        assert stats["profit_processed"] == 2
        assert stats["candidate_dropped_throttle"] == 0

        # 체크포인트 모두 consumed
        pending = await store.pending()
        assert pending == []
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 2) throttle 소진 시 CandidateMatched drop + deny 카운트 증가
# ----------------------------------------------------------------------
async def test_candidate_dropped_when_throttle_exhausted(bus, store):
    # burst=1, rate 매우 낮음 → 첫 토큰만 통과, 둘째부터 drop
    throttle = CallThrottle(rate_per_min=0.001, burst=1)
    orch = Orchestrator(bus, store, throttle)

    called: list[str] = []

    async def catalog_handler(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        called.append(event.model_no)
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        await bus.publish(_sample_candidate("A-1"))
        await bus.publish(_sample_candidate("A-2"))
        await bus.publish(_sample_candidate("A-3"))

        assert await _wait_until(
            lambda: orch.stats()["candidate_dropped_throttle"] >= 2
        )

        stats = orch.stats()
        assert stats["candidate_processed"] == 1
        assert stats["candidate_dropped_throttle"] == 2
        assert called == ["A-1"]
        # throttle 통계도 반영
        assert stats["throttle"]["denied_total"] >= 2
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 3) handler 예외 시 로깅 + 실패 카운트 + 다음 이벤트는 정상 처리
# ----------------------------------------------------------------------
async def test_handler_exception_does_not_break_chain(bus, store, caplog):
    throttle = _throttle()
    orch = Orchestrator(bus, store, throttle)

    processed: list[str] = []

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        if event.model_no == "BOOM":
            raise RuntimeError("handler 폭발")
        processed.append(event.model_no)
        return None

    async def noop_catalog(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def noop_profit(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(noop_catalog)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(noop_profit)

    await orch.start()
    try:
        await bus.publish(_sample_candidate("BOOM"))
        await bus.publish(_sample_candidate("OK"))

        assert await _wait_until(lambda: "OK" in processed)

        stats = orch.stats()
        assert stats["candidate_failed"] == 1
        assert stats["candidate_processed"] == 1
        # 체크포인트는 두 건 모두 consumed (실패해도 mark)
        pending = await store.pending()
        assert pending == []
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 4) recover(): pending 체크포인트 → 재주입 → 체인 끝까지 도달
# ----------------------------------------------------------------------
async def test_recover_replays_pending_events(bus, store):
    # 사전: 이전 세션이 CandidateMatched 기록만 남기고 crash 했다고 가정
    leftover = _sample_candidate("LEFT-1")
    leftover_ckpt = await store.record(leftover, consumer="candidate")

    throttle = _throttle()
    orch = Orchestrator(bus, store, throttle)

    alerts: list[AlertSent] = []

    async def catalog_handler(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        return _sample_profit(event.model_no)

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        sent = AlertSent(
            alert_id=1,
            kream_product_id=event.kream_product_id,
            signal=event.signal,
            fired_at=1.0,
        )
        alerts.append(sent)
        return sent

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    # start 먼저 → recover 로 직접 처리 (bus 우회, 결정적)
    await orch.start()
    try:
        await orch.recover()

        # recover 가 끝나면 체인이 완주한 상태
        assert len(alerts) == 1
        assert alerts[0].kream_product_id == 1

        # 원본 leftover ckpt 는 consumed 처리되어 pending 에서 빠져야 한다
        pending = await store.pending()
        assert not any(p["id"] == leftover_ckpt for p in pending)
        # 그리고 중복 실행 흔적 없음
        assert orch.stats()["candidate_processed"] == 1
        assert orch.stats()["profit_processed"] == 1
    finally:
        await orch.stop()


async def test_recover_before_start_raises(bus, store):
    orch = Orchestrator(bus, store, _throttle())
    with pytest.raises(RuntimeError):
        await orch.recover()


# ----------------------------------------------------------------------
# 7) throttle 소진 시 CandidateMatched 는 deferred 로 보존 (이벤트 손실 X)
# ----------------------------------------------------------------------
async def test_throttle_rejected_candidate_is_deferred(bus, store):
    throttle = CallThrottle(rate_per_min=0.001, burst=1)
    orch = Orchestrator(bus, store, throttle)

    async def catalog_handler(event):
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event):
        return None

    async def profit_handler(event):
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        # 첫 건은 통과 (burst=1), 두 번째는 throttle 거부 → deferred
        await bus.publish(_sample_candidate("OK"))
        await bus.publish(_sample_candidate("DEFER"))
        assert await _wait_until(
            lambda: orch.stats()["candidate_deferred"] >= 1
        )

        # deferred 건은 pending() 에 status='deferred' 로 남아있어야 한다
        pending = await store.pending()
        deferred = [p for p in pending if p["status"] == "deferred"]
        assert len(deferred) == 1
        assert deferred[0]["payload"]["model_no"] == "DEFER"
        assert deferred[0]["last_reason"] == "throttle_exhausted"
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 8) recover 가 deferred 건을 토큰 복구 후 재시도하여 완주
# ----------------------------------------------------------------------
async def test_recover_retries_deferred_after_tokens_refilled(bus, store):
    # 이전 세션: throttle 거부로 deferred 상태 simulate
    leftover = _sample_candidate("RETRY-1")
    cid = await store.record(leftover, consumer="candidate")
    await store.mark_deferred(cid, "throttle_exhausted")

    throttle = _throttle()  # 넉넉한 토큰
    orch = Orchestrator(bus, store, throttle)

    alerts: list[AlertSent] = []

    async def catalog_handler(event):
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event):
        return _sample_profit(event.model_no)

    async def profit_handler(event):
        sent = AlertSent(
            alert_id=1,
            kream_product_id=event.kream_product_id,
            signal=event.signal,
            fired_at=1.0,
        )
        alerts.append(sent)
        return sent

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        await orch.recover()
        assert len(alerts) == 1
        # 원본 deferred 건 소비 처리
        pending = await store.pending()
        assert not any(p["id"] == cid for p in pending)
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 9) attempts 3회 초과 시 failed 로 전환
# ----------------------------------------------------------------------
async def test_recover_attempts_exceeded_marks_failed(bus, store):
    leftover = _sample_candidate("DOOMED")
    cid = await store.record(leftover, consumer="candidate")
    # 이미 3회 시도한 상태
    for _ in range(3):
        await store.increment_attempts(cid)

    throttle = _throttle()
    orch = Orchestrator(bus, store, throttle)

    calls: list[str] = []

    async def catalog_handler(event):
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event):
        calls.append(event.model_no)
        return None

    async def profit_handler(event):
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        await orch.recover()
        # 재시도 안 됨
        assert calls == []
        # failed 상태
        import aiosqlite  # noqa: F401

        db = store._require_db()
        cur = await db.execute(
            "SELECT status FROM event_checkpoint WHERE id = ?", (cid,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row["status"] == "failed"
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 10) AlertSent dedup: 같은 ckpt 두 번 처리 시 handler 1회만 호출
# ----------------------------------------------------------------------
async def test_alert_dedup_on_duplicate_processing(bus, store):
    throttle = _throttle()
    orch = Orchestrator(bus, store, throttle)

    alert_calls: list[str] = []

    async def catalog_handler(event):
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event):
        return None

    async def profit_handler(event):
        alert_calls.append(event.model_no)
        return AlertSent(
            alert_id=1,
            kream_product_id=event.kream_product_id,
            signal=event.signal,
            fired_at=1.0,
        )

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        # 동일 ProfitFound 를 같은 checkpoint_id 로 두 번 처리 시뮬레이션
        profit = _sample_profit("DUP")
        ckpt_id = await store.record(profit, consumer="profit")
        await orch._process_profit(profit, ckpt_id)
        # 두 번째 시도 — dedup 테이블에 이미 있으므로 handler 호출 X
        # (새 ckpt 를 흉내내면 dedup 우회되므로, 동일 ckpt 로 재시도)
        # record 는 한 번만. 두 번째는 pending 아닐 수 있으나 직접 호출.
        await orch._process_profit(profit, ckpt_id)

        assert len(alert_calls) == 1
        assert orch.stats()["alert_duplicated"] >= 1

        # alert_sent 에 정확히 1행만
        db = store._require_db()
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM alert_sent WHERE checkpoint_id = ?",
            (ckpt_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row["n"] == 1
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 5) 중복 handler 등록 → ValueError
# ----------------------------------------------------------------------
async def test_duplicate_handler_registration_raises(bus, store):
    orch = Orchestrator(bus, store, _throttle())

    async def catalog_handler(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    with pytest.raises(ValueError):
        orch.on_catalog_dumped(catalog_handler)

    orch.on_candidate_matched(candidate_handler)
    with pytest.raises(ValueError):
        orch.on_candidate_matched(candidate_handler)

    orch.on_profit_found(profit_handler)
    with pytest.raises(ValueError):
        orch.on_profit_found(profit_handler)


# ----------------------------------------------------------------------
# 6) stop() 후 더 이상 이벤트 처리 안 됨
# ----------------------------------------------------------------------
async def test_stop_halts_processing(bus, store):
    throttle = _throttle()
    orch = Orchestrator(bus, store, throttle)

    processed: list[str] = []

    async def catalog_handler(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        processed.append(event.model_no)
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    await bus.publish(_sample_candidate("BEFORE"))
    assert await _wait_until(lambda: "BEFORE" in processed)

    await orch.stop()
    assert orch.stats()["running"] is False

    # stop 이후 publish 해도 processed 에 추가 안 됨
    await bus.publish(_sample_candidate("AFTER"))
    await asyncio.sleep(0.05)
    assert "AFTER" not in processed
