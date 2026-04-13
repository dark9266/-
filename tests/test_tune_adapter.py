"""튠 푸시 어댑터 테스트 (Phase 3 배치 3).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.tune_adapter import TuneAdapter
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
    category TEXT DEFAULT 'sneakers',
    image_url TEXT DEFAULT '',
    url TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kream_collect_queue (
    model_number TEXT PRIMARY KEY,
    brand_hint TEXT DEFAULT '',
    name_hint TEXT DEFAULT '',
    source TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0
);
"""


def _init_kream_db(path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_KREAM_SCHEMA)
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO kream_products "
                "(product_id, name, model_number, brand) VALUES (?, ?, ?, ?)",
                (r["product_id"], r["name"], r["model_number"], r.get("brand", "")),
            )
        conn.commit()
    finally:
        conn.close()


def _count_queue(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM kream_collect_queue")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


# ─── mock HTTP 레이어 ─────────────────────────────────────


class _FakeTuneHttp:
    """fetch_products_page(cursor, first) 만 mock.

    pages 리스트는 (nodes, next_cursor) 튜플 시퀀스. 호출 순서대로 소비.
    """

    def __init__(self, pages: list[tuple[list[dict], str | None]]):
        self._pages = list(pages)
        self.calls: list[tuple[str | None, int]] = []

    async def fetch_products_page(
        self, cursor: str | None, first: int = 250
    ) -> tuple[list[dict], str | None]:
        self.calls.append((cursor, first))
        if not self._pages:
            return [], None
        return self._pages.pop(0)


def _mk_product(
    handle: str,
    title: str,
    vendor: str,
    variants: list[dict],
) -> dict:
    """Shopify GraphQL product node 모방."""
    return {
        "id": f"gid://shopify/Product/{handle}",
        "handle": handle,
        "title": title,
        "vendor": vendor,
        "variants": {
            "edges": [{"node": v} for v in variants],
        },
    }


def _mk_variant(
    model_no: str,
    size: str = "270",
    price: str = "180000",
    available: bool = True,
) -> dict:
    vtitle = f"{model_no} / {size}" if size else model_no
    return {
        "title": vtitle,
        "price": {"amount": price, "currencyCode": "KRW"},
        "availableForSale": available,
    }


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
            # 존재 — 나이키 일반 모델
            {
                "product_id": "701",
                "name": "Nike Air Force 1 Low White",
                "model_number": "CW2288-111",
                "brand": "Nike",
            },
            # 존재 — 나이키 Supreme 콜라보 (콜라보 가드 테스트용)
            {
                "product_id": "702",
                "name": "Nike Air Force 1 Supreme White",
                "model_number": "CU9225-100",
                "brand": "Nike",
            },
        ],
    )
    return str(path)


# ─── (a) dump_catalog publish + 커서 페이지네이션 ─────────


async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeTuneHttp(
        pages=[
            (
                [
                    _mk_product(
                        "air-force-1",
                        "Nike Air Force 1 Low White",
                        "Nike",
                        [
                            _mk_variant("CW2288-111", "270"),
                            _mk_variant("CW2288-111", "280"),
                        ],
                    ),
                    _mk_product(
                        "dunk-low",
                        "Nike Dunk Low Panda",
                        "Nike",
                        [_mk_variant("DD1391-100", "270")],
                    ),
                ],
                "cursor-page-2",
            ),
            # 두 번째 페이지 비어 → 종료
            ([], None),
        ]
    )
    adapter = TuneAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        max_pages=5,
        page_size=250,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, variants = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "tune"
    assert event.product_count == 3  # variant flat 전개
    assert len(variants) == 3
    # 커서 진행 확인: 첫 호출 None, 두 번째 호출 "cursor-page-2"
    assert fake_http.calls[0] == (None, 250)
    assert fake_http.calls[1] == ("cursor-page-2", 250)
    assert received and received[0].product_count == 3

    # variant 파싱 확인 (title → sku/size)
    first = variants[0]
    assert first["sku"] == "CW2288-111"
    assert first["size"] == "270"
    assert first["price"] == 180000
    assert first["available"] is True
    assert first["handle"] == "air-force-1"
    assert first["vendor"] == "Nike"


# ─── (b) match_to_kream — 3케이스 (매칭/큐/가드/품절) ───────


