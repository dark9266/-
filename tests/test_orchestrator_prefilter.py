"""Orchestrator pre-filter 게이트 테스트.

로컬 kream_price_history 의 최근 sell_now_price 로 tentative profit 을
산정, 명백한 손실 candidate 를 throttle·snapshot 이전에 차단.

- 4,320/일 throttle 예산 vs 10건/일 profit (hit rate 0.23%) 구조 대응.
- fail-open: 데이터 없거나 테이블 누락 시 통과 (기존 파이프라인 유지).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
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
    REASON_PREFILTER_LOW_VOLUME,
    REASON_PREFILTER_UNPROFITABLE,
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
    pid: int = 1,
    retail_price: int = 100000,
) -> CandidateMatched:
    return CandidateMatched(
        source="musinsa",
        kream_product_id=pid,
        model_no=f"M-{pid}",
        retail_price=retail_price,
        size="270",
        url="https://example.com/p/1",
    )


async def _seed_kream_price(store: CheckpointStore, pid: int, sell_now: int) -> None:
    """kream_price_history 시드 — 테스트 DB 에 최근 가격 1건 주입."""
    db = store._require_db()  # noqa: SLF001
    await db.execute(
        """CREATE TABLE IF NOT EXISTS kream_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            size TEXT NOT NULL,
            sell_now_price INTEGER,
            buy_now_price INTEGER,
            bid_count INTEGER DEFAULT 0,
            last_sale_price INTEGER,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await db.execute(
        "INSERT INTO kream_price_history (product_id, size, sell_now_price) "
        "VALUES (?, ?, ?)",
        (str(pid), "ALL", sell_now),
    )
    await db.commit()


async def _seed_kream_product(
    store: CheckpointStore, pid: int, volume_7d: int
) -> None:
    """kream_products 시드 — volume gate 테스트용 단일 row 주입."""
    db = store._require_db()  # noqa: SLF001
    await db.execute(
        """CREATE TABLE IF NOT EXISTS kream_products (
            product_id TEXT PRIMARY KEY,
            volume_7d INTEGER DEFAULT 0
        )"""
    )
    await db.execute(
        "INSERT OR REPLACE INTO kream_products (product_id, volume_7d) "
        "VALUES (?, ?)",
        (str(pid), volume_7d),
    )
    await db.commit()


