"""살로몬 푸시 어댑터 테스트 (Phase 3 배치 2).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.salomon_adapter import SALOMON_SKU_RE, SalomonAdapter
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


class _FakeSalomonHttp:
    """fetch_products_page(page, limit) 만 mock. pages 는 page 번호→products 리스트."""

    def __init__(self, pages: dict[int, list[dict]]):
        self._pages = pages
        self.calls: list[tuple[int, int]] = []

    async def fetch_products_page(
        self, page: int, limit: int = 250
    ) -> list[dict]:
        self.calls.append((page, limit))
        return list(self._pages.get(page, []))


def _mk_product(
    handle: str,
    title: str,
    variants: list[dict],
) -> dict:
    return {"handle": handle, "title": title, "variants": variants}


def _mk_variant(
    sku: str,
    size: str = "270",
    price: str = "180000",
    available: bool = True,
) -> dict:
    return {
        "sku": sku,
        "option2": size,
        "price": price,
        "available": available,
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
            # 존재 — 살로몬 XT-6
            {
                "product_id": "501",
                "name": "Salomon XT-6 Black",
                "model_number": "L41195100",
                "brand": "Salomon",
            },
            # 존재 — 살로몬 XA Pro Sacai 콜라보 (가드 테스트용)
            {
                "product_id": "502",
                "name": "Salomon XA Pro 3D Sacai",
                "model_number": "L47988000",
                "brand": "Salomon",
            },
        ],
    )
    return str(path)


# ─── (a) dump_catalog publish + 페이지네이션 ─────────────


async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakeSalomonHttp(
        pages={
            1: [
                _mk_product(
                    "xt-6-black",
                    "Salomon XT-6 Black",
                    [_mk_variant("L41195100", "270"), _mk_variant("L41195100", "280")],
                ),
                _mk_product(
                    "xa-pro",
                    "Salomon XA Pro",
                    [_mk_variant("L47988000", "270")],
                ),
            ],
            2: [],  # 두 번째 페이지 비어 → 덤프 종료
        }
    )
    adapter = SalomonAdapter(
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

    # 3개 variant flat 전개
    assert event.source == "salomon"
    assert event.product_count == 3
    assert len(variants) == 3
    # page=1 호출 + page=2 호출 (limit<page_size 조기 종료 경로)
    assert fake_http.calls[0] == (1, 250)
    assert received and received[0].product_count == 3


# ─── (b) SKU 정규식 필터 ──────────────────────────────────


def test_salomon_sku_regex():
    assert SALOMON_SKU_RE.match("L41195100")
    assert SALOMON_SKU_RE.match("L47988000")
    # L + 8자리 숫자가 아닌 것은 전부 reject
    assert not SALOMON_SKU_RE.match("L4119510")      # 7자리
    assert not SALOMON_SKU_RE.match("L411951000")    # 9자리
    assert not SALOMON_SKU_RE.match("M41195100")     # 다른 prefix
    assert not SALOMON_SKU_RE.match("L41195 100")    # 공백
    assert not SALOMON_SKU_RE.match("")


async def test_invalid_sku_dropped(bus, kream_db):
    adapter = SalomonAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakeSalomonHttp(pages={}),
    )
    variants = [
        # valid + 존재
        {
            "handle": "xt-6",
            "title": "Salomon XT-6",
            "sku": "L41195100",
            "size": "270",
            "price": 180000,
            "available": True,
        },
        # invalid SKU — 정규식 불통과
        {
            "handle": "junk-1",
            "title": "Junk 1",
            "sku": "XX123",
            "size": "270",
            "price": 99000,
            "available": True,
        },
        # invalid SKU — 9자리
        {
            "handle": "junk-2",
            "title": "Junk 2",
            "sku": "L123456789",
            "size": "270",
            "price": 99000,
            "available": True,
        },
    ]

    matches, stats = await adapter.match_to_kream(variants)
    assert stats.matched == 1
    assert stats.invalid_sku == 2
    assert len(matches) == 1
    assert matches[0].model_no == "L41195100"


# ─── (c) match_to_kream — 3케이스 (매칭/큐/가드) ───────────


async def test_match_to_kream_classifies(bus, kream_db):
    adapter = SalomonAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakeSalomonHttp(pages={}),
    )

    variants = [
        # (1) 정상 매칭 — XT-6 일반 (사이즈별 variant 2개 → dedup 1회만 매칭)
        {
            "handle": "xt-6",
            "title": "Salomon XT-6 Black",
            "sku": "L41195100",
            "size": "270",
            "price": 180000,
            "available": True,
        },
        {
            "handle": "xt-6",
            "title": "Salomon XT-6 Black",
            "sku": "L41195100",
            "size": "280",
            "price": 180000,
            "available": True,
        },
        # (2) 미등재 신상 → collect_queue
        {
            "handle": "speedcross-new",
            "title": "Salomon Speedcross New",
            "sku": "L99999999",
            "size": "270",
            "price": 160000,
            "available": True,
        },
        # (3) 콜라보 가드 차단 — 크림은 Sacai 콜라보, 소싱은 일반
        # 크림 "Salomon XA Pro 3D Sacai" → collab_match_fails 차단
        {
            "handle": "xa-pro",
            "title": "Salomon XA Pro",
            "sku": "L47988000",
            "size": "270",
            "price": 199000,
            "available": True,
        },
        # (4) 품절 제외
        {
            "handle": "xt-6-sold",
            "title": "Salomon XT-6 (Sold)",
            "sku": "L41195100",
            "size": "290",
            "price": 180000,
            "available": False,
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(variants)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.dumped == 5
    assert stats.soldout_dropped == 1
    assert stats.matched == 1  # dedup: XT-6 variant 2개 → 1회만
    assert stats.collected_to_queue == 1
    # (3)은 크림이름에 "mindesign"(콜라보 키워드)이 있어 차단
    assert stats.skipped_guard == 1
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert cand.source == "salomon"
    assert cand.kream_product_id == 501
    assert cand.model_no == "L41195100"
    assert cand.retail_price == 180000
    assert cand.size == "270"  # 첫 번째 variant 의 사이즈
    assert "xt-6" in cand.url

    # collect_queue 에 L99999999 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (d) run_once 통계 정확성 ─────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeSalomonHttp(
        pages={
            1: [
                _mk_product(
                    "xt-6",
                    "Salomon XT-6 Black",
                    [_mk_variant("L41195100", "270", "180000", True)],
                ),
                _mk_product(
                    "speedcross-new",
                    "Salomon Speedcross New",
                    [_mk_variant("L99999999", "270", "160000", True)],
                ),
            ],
        }
    )
    adapter = SalomonAdapter(
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


# ─── (e) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────


async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeSalomonHttp(
        pages={
            1: [
                _mk_product(
                    "xt-6",
                    "Salomon XT-6 Black",
                    [_mk_variant("L41195100", "270", "180000", True)],
                ),
            ]
        }
    )
    adapter = SalomonAdapter(
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
            fired_at=222.0,
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
        assert sent.kream_product_id == 501
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
