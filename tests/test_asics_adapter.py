"""Asics 푸시 어댑터 테스트 (Phase 3 배치 6).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.asics_adapter import ASICS_SKU_RE, AsicsAdapter
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


class _FakeAsicsHttp:
    """fetch_category_products + fetch_product_detail 를 mock.

    - ``categories``: dict[str, list[tile_dict]] (category_code → tiles)
    - ``details``:    dict[str, detail_dict]     (product_id → detail)
    """

    def __init__(
        self,
        categories: dict[str, list[dict]],
        details: dict[str, dict],
    ):
        self._categories = categories
        self._details = details
        self.category_calls: list[str] = []
        self.detail_calls: list[str] = []

    async def fetch_category_products(self, category_code: str) -> list[dict]:
        self.category_calls.append(category_code)
        return list(self._categories.get(category_code, []))

    async def fetch_product_detail(self, product_id: str) -> dict:
        self.detail_calls.append(product_id)
        return dict(self._details.get(product_id, {}))


def _mk_tile(product_id: str, name: str, price_krw: int) -> dict:
    return {
        "product_id": product_id,
        "name": name,
        "url": f"https://www.asics.co.kr/p/{product_id}",
        "price_krw": price_krw,
    }


def _mk_detail(
    model: str,
    name: str = "",
    price_krw: int = 0,
    sizes: list[dict] | None = None,
) -> dict:
    sz = list(sizes or [])
    available = any(s.get("available") for s in sz) if sz else True
    return {
        "model_number": model,
        "name": name,
        "price_krw": price_krw,
        "sizes": sz,
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
            # 존재 — 노바블라스트 5 블랙
            {
                "product_id": "701",
                "name": "아식스 노바블라스트 5 블랙 캐리어 그레이",
                "model_number": "1011B974-002",
                "brand": "Asics",
            },
            # 존재 — 슈퍼블라스트 3 코발트
            {
                "product_id": "702",
                "name": "아식스 슈퍼블라스트 3 코발트 버스트 라이트 오렌지",
                "model_number": "1013A177-400",
                "brand": "Asics",
            },
            # 존재 — 콜라보 (sacai) 가드 테스트
            {
                "product_id": "703",
                "name": "아식스 젤 카야노 14 x sacai 블랙",
                "model_number": "1203B111-001",
                "brand": "Asics",
            },
        ],
    )
    return str(path)


# ─── (a) dump_catalog: 카테고리 순회 + 상세 보강 + 이벤트 ───


async def test_dump_catalog_publishes_event(bus, kream_db):
    fake = _FakeAsicsHttp(
        categories={
            "catA": [
                _mk_tile("20406", "아식스 노바블라스트 5", 159000),
                _mk_tile("20407", "아식스 슈퍼블라스트 3", 219000),
            ],
            "catB": [
                # 다른 카테고리에 중복 노출 — dedup 되어야 함
                _mk_tile("20406", "아식스 노바블라스트 5", 159000),
                _mk_tile("20408", "아식스 젤 카야노", 199000),
            ],
        },
        details={
            "20406": _mk_detail(
                "1011B974-002",
                name="아식스 노바블라스트 5 블랙",
                price_krw=159000,
                sizes=[{"size": "270", "available": True}],
            ),
            "20407": _mk_detail(
                "1013A177-400",
                name="아식스 슈퍼블라스트 3 코발트",
                price_krw=219000,
                sizes=[{"size": "275", "available": True}],
            ),
            "20408": _mk_detail(
                "1203A999-001",  # 미등재 — collect_queue 대상
                name="아식스 젤 카야노 14",
                price_krw=199000,
                sizes=[{"size": "270", "available": True}],
            ),
        },
    )
    adapter = AsicsAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        categories=("catA", "catB"),
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, variants = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    # 3개 master dedup
    assert event.source == "asics"
    assert event.product_count == 3
    assert len(variants) == 3

    # 카테고리 순서 호출 + 3개 상세 보강
    assert fake.category_calls == ["catA", "catB"]
    assert sorted(fake.detail_calls) == ["20406", "20407", "20408"]
    assert received and received[0].product_count == 3

    # 상세 보강 검증 — 모델번호가 tile 에 없다가 채워져야 함
    by_pid = {v["product_id"]: v for v in variants}
    assert by_pid["20406"]["model_number"] == "1011B974-002"
    assert by_pid["20407"]["model_number"] == "1013A177-400"
    assert by_pid["20408"]["model_number"] == "1203A999-001"


# ─── (b) SKU 정규식 필터 ──────────────────────────────────


def test_asics_sku_regex():
    assert ASICS_SKU_RE.match("1011B974-002")
    assert ASICS_SKU_RE.match("1013A177-400")
    assert ASICS_SKU_RE.match("1203A879-021")
    # 불량
    assert not ASICS_SKU_RE.match("1011B974")         # 색상코드 누락
    assert not ASICS_SKU_RE.match("1011B974-0021")    # 4자리 색상
    assert not ASICS_SKU_RE.match("1011B-974-002")    # 위치 이상
    assert not ASICS_SKU_RE.match("L41195100")        # 살로몬 포맷
    assert not ASICS_SKU_RE.match("1011b974-002")     # 소문자
    assert not ASICS_SKU_RE.match("")


async def test_invalid_sku_dropped(bus, kream_db):
    adapter = AsicsAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakeAsicsHttp(categories={}, details={}),
    )
    variants = [
        # valid + 존재
        {
            "product_id": "701",
            "name": "노바블라스트 5",
            "url": "https://www.asics.co.kr/p/701",
            "model_number": "1011B974-002",
            "price_krw": 159000,
            "sizes": [{"size": "270", "available": True}],
            "available": True,
        },
        # invalid — 살로몬 포맷
        {
            "product_id": "999",
            "name": "junk1",
            "url": "https://www.asics.co.kr/p/999",
            "model_number": "L41195100",
            "price_krw": 180000,
            "sizes": [],
            "available": True,
        },
        # invalid — 빈 모델번호
        {
            "product_id": "998",
            "name": "junk2",
            "url": "https://www.asics.co.kr/p/998",
            "model_number": "",
            "price_krw": 180000,
            "sizes": [],
            "available": True,
        },
    ]

    matches, stats = await adapter.match_to_kream(variants)
    assert stats.matched == 1
    assert stats.invalid_sku == 1
    assert stats.no_model_number == 1
    assert len(matches) == 1
    assert matches[0].model_no == "1011B974-002"


# ─── (c) match_to_kream — 품절/큐/가드 ─────────────────────


async def test_match_to_kream_classifies(bus, kream_db):
    adapter = AsicsAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakeAsicsHttp(categories={}, details={}),
    )

    variants = [
        # (1) 정상 매칭 — 노바블라스트 5 (사이즈별 재고 있음)
        {
            "product_id": "20406",
            "name": "아식스 노바블라스트 5 블랙",
            "url": "https://www.asics.co.kr/p/20406",
            "model_number": "1011B974-002",
            "price_krw": 159000,
            "sizes": [
                {"size": "270", "available": False},
                {"size": "280", "available": True},
            ],
            "available": True,
        },
        # (2) 미등재 신상 — collect_queue 에 적재
        {
            "product_id": "20408",
            "name": "아식스 젤 카야노 신상",
            "url": "https://www.asics.co.kr/p/20408",
            "model_number": "1203A999-001",
            "price_krw": 199000,
            "sizes": [{"size": "270", "available": True}],
            "available": True,
        },
        # (3) 콜라보 가드 차단 — 크림 이름에 'sacai' 포함, 소싱 이름은 일반
        {
            "product_id": "20409",
            "name": "아식스 젤 카야노 14",
            "url": "https://www.asics.co.kr/p/20409",
            "model_number": "1203B111-001",
            "price_krw": 259000,
            "sizes": [{"size": "270", "available": True}],
            "available": True,
        },
        # (4) 품절 — 전체 available=False 로 드롭
        {
            "product_id": "20410",
            "name": "아식스 노바블라스트 5 (품절)",
            "url": "https://www.asics.co.kr/p/20410",
            "model_number": "1011B974-002",
            "price_krw": 159000,
            "sizes": [],
            "available": False,
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(variants)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.dumped == 4
    assert stats.soldout_dropped == 1
    assert stats.matched == 1
    assert stats.collected_to_queue == 1
    assert stats.skipped_guard == 1
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert cand.source == "asics"
    assert cand.kream_product_id == 701
    assert cand.model_no == "1011B974-002"
    assert cand.retail_price == 159000
    # 첫 available=True 사이즈가 대표로 선택 (270 품절 → 280)
    assert cand.size == "280"
    assert "20406" in cand.url

    # collect_queue 에 1203A999-001 쌓여야 함
    assert _count_queue(kream_db) == 1


# ─── (d) run_once 통계 정확성 ─────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake = _FakeAsicsHttp(
        categories={
            "cat1": [
                _mk_tile("20406", "노바블라스트 5", 159000),
                _mk_tile("20408", "신상", 199000),
            ],
        },
        details={
            "20406": _mk_detail(
                "1011B974-002",
                name="노바블라스트 5 블랙",
                price_krw=159000,
                sizes=[{"size": "270", "available": True}],
            ),
            "20408": _mk_detail(
                "1203A999-001",
                name="신상",
                price_krw=199000,
                sizes=[{"size": "270", "available": True}],
            ),
        },
    )
    adapter = AsicsAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        categories=("cat1",),
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["collected_to_queue"] == 1
    assert stats["soldout_dropped"] == 0
    assert stats["invalid_sku"] == 0


# ─── (e) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────


async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake = _FakeAsicsHttp(
        categories={
            "cat1": [_mk_tile("20406", "노바블라스트 5", 159000)],
        },
        details={
            "20406": _mk_detail(
                "1011B974-002",
                name="노바블라스트 5 블랙",
                price_krw=159000,
                sizes=[{"size": "270", "available": True}],
            ),
        },
    )
    adapter = AsicsAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake,
        categories=("cat1",),
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
            kream_sell_price=230000,
            net_profit=55000,
            roi=0.34,
            signal="STRONG_BUY",
            volume_7d=9,
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
