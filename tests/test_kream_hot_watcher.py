"""크림 hot 감시 어댑터 테스트 (Phase 2.5).

실호출 금지: 크림 client 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.kream_hot_watcher import (
    DEFAULT_HOT_VOLUME_THRESHOLD,
    KreamHotWatcher,
    PollStats,
)
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

# ─── DB 헬퍼 ──────────────────────────────────────────────

_KREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS kream_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_number TEXT NOT NULL,
    brand TEXT DEFAULT '',
    volume_7d INTEGER DEFAULT 0
);
"""


def _init_kream_db(path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_KREAM_SCHEMA)
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO kream_products "
                "(product_id, name, model_number, brand, volume_7d) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    r["product_id"],
                    r["name"],
                    r["model_number"],
                    r.get("brand", ""),
                    r.get("volume_7d", 0),
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ─── mock 크림 client ─────────────────────────────────────

class _FakeKreamClient:
    """pid → 순차 스냅샷 리스트. 호출마다 하나씩 뽑아서 반환."""

    def __init__(self, snapshots: dict[int, list[dict | None]]):
        self._snapshots = snapshots
        self._idx: dict[int, int] = {pid: 0 for pid in snapshots}
        self.calls: list[int] = []

    async def get_snapshot(self, product_id: int) -> dict | None:
        self.calls.append(product_id)
        seq = self._snapshots.get(product_id, [])
        if not seq:
            return None
        i = self._idx.get(product_id, 0)
        data = seq[min(i, len(seq) - 1)]
        self._idx[product_id] = i + 1
        return data


class _ExplodingKreamClient:
    """특정 pid 호출 시 예외. 나머지는 정상."""

    def __init__(self, ok_snapshots: dict[int, dict], exploding_pids: set[int]):
        self._ok = ok_snapshots
        self._bad = exploding_pids

    async def get_snapshot(self, product_id: int) -> dict | None:
        if product_id in self._bad:
            raise RuntimeError(f"boom: {product_id}")
        return self._ok.get(product_id)


# ─── fixtures ─────────────────────────────────────────────

@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def kream_db(tmp_path):
    path = tmp_path / "kream.db"
    _init_kream_db(
        str(path),
        rows=[
            {
                "product_id": "101",
                "name": "Nike Air Force 1 Low White",
                "model_number": "CW2288-111",
                "brand": "Nike",
                "volume_7d": 10,  # hot
            },
            {
                "product_id": "202",
                "name": "Nike Dunk Low Panda",
                "model_number": "DD1391-100",
                "brand": "Nike",
                "volume_7d": 7,  # hot
            },
            {
                "product_id": "303",
                "name": "Nike Air Max 90",
                "model_number": "DM0029-100",
                "brand": "Nike",
                "volume_7d": 2,  # cold — 제외
            },
            {
                "product_id": "404",
                "name": "Jordan 1 High",
                "model_number": "555088-063",
                "brand": "Jordan",
                "volume_7d": 5,  # threshold 경계
            },
        ],
    )
    return str(path)


# ─── (a) load_hot_products ────────────────────────────────

async def test_load_hot_products_filters_by_volume(bus, kream_db):
    watcher = KreamHotWatcher(bus=bus, db_path=kream_db)
    hot = await watcher.load_hot_products()

    pids = {row["product_id"] for row in hot}
    # 101 (10), 202 (7), 404 (5) 만. 303 (2) 제외.
    assert pids == {"101", "202", "404"}
    assert all(row["volume_7d"] >= DEFAULT_HOT_VOLUME_THRESHOLD for row in hot)
    # 정렬: volume_7d DESC
    assert [r["volume_7d"] for r in hot] == [10, 7, 5]


# ─── (b) 가격 급등 감지 ──────────────────────────────────