# ----------------------------------------------------------------------
# 1) 명백한 손실 candidate 차단 (last kream_sell 이 retail 보다 작음)
# ----------------------------------------------------------------------
async def test_prefilter_blocks_unprofitable(bus, store):
    orch = Orchestrator(bus, store, _throttle())
    await orch.start()
    try:
        # 스키마 초기화 강제
        pass
    finally:
        pass

    # dedup/alert_sent 스키마 로딩 후에 price_history seed 가능
    # → orch.start() 에서 ensure_alert_schema 호출되었으므로 OK
    await _seed_kream_price(store, pid=42, sell_now=50_000)

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

    try:
        # retail 100k > last kream_sell 50k → 명백한 손실
        cand = _candidate(pid=42, retail_price=100_000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        assert call_count["n"] == 0, "명백한 손실 후보는 handler 도달 전 차단"
        stats = orch.stats()
        assert stats.get("candidate_prefilter_blocked", 0) == 1
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 2) 수익 가능한 candidate 는 통과
# ----------------------------------------------------------------------
async def test_prefilter_passes_profitable(bus, store):
    orch = Orchestrator(bus, store, _throttle())
    await orch.start()
    await _seed_kream_price(store, pid=42, sell_now=200_000)

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

    try:
        # retail 100k vs last kream_sell 200k → 수수료 공제 후도 수익 가능
        cand = _candidate(pid=42, retail_price=100_000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        assert call_count["n"] == 1, "수익 가능 후보는 handler 까지 통과"
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 3) kream_price_history 데이터 없으면 fail-open (통과)
# ----------------------------------------------------------------------
async def test_prefilter_fails_open_on_missing_data(bus, store):
    orch = Orchestrator(bus, store, _throttle())
    await orch.start()

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

    try:
        # pid 99 의 kream_price_history 데이터 없음 → fail-open 통과
        cand = _candidate(pid=99, retail_price=100_000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        assert call_count["n"] == 1
        assert orch.stats().get("candidate_prefilter_blocked", 0) == 0
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 4) 블록 시 decision_log 에 REASON_PREFILTER_UNPROFITABLE 기록
# ----------------------------------------------------------------------
async def test_prefilter_logs_decision(bus, store):
    orch = Orchestrator(bus, store, _throttle())
    await orch.start()
    await _seed_kream_price(store, pid=42, sell_now=50_000)

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

    try:
        cand = _candidate(pid=42, retail_price=100_000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        db = store._require_db()  # noqa: SLF001
        async with db.execute(
            "SELECT reason FROM decision_log WHERE kream_product_id = 42"
        ) as cur:
            rows = await cur.fetchall()
        reasons = [r[0] for r in rows]
        assert REASON_PREFILTER_UNPROFITABLE in reasons
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 5) 게이트 순서 — dedup 다음, throttle 이전
#    (dedup 이 먼저 처리하면 prefilter 까지 안 감; throttle 을 흘려버려도
#    prefilter 가 차단하면 candidate_dropped_throttle 증가 없어야 함)
# ----------------------------------------------------------------------
async def test_prefilter_runs_before_throttle(bus, store):
    # 타이트한 throttle — prefilter 가 먼저 차단하면 throttle 은 건드리지 않음
    throttle = CallThrottle(rate_per_min=0.001, burst=1)
    orch = Orchestrator(bus, store, throttle)
    await orch.start()
    await _seed_kream_price(store, pid=42, sell_now=50_000)

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

    try:
        cand = _candidate(pid=42, retail_price=100_000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        stats = orch.stats()
        # prefilter 차단 → throttle 도달 X
        assert stats.get("candidate_prefilter_blocked", 0) == 1
        assert stats.get("candidate_dropped_throttle", 0) == 0
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 6) volume gate — kream_products.volume_7d=0 이면 차단 (adidas root cause)
# ----------------------------------------------------------------------
async def test_volume_gate_blocks_zero_volume(bus, store):
    orch = Orchestrator(bus, store, _throttle())
    await orch.start()
    await _seed_kream_product(store, pid=42, volume_7d=0)

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

    try:
        cand = _candidate(pid=42, retail_price=100_000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        assert call_count["n"] == 0, "거래량 0 후보는 handler 도달 전 차단"
        stats = orch.stats()
        assert stats.get("candidate_prefilter_low_volume", 0) == 1
        assert stats.get("candidate_dropped_throttle", 0) == 0

        db = store._require_db()  # noqa: SLF001
        async with db.execute(
            "SELECT reason FROM decision_log WHERE kream_product_id = 42"
        ) as cur:
            reasons = [r[0] for r in await cur.fetchall()]
        assert REASON_PREFILTER_LOW_VOLUME in reasons
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 7) volume gate — volume_7d>=1 이면 통과 (이후 prefilter_blocks 위임)
# ----------------------------------------------------------------------
async def test_volume_gate_passes_with_volume(bus, store):
    orch = Orchestrator(bus, store, _throttle())
    await orch.start()
    await _seed_kream_product(store, pid=42, volume_7d=5)
    # kream_price_history 비워두면 prefilter_blocks 도 fail-open → handler 도달
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

    try:
        cand = _candidate(pid=42, retail_price=100_000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        assert call_count["n"] == 1
        assert orch.stats().get("candidate_prefilter_low_volume", 0) == 0
    finally:
        await orch.stop()


# ----------------------------------------------------------------------
# 8) volume gate — kream_products row 없으면 fail-open (신상 보호)
# ----------------------------------------------------------------------
async def test_volume_gate_fails_open_on_missing_row(bus, store):
    orch = Orchestrator(bus, store, _throttle())
    await orch.start()
    # kream_products 비움. 신상 = DB 미수집 → 통과해야 handler 가 실시간 fetch
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

    try:
        cand = _candidate(pid=999, retail_price=100_000)
        ckpt = await store.record(cand, consumer="candidate")
        await orch._process_candidate(cand, ckpt)

        assert call_count["n"] == 1
        assert orch.stats().get("candidate_prefilter_low_volume", 0) == 0
    finally:
        await orch.stop()
