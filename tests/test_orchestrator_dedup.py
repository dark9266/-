"""Orchestrator pid-level 24h dedup 게이트 테스트.

실측 데이터: 15,582 block / 3,226 unique pid = 4.83x 재처리.
→ 동일 (pid, retail_price) 가 6h 창 안에 두 번째 진입하면 snapshot/handler 미호출.
"""

from __future__ import annotations

import time
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
from src.core.orchestrator import (
    DEDUP_WINDOW_SECONDS,
    REASON_DEDUP_RECENT,
    Orchestrator,
)


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


def _throttle() -> CallThrottle:
    return CallThrottle(rate_per_min=6000.0, burst=100)


def _candidate(
    model_no: str = "CW2288-111",
    kream_pid: int = 1,
    retail_price: int = 100000,
    source: str = "musinsa",
) -> CandidateMatched:
    return CandidateMatched(
        source=source,
        kream_product_id=kream_pid,
        model_no=model_no,
        retail_price=retail_price,
        size="270",
        url="https://example.com/p/1",
    )


# ----------------------------------------------------------------------
# 1) 같은 (pid, retail_price) 6h 내 2회 진입 → 두 번째는 dedup
# ----------------------------------------------------------------------
async def test_dedup_blocks_second_entry_same_pid_price(bus, store):
    orch = Orchestrator(bus, store, _throttle())

    call_count = {"n": 0}

    async def catalog_handler(event: CatalogDumped) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        call_count["n"] += 1
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        cand = _candidate(kream_pid=1, retail_price=100000)
        ckpt1 = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt1)

        ckpt2 = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt2)

        assert call_count["n"] == 1, "두 번째 호출은 dedup 으로 차단돼야 함"
        stats = orch.stats()
        assert stats.get("candidate_dedup_skipped", 0) == 1
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 2) 같은 pid 지만 retail_price 가 다르면 dedup 아님 (가격 변동 → 재평가)
# ----------------------------------------------------------------------
async def test_different_retail_price_not_deduped(bus, store):
    orch = Orchestrator(bus, store, _throttle())

    prices_seen: list[int] = []

    async def catalog_handler(event: CatalogDumped) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        prices_seen.append(event.retail_price)
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        cand_100 = _candidate(kream_pid=1, retail_price=100000)
        ckpt = await store.record(cand_100, consumer="candidate")
        await orch._process_candidate(cand_100, ckpt)

        # 같은 pid, 다른 가격 (flash sale) → 재평가 필요
        cand_80 = _candidate(kream_pid=1, retail_price=80000)
        ckpt = await store.record(cand_80, consumer="candidate")
        await orch._process_candidate(cand_80, ckpt)

        assert prices_seen == [100000, 80000]
        assert orch.stats().get("candidate_dedup_skipped", 0) == 0
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 3) 다른 pid 는 dedup 무관
# ----------------------------------------------------------------------
async def test_different_pid_not_deduped(bus, store):
    orch = Orchestrator(bus, store, _throttle())

    pids_seen: list[int] = []

    async def catalog_handler(event: CatalogDumped) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        pids_seen.append(event.kream_product_id)
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        cand_a = _candidate(kream_pid=1, retail_price=100000)
        ckpt = await store.record(cand_a, consumer="candidate")
        await orch._process_candidate(cand_a, ckpt)

        cand_b = _candidate(kream_pid=2, retail_price=100000)
        ckpt = await store.record(cand_b, consumer="candidate")
        await orch._process_candidate(cand_b, ckpt)

        assert pids_seen == [1, 2]
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 4) handler 예외 시 dedup 기록 없음 → 다음 시도는 정상 진입
# ----------------------------------------------------------------------
async def test_handler_exception_does_not_record_dedup(bus, store):
    orch = Orchestrator(bus, store, _throttle())

    call_count = {"n": 0}

    async def catalog_handler(event: CatalogDumped) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first attempt 폭발")
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        cand = _candidate(kream_pid=1, retail_price=100000)
        ckpt1 = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt1)

        ckpt2 = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt2)

        assert call_count["n"] == 2, "handler 예외는 dedup 기록 안 해야 함"
        assert orch.stats().get("candidate_dedup_skipped", 0) == 0
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 5) dedup 6h 창 경과 후엔 재진입 허용 (시간 변조로 검증)
# ----------------------------------------------------------------------
async def test_dedup_window_expiry(bus, store):
    orch = Orchestrator(bus, store, _throttle())

    call_count = {"n": 0}

    async def catalog_handler(event: CatalogDumped) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        call_count["n"] += 1
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        cand = _candidate(kream_pid=1, retail_price=100000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        # dedup 기록 시각을 과거로 변조 (창 초과)
        db = store._require_db()  # noqa: SLF001
        await db.execute(
            "UPDATE kream_candidate_dedup SET last_processed_at = ? "
            "WHERE kream_product_id = ? AND retail_price = ?",
            (time.time() - DEDUP_WINDOW_SECONDS - 60, 1, 100000),
        )
        await db.commit()

        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        assert call_count["n"] == 2, "창 경과 후엔 재진입 허용"
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 6) dedup block 이 decision_log 에 REASON_DEDUP_RECENT 로 기록
# ----------------------------------------------------------------------
async def test_dedup_logs_decision(bus, store):
    orch = Orchestrator(bus, store, _throttle())

    async def catalog_handler(event: CatalogDumped) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(event: CandidateMatched) -> ProfitFound | None:
        return None

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        return None

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        cand = _candidate(kream_pid=1, retail_price=100000)
        ckpt1 = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt1)

        ckpt2 = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt2)

        db = store._require_db()  # noqa: SLF001
        async with db.execute(
            "SELECT reason FROM decision_log WHERE kream_product_id = 1 "
            "ORDER BY id ASC"
        ) as cur:
            rows = await cur.fetchall()
        reasons = [r[0] for r in rows]
        assert REASON_DEDUP_RECENT in reasons
    finally:
        await orch.stop()