async def test_poll_once_detects_price_spike(bus, kream_db):
    # 2회 스냅샷: 1회차 baseline, 2회차 +10% → 급등
    client = _FakeKreamClient(
        snapshots={
            101: [
                {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
                {"sell_now_price": 220_000, "volume_7d": 10, "top_size": "270"},
            ],
            202: [
                {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
                {"sell_now_price": 151_000, "volume_7d": 7, "top_size": "260"},
            ],
            404: [
                {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
                {"sell_now_price": 300_500, "volume_7d": 5, "top_size": "275"},
            ],
        }
    )
    watcher = KreamHotWatcher(bus=bus, db_path=kream_db, kream_client=client)
    queue = bus.subscribe(CandidateMatched)

    s1 = await watcher.poll_once()
    assert s1.baseline_initialized == 3
    assert s1.spiked == 0
    assert s1.published == 0
    assert queue.empty()

    s2 = await watcher.poll_once()
    assert s2.polled == 3
    assert s2.spiked == 1
    assert s2.published == 1

    # 발행된 이벤트 확인
    received: list[CandidateMatched] = []
    while not queue.empty():
        received.append(queue.get_nowait())
    assert len(received) == 1
    cand = received[0]
    assert cand.source == "kream_hot"
    assert cand.kream_product_id == 101
    assert cand.retail_price == 220_000
    assert cand.size == "270"
    assert cand.model_no == "CW2288-111"
    assert "101" in cand.url


# ─── (c) 거래량 급등 감지 (5→15) ─────────────────────────

async def test_poll_once_detects_volume_spike(bus, kream_db):
    client = _FakeKreamClient(
        snapshots={
            101: [
                {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
                {"sell_now_price": 201_000, "volume_7d": 10, "top_size": "270"},
            ],
            202: [
                # 가격은 거의 그대로, 거래량 7 → 20 (2.86배)
                {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
                {"sell_now_price": 150_500, "volume_7d": 20, "top_size": "260"},
            ],
            404: [
                {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
                {"sell_now_price": 300_100, "volume_7d": 5, "top_size": "275"},
            ],
        }
    )
    watcher = KreamHotWatcher(bus=bus, db_path=kream_db, kream_client=client)
    queue = bus.subscribe(CandidateMatched)

    await watcher.poll_once()  # baseline
    stats = await watcher.poll_once()

    assert stats.spiked == 1
    assert stats.published == 1

    received = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(received) == 1
    assert received[0].kream_product_id == 202
    assert received[0].source == "kream_hot"


# ─── (d) 변동 없음 → publish 0 ───────────────────────────

async def test_poll_once_no_change_publishes_nothing(bus, kream_db):
    client = _FakeKreamClient(
        snapshots={
            101: [
                {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
                {"sell_now_price": 201_000, "volume_7d": 10, "top_size": "270"},
                {"sell_now_price": 202_000, "volume_7d": 10, "top_size": "270"},
            ],
            202: [
                {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
                {"sell_now_price": 150_500, "volume_7d": 7, "top_size": "260"},
                {"sell_now_price": 151_000, "volume_7d": 8, "top_size": "260"},
            ],
            404: [
                {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
                {"sell_now_price": 300_100, "volume_7d": 5, "top_size": "275"},
                {"sell_now_price": 300_200, "volume_7d": 5, "top_size": "275"},
            ],
        }
    )
    watcher = KreamHotWatcher(bus=bus, db_path=kream_db, kream_client=client)
    queue = bus.subscribe(CandidateMatched)

    for _ in range(3):
        await watcher.poll_once()

    assert queue.empty()
    assert bus.published_count(CandidateMatched) == 0


# ─── (e) 개별 예외 격리 ──────────────────────────────────

async def test_poll_once_isolates_individual_errors(bus, kream_db):
    # 101 폭발, 202/404 정상 — 101 예외가 다른 상품 처리 막으면 안 됨
    client = _ExplodingKreamClient(
        ok_snapshots={
            202: {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
            404: {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
        },
        exploding_pids={101},
    )
    watcher = KreamHotWatcher(bus=bus, db_path=kream_db, kream_client=client)

    stats = await watcher.poll_once()

    assert stats.polled == 3
    assert stats.errors == 1  # 101
    # 202/404 는 baseline 초기화 성공
    assert stats.baseline_initialized == 2
    assert stats.spiked == 0


# ─── (f) E2E: watcher → bus → Orchestrator → AlertSent ────

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """KreamHotWatcher → CandidateMatched → ProfitFound → AlertSent 완주."""
    client = _FakeKreamClient(
        snapshots={
            101: [
                {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
                {"sell_now_price": 230_000, "volume_7d": 10, "top_size": "270"},  # +15%
            ],
            202: [
                {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
                {"sell_now_price": 150_500, "volume_7d": 7, "top_size": "260"},
            ],
            404: [
                {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
                {"sell_now_price": 300_500, "volume_7d": 5, "top_size": "275"},
            ],
        }
    )
    watcher = KreamHotWatcher(bus=bus, db_path=kream_db, kream_client=client)

    ckpt_path = tmp_path / "ckpt.db"
    store = CheckpointStore(str(ckpt_path))
    await store.init()
    throttle = CallThrottle(rate_per_min=6000.0, burst=100)
    orch = Orchestrator(bus, store, throttle)

    alerts: list[AlertSent] = []

    async def catalog_handler(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        if False:  # pragma: no cover
            yield

    async def candidate_handler(
        event: CandidateMatched,
    ) -> ProfitFound | None:
        assert event.source == "kream_hot"
        return ProfitFound(
            source=event.source,
            kream_product_id=event.kream_product_id,
            model_no=event.model_no,
            size=event.size,
            retail_price=event.retail_price,
            kream_sell_price=event.retail_price,
            net_profit=30_000,
            roi=0.15,
            signal="BUY",
            volume_7d=10,
            url=event.url,
        )

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        sent = AlertSent(
            alert_id=len(alerts) + 1,
            kream_product_id=event.kream_product_id,
            signal=event.signal,
            fired_at=222.0,
        )
        alerts.append(sent)
        return sent

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        # 1차 = baseline, 2차 = 급등 감지 → publish → orchestrator 체인
        await watcher.poll_once()
        await watcher.poll_once()

        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if alerts:
                break
            await asyncio.sleep(0.01)

        assert len(alerts) == 1
        sent = alerts[0]
        assert sent.kream_product_id == 101
        assert sent.signal == "BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()


# ─── (g) run_forever / stop ───────────────────────────────

async def test_run_forever_stops_cleanly(bus, kream_db):
    client = _FakeKreamClient(
        snapshots={
            101: [{"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"}],
            202: [{"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"}],
            404: [{"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"}],
        }
    )
    watcher = KreamHotWatcher(
        bus=bus,
        db_path=kream_db,
        kream_client=client,
        poll_interval_sec=60,  # 큰 값 → stop 이 wait_for 인터럽트
    )

    task = asyncio.create_task(watcher.run_forever())
    # 최소 1 사이클 돌 때까지 기다림
    for _ in range(50):
        await asyncio.sleep(0.01)
        if client.calls:
            break
    assert client.calls, "poll_once 가 최소 1회 실행되어야 함"

    await watcher.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()


# ─── (h) PollStats 기본 ───────────────────────────────────

def test_pollstats_as_dict():
    s = PollStats(polled=3, spiked=1, published=1)
    d = s.as_dict()
    assert d["polled"] == 3
    assert d["spiked"] == 1
    assert d["published"] == 1
    assert "started_at" in d
