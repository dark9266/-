"""KreamDeltaWatcher 어댑터 테스트 (Phase 3).

실호출 금지 — 크림 client 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.kream_delta_watcher import (
    DeltaPollStats,
    KreamDeltaWatcher,
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


# ─── Mock 클라이언트 ─────────────────────────────────────

class _FakeDeltaClient:
    """경량 조회 + 상세 조회 mock.

    light_sequence: poll_once 차수별 light fetch 결과 리스트
    detail_snapshots: pid → 상세 스냅샷
    """

    def __init__(
        self,
        light_sequence: list[list[dict]],
        detail_snapshots: dict[int, dict],
    ):
        self._light_seq = light_sequence
        self._light_idx = 0
        self._details = detail_snapshots
        self.light_calls = 0
        self.detail_calls: list[int] = []

    async def fetch_light(self, product_ids: list[int]) -> list[dict]:
        self.light_calls += 1
        i = min(self._light_idx, len(self._light_seq) - 1)
        self._light_idx += 1
        return list(self._light_seq[i])

    async def get_snapshot(self, product_id: int) -> dict | None:
        self.detail_calls.append(product_id)
        return self._details.get(product_id)


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
                "volume_7d": 10,
            },
            {
                "product_id": "202",
                "name": "Nike Dunk Low Panda",
                "model_number": "DD1391-100",
                "brand": "Nike",
                "volume_7d": 7,
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
                "volume_7d": 5,
            },
        ],
    )
    return str(path)


# ─── (a) watch_targets 로드 ─────────────────────────────

async def test_load_watch_targets_filters(bus, kream_db):
    watcher = KreamDeltaWatcher(bus=bus, db_path=kream_db)
    ids = await watcher.load_watch_targets()
    assert set(ids) == {101, 202, 404}
    # 정렬: volume DESC → 101(10) 202(7) 404(5)
    assert ids == [101, 202, 404]


# ─── (b) 변경 감지 → 상세 조회 → publish ─────────────────

async def test_poll_once_detects_change_and_publishes(bus, kream_db):
    """1차: baseline 마킹 → 2차: 101 가격 변경 → 101 만 상세 조회·publish."""
    light_poll1 = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]
    # 101 가격 변경
    light_poll2 = [
        {"kream_product_id": 101, "sell_now_price": 220_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]

    client = _FakeDeltaClient(
        light_sequence=[light_poll1, light_poll2],
        detail_snapshots={
            101: {"sell_now_price": 220_000, "volume_7d": 10, "top_size": "270"},
            202: {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
            404: {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
        },
    )
    watcher = KreamDeltaWatcher(bus=bus, db_path=kream_db, kream_client=client)
    queue = bus.subscribe(CandidateMatched)

    # 1차: 전부 신규 → 전부 상세 조회·publish (초기 부팅 시)
    s1 = await watcher.poll_once()
    assert s1.targets == 3
    assert s1.light_fetch_calls == 1
    assert s1.changed == 3
    assert s1.detail_fetch_calls == 3
    assert s1.published == 3

    # 구독 큐 초기화
    while not queue.empty():
        queue.get_nowait()

    # 2차: 101 만 변경 → 101 만 상세 조회·publish
    s2 = await watcher.poll_once()
    assert s2.targets == 3
    assert s2.light_fetch_calls == 1
    assert s2.changed == 1
    assert s2.detail_fetch_calls == 1
    assert s2.published == 1

    received = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(received) == 1
    cand = received[0]
    assert cand.source == "kream_delta"
    assert cand.kream_product_id == 101
    assert cand.retail_price == 220_000
    assert cand.model_no == "CW2288-111"
    assert "101" in cand.url


# ─── (c) 변경 없음 → detail 호출 0, publish 0 ─────────────

async def test_poll_once_no_change_no_detail_calls(bus, kream_db):
    light = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]
    client = _FakeDeltaClient(
        light_sequence=[light, light, light],
        detail_snapshots={
            101: {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
            202: {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
            404: {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
        },
    )
    watcher = KreamDeltaWatcher(bus=bus, db_path=kream_db, kream_client=client)

    # 1차: baseline (전부 신규)
    s1 = await watcher.poll_once()
    assert s1.changed == 3
    detail_calls_after_1 = len(client.detail_calls)

    # 2차: 변경 없음
    s2 = await watcher.poll_once()
    assert s2.changed == 0
    assert s2.detail_fetch_calls == 0
    assert s2.published == 0
    # 추가 detail 호출 없어야 함
    assert len(client.detail_calls) == detail_calls_after_1

    # 3차: 여전히 변경 없음
    s3 = await watcher.poll_once()
    assert s3.changed == 0
    assert s3.detail_fetch_calls == 0
    assert len(client.detail_calls) == detail_calls_after_1


# ─── (d) max_changes_per_poll 상한 ────────────────────────

async def test_poll_once_respects_max_changes_cap(bus, kream_db):
    """1차: 신규 3건 중 2건만 처리. 나머지는 skipped."""
    light = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]
    client = _FakeDeltaClient(
        light_sequence=[light],
        detail_snapshots={
            101: {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
            202: {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
            404: {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
        },
    )
    watcher = KreamDeltaWatcher(
        bus=bus,
        db_path=kream_db,
        kream_client=client,
        max_changes_per_poll=2,
    )

    stats = await watcher.poll_once()
    assert stats.changed == 3
    assert stats.skipped_over_cap == 1
    assert stats.detail_fetch_calls == 2
    assert stats.published == 2


# ─── (e) 호출 절감 시뮬레이션 — 1회 light + 0 detail ──────

async def test_call_count_savings_vs_hot_watcher(bus, kream_db):
    """핵심 검증: 변경 없는 사이클에 크림 호출이 1회(light)만 발생.

    hot_watcher = 3건 × 폴링 = 3회/사이클
    delta_watcher = 1회(light) + 0회(detail) = 1회/사이클 (baseline 이후)
    """
    light = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]
    client = _FakeDeltaClient(
        light_sequence=[light] * 10,
        detail_snapshots={
            101: {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
            202: {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
            404: {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
        },
    )
    watcher = KreamDeltaWatcher(bus=bus, db_path=kream_db, kream_client=client)

    # 1차: baseline — 전부 detail 호출 (3회)
    await watcher.poll_once()
    baseline_details = len(client.detail_calls)
    assert baseline_details == 3

    # 다음 9 사이클: 변경 없음 → detail 0
    for _ in range(9):
        await watcher.poll_once()

    # 총 light 10회, 총 detail = 3 (baseline 만)
    assert client.light_calls == 10
    assert len(client.detail_calls) == 3


# ─── (f) stale 감지 (삭제/누락) ───────────────────────────

async def test_poll_once_tracks_stale(bus, kream_db):
    light_poll1 = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]
    # 2차에 202 사라짐
    light_poll2 = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]
    client = _FakeDeltaClient(
        light_sequence=[light_poll1, light_poll2],
        detail_snapshots={
            101: {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
            202: {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
            404: {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
        },
    )
    watcher = KreamDeltaWatcher(bus=bus, db_path=kream_db, kream_client=client)

    await watcher.poll_once()  # baseline 3건
    s2 = await watcher.poll_once()
    assert s2.stale == 1  # 202 사라짐
    assert s2.changed == 0
    assert s2.detail_fetch_calls == 0


# ─── (g) 개별 예외 격리 ─────────────────────────────────

async def test_detail_fetch_error_isolated(bus, kream_db):
    light = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]

    class _PartiallyExploding(_FakeDeltaClient):
        async def get_snapshot(self, product_id: int):
            self.detail_calls.append(product_id)
            if product_id == 101:
                raise RuntimeError("boom 101")
            return self._details.get(product_id)

    client = _PartiallyExploding(
        light_sequence=[light],
        detail_snapshots={
            202: {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
            404: {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
        },
    )
    watcher = KreamDeltaWatcher(bus=bus, db_path=kream_db, kream_client=client)
    queue = bus.subscribe(CandidateMatched)

    stats = await watcher.poll_once()
    assert stats.errors >= 1
    # 101 예외가 202/404 publish 를 막아선 안됨
    pids = {e.kream_product_id for e in [queue.get_nowait() for _ in range(queue.qsize())]}
    assert 202 in pids
    assert 404 in pids


# ─── (h) client 미주입 시 스킵 ────────────────────────────

async def test_poll_once_skips_without_client(bus, kream_db):
    watcher = KreamDeltaWatcher(bus=bus, db_path=kream_db)
    stats = await watcher.poll_once()
    assert stats.targets == 0  # light 호출 자체를 안함
    assert stats.published == 0


# ─── (i) E2E: watcher → bus → orchestrator → AlertSent ───

async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    light_poll1 = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]
    light_poll2 = [
        {"kream_product_id": 101, "sell_now_price": 230_000, "volume_7d": 10, "sold_count": 3},
        {"kream_product_id": 202, "sell_now_price": 150_000, "volume_7d": 7, "sold_count": 2},
        {"kream_product_id": 404, "sell_now_price": 300_000, "volume_7d": 5, "sold_count": 1},
    ]
    client = _FakeDeltaClient(
        light_sequence=[light_poll1, light_poll2],
        detail_snapshots={
            101: {"sell_now_price": 230_000, "volume_7d": 10, "top_size": "270"},
            202: {"sell_now_price": 150_000, "volume_7d": 7, "top_size": "260"},
            404: {"sell_now_price": 300_000, "volume_7d": 5, "top_size": "275"},
        },
    )
    watcher = KreamDeltaWatcher(bus=bus, db_path=kream_db, kream_client=client)

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
        assert event.source == "kream_delta"
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
            fired_at=333.0,
        )
        alerts.append(sent)
        return sent

    orch.on_catalog_dumped(catalog_handler)
    orch.on_candidate_matched(candidate_handler)
    orch.on_profit_found(profit_handler)

    await orch.start()
    try:
        await watcher.poll_once()  # baseline: 3건 publish
        await watcher.poll_once()  # 101 만 변경 publish

        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if len(alerts) >= 4:  # baseline 3 + delta 1
                break
            await asyncio.sleep(0.01)

        assert len(alerts) >= 4
        sources = {a.kream_product_id for a in alerts}
        assert 101 in sources
    finally:
        await orch.stop()
        await store.close()


# ─── (j) run_forever / stop ───────────────────────────────

async def test_run_forever_stops_cleanly(bus, kream_db):
    light = [
        {"kream_product_id": 101, "sell_now_price": 200_000, "volume_7d": 10, "sold_count": 3},
    ]
    client = _FakeDeltaClient(
        light_sequence=[light] * 5,
        detail_snapshots={
            101: {"sell_now_price": 200_000, "volume_7d": 10, "top_size": "270"},
        },
    )
    watcher = KreamDeltaWatcher(
        bus=bus,
        db_path=kream_db,
        kream_client=client,
        poll_interval_sec=60,
    )

    task = asyncio.create_task(watcher.run_forever())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if client.light_calls >= 1:
            break
    assert client.light_calls >= 1

    await watcher.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()


# ─── (k) DeltaPollStats 기본 ──────────────────────────────

def test_delta_poll_stats_as_dict():
    s = DeltaPollStats(targets=3, changed=1, published=1, light_fetch_calls=1)
    d = s.as_dict()
    assert d["targets"] == 3
    assert d["changed"] == 1
    assert d["published"] == 1
    assert d["light_fetch_calls"] == 1
    assert "started_at" in d