async def test_match_to_kream_classifies(bus, kream_db):
    adapter = TuneAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakeTuneHttp(pages=[]),
    )

    variants = [
        # (1) 정상 매칭 — AF1 white (사이즈별 variant 2개 → dedup)
        {
            "handle": "air-force-1",
            "title": "Nike Air Force 1 Low White",
            "vendor": "Nike",
            "sku": "CW2288-111",
            "size": "270",
            "price": 180000,
            "available": True,
        },
        {
            "handle": "air-force-1",
            "title": "Nike Air Force 1 Low White",
            "vendor": "Nike",
            "sku": "CW2288-111",
            "size": "280",
            "price": 180000,
            "available": True,
        },
        # (2) 미등재 신상 → collect_queue
        {
            "handle": "new-dunk",
            "title": "Nike Dunk Low New Color",
            "vendor": "Nike",
            "sku": "DD9999-001",
            "size": "270",
            "price": 160000,
            "available": True,
        },
        # (3) 콜라보 가드 차단 — 크림=Supreme 콜라보, 소싱은 일반 이름
        {
            "handle": "af1-supreme-lookalike",
            "title": "Nike Air Force 1 Low White",
            "vendor": "Nike",
            "sku": "CU9225-100",
            "size": "270",
            "price": 220000,
            "available": True,
        },
        # (4) 품절
        {
            "handle": "af1-sold",
            "title": "Nike Air Force 1 Low White",
            "vendor": "Nike",
            "sku": "CW2288-111",
            "size": "290",
            "price": 180000,
            "available": False,
        },
        # (5) 모델번호 없음
        {
            "handle": "no-model",
            "title": "무지 상품",
            "vendor": "",
            "sku": "",
            "size": "270",
            "price": 50000,
            "available": True,
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(variants)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.dumped == 6
    assert stats.soldout_dropped == 1
    assert stats.no_model_number == 1
    assert stats.matched == 1  # dedup: 사이즈별 variant 2개 → 1회
    assert stats.collected_to_queue == 1
    assert stats.skipped_guard == 1
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert cand.source == "tune"
    assert cand.kream_product_id == 701
    assert cand.model_no == "CW2288-111"
    assert cand.retail_price == 180000
    assert cand.size == "270"
    assert "air-force-1" in cand.url

    # collect_queue 에 DD9999-001 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (c) run_once 통계 정확성 ─────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeTuneHttp(
        pages=[
            (
                [
                    _mk_product(
                        "af1",
                        "Nike Air Force 1 Low White",
                        "Nike",
                        [_mk_variant("CW2288-111", "270", "180000", True)],
                    ),
                    _mk_product(
                        "dunk-new",
                        "Nike Dunk Low New",
                        "Nike",
                        [_mk_variant("DD9999-001", "270", "160000", True)],
                    ),
                ],
                None,
            ),
        ]
    )
    adapter = TuneAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        max_pages=2,
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["collected_to_queue"] == 1
    assert stats["soldout_dropped"] == 0


# ─── (d) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────


async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeTuneHttp(
        pages=[
            (
                [
                    _mk_product(
                        "af1",
                        "Nike Air Force 1 Low White",
                        "Nike",
                        [_mk_variant("CW2288-111", "270", "180000", True)],
                    ),
                ],
                None,
            )
        ]
    )
    adapter = TuneAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        max_pages=1,
    )

    ckpt_path = tmp_path / "ckpt.db"
    store = CheckpointStore(str(ckpt_path))
    await store.init()
    throttle = CallThrottle(rate_per_min=6000.0, burst=100)
    orch = Orchestrator(bus, store, throttle)

    alerts: list[AlertSent] = []

    async def catalog_handler(
        event: CatalogDumped,
    ) -> AsyncIterator[CandidateMatched]:
        if False:
            yield  # pragma: no cover

    async def candidate_handler(
        event: CandidateMatched,
    ) -> ProfitFound | None:
        return ProfitFound(
            source=event.source,
            kream_product_id=event.kream_product_id,
            model_no=event.model_no,
            size=event.size or "270",
            retail_price=event.retail_price,
            kream_sell_price=260000,
            net_profit=60000,
            roi=0.33,
            signal="STRONG_BUY",
            volume_7d=8,
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
        await adapter.run_once()

        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if alerts:
                break
            await asyncio.sleep(0.01)

        assert len(alerts) == 1
        sent = alerts[0]
        assert sent.kream_product_id == 701
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
