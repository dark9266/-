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
    await store.record(leftover, consumer="candidate")

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

    # start 먼저 해서 구독자 붙인 다음 recover 로 재주입 → 큐로 들어감
    await orch.start()
    try:
        await orch.recover()

        assert await _wait_until(lambda: len(alerts) == 1)
        assert alerts[0].kream_product_id == 1

        # 기존 체크포인트(이전 세션 것)는 replay 중이라 mark 안 되고,
        # 새로 생긴 profit 체크포인트는 consumed 됨.
        # 남은 pending 은 leftover candidate 1건이어야 한다
        # (replay 시 _replaying=True 라 record 를 건너뛰고 mark 도 안 함).
        pending = await store.pending()
        pending_candidate = [
            p for p in pending if p["event_type"] == "CandidateMatched"
        ]
        assert len(pending_candidate) == 1
        assert pending_candidate[0]["payload"]["model_no"] == "LEFT-1"
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
