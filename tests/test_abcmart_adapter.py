"""abcmart 푸시 어댑터 테스트 (Phase 3 배치 3).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
2개 채널(그랜드스테이지 + 온더스팟) 을 단일 어댑터가 통합 덤프하는지 검증.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.abcmart_adapter import (
    GRANDSTAGE,
    ONTHESPOT,
    AbcmartAdapter,
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


class _FakeAbcmartHttp:
    """`fetch_channel_listing(channel, max_pages, page_size)` 만 mock."""

    def __init__(self, channel_items: dict[str, list[dict]]):
        self._channel_items = channel_items
        self.calls: list[dict] = []

    async def fetch_channel_listing(
        self, channel: str, *, max_pages: int, page_size: int
    ) -> list[dict]:
        self.calls.append(
            {"channel": channel, "max_pages": max_pages, "page_size": page_size}
        )
        return list(self._channel_items.get(channel, []))


def _item(prdt_no: int, name: str, style: str, color: str = "",
          sell_price: int = 139000, normal_price: int = 159000,
          brand: str = "Nike", sold_out: str = "N") -> dict:
    return {
        "PRDT_NO": prdt_no,
        "PRDT_NAME": name,
        "STYLE_INFO": style,
        "COLOR_ID": color,
        "PRDT_DC_PRICE": sell_price,
        "NRMAL_AMT": normal_price,
        "BRAND_NAME": brand,
        "SOLD_OUT": sold_out,
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
            {
                "product_id": "101",
                "name": "Nike Air Force 1 Low White",
                "model_number": "CW2288-111",
                "brand": "Nike",
            },
            {
                "product_id": "202",
                "name": "Nike Air Force 1 Travis Scott Cactus Jack",
                "model_number": "AQ4211-100",
                "brand": "Nike",
            },
            {
                "product_id": "303",
                "name": "adidas Samba OG White Black",
                "model_number": "B75806",
                "brand": "adidas",
            },
        ],
    )
    return str(path)


# ─── (a) 2채널 통합 덤프 — product_count 합산 ─────────────


async def test_dump_catalog_merges_two_channels(bus, kream_db):
    """그랜드스테이지 + 온더스팟 두 채널을 한 번에 덤프하고 단일 이벤트로 통합."""
    fake_http = _FakeAbcmartHttp(
        channel_items={
            GRANDSTAGE: [
                _item(1001, "나이키 에어포스 1 로우 화이트", "CW2288", "111"),
                _item(1002, "나이키 덩크 로우", "DD1234", "100"),
            ],
            ONTHESPOT: [
                _item(2001, "아디다스 삼바 OG", "B75806", ""),
            ],
        }
    )
    adapter = AbcmartAdapter(bus=bus, db_path=kream_db, http_client=fake_http)

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    # 통합 이벤트 — source="abcmart", count=3 (2+1)
    assert event.source == "abcmart"
    assert event.product_count == 3
    assert len(products) == 3

    # 각 상품에 _channel 메타 유지
    channels = {p["_channel"] for p in products}
    assert channels == {GRANDSTAGE, ONTHESPOT}

    # 두 채널 각각 1회씩 호출
    called_channels = [c["channel"] for c in fake_http.calls]
    assert GRANDSTAGE in called_channels
    assert ONTHESPOT in called_channels
    assert len(fake_http.calls) == 2

    # 이벤트 1건만 publish
    assert len(received) == 1
    assert received[0].product_count == 3


# ─── (b) 매칭 3케이스: 정상/미등재/콜라보가드 + 품절/no-model ─


async def test_match_three_cases(bus, kream_db):
    fake_http = _FakeAbcmartHttp(channel_items={})
    adapter = AbcmartAdapter(bus=bus, db_path=kream_db, http_client=fake_http)

    products = [
        # (1) 정상 매칭 — grandstage Nike AF1
        {
            "PRDT_NO": 5001,
            "PRDT_NAME": "나이키 에어포스 1 로우 화이트",
            "STYLE_INFO": "CW2288",
            "COLOR_ID": "111",
            "PRDT_DC_PRICE": 139000,
            "NRMAL_AMT": 159000,
            "BRAND_NAME": "Nike",
            "SOLD_OUT": "N",
            "_channel": GRANDSTAGE,
        },
        # (2) 미등재 신상 — onthespot, 크림 DB 에 없음
        {
            "PRDT_NO": 5002,
            "PRDT_NAME": "뉴발란스 2002R",
            "STYLE_INFO": "M2002RHO",
            "COLOR_ID": "",
            "PRDT_DC_PRICE": 159000,
            "NRMAL_AMT": 179000,
            "BRAND_NAME": "New Balance",
            "SOLD_OUT": "N",
            "_channel": ONTHESPOT,
        },
        # (3) 콜라보 가드 차단 — 크림=Travis Scott, 소싱=일반명
        {
            "PRDT_NO": 5003,
            "PRDT_NAME": "나이키 에어포스 1 로우 화이트",
            "STYLE_INFO": "AQ4211",
            "COLOR_ID": "100",
            "PRDT_DC_PRICE": 149000,
            "NRMAL_AMT": 159000,
            "BRAND_NAME": "Nike",
            "SOLD_OUT": "N",
            "_channel": GRANDSTAGE,
        },
        # (4) 품절
        {
            "PRDT_NO": 5004,
            "PRDT_NAME": "아디다스 삼바 OG",
            "STYLE_INFO": "B75806",
            "COLOR_ID": "",
            "PRDT_DC_PRICE": 129000,
            "NRMAL_AMT": 149000,
            "BRAND_NAME": "adidas",
            "SOLD_OUT": "Y",
            "_channel": ONTHESPOT,
        },
        # (5) 모델번호 없음
        {
            "PRDT_NO": 5005,
            "PRDT_NAME": "이름만 있고 STYLE 없음",
            "STYLE_INFO": "",
            "COLOR_ID": "",
            "PRDT_DC_PRICE": 10000,
            "NRMAL_AMT": 10000,
            "BRAND_NAME": "Nike",
            "SOLD_OUT": "N",
            "_channel": GRANDSTAGE,
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(products)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.dumped == 5
    assert stats.matched == 1
    assert stats.collected_to_queue == 1
    assert stats.skipped_guard == 1
    assert stats.soldout_dropped == 1
    assert stats.no_model_number == 1
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert isinstance(cand, CandidateMatched)
    assert cand.source == "abcmart"
    assert cand.kream_product_id == 101
    assert cand.model_no == "CW2288-111"
    assert cand.retail_price == 139000
    # 채널별 base_url 확인 — grandstage
    assert "grandstage.a-rt.com" in cand.url
    assert "5001" in cand.url

    # collect_queue 적재 확인
    assert _count_queue(kream_db) == 1


# ─── (c) URL 에 채널 구분 반영 ────────────────────────────


async def test_url_reflects_channel_base(bus, kream_db):
    """onthespot 상품은 www.onthespot.co.kr, grandstage 는 grandstage.a-rt.com."""
    fake_http = _FakeAbcmartHttp(
        channel_items={
            GRANDSTAGE: [_item(7001, "AF1", "CW2288", "111")],
            ONTHESPOT: [_item(7002, "Samba", "B75806", "")],
        }
    )
    adapter = AbcmartAdapter(bus=bus, db_path=kream_db, http_client=fake_http)
    _, products = await adapter.dump_catalog()
    matches, stats = await adapter.match_to_kream(products)

    assert stats.matched == 2
    urls_by_id = {m.kream_product_id: m.url for m in matches}
    assert "grandstage.a-rt.com" in urls_by_id[101]
    assert "www.onthespot.co.kr" in urls_by_id[303]


# ─── (d) run_once 통계 ────────────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake_http = _FakeAbcmartHttp(
        channel_items={
            GRANDSTAGE: [
                _item(1, "AF1", "CW2288", "111"),
                _item(2, "Sold", "ZZ9999", "000", sold_out="Y"),
            ],
            ONTHESPOT: [
                _item(3, "Samba", "B75806", ""),
            ],
        }
    )
    adapter = AbcmartAdapter(bus=bus, db_path=kream_db, http_client=fake_http)
    stats = await adapter.run_once()
    assert stats["dumped"] == 3
    assert stats["matched"] == 2
    assert stats["soldout_dropped"] == 1


# ─── (e) E2E: 오케스트레이터 완주 ─────────────────────────


async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakeAbcmartHttp(
        channel_items={
            GRANDSTAGE: [_item(9001, "나이키 에어포스 1", "CW2288", "111")],
            ONTHESPOT: [],
        }
    )
    adapter = AbcmartAdapter(bus=bus, db_path=kream_db, http_client=fake_http)

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
            kream_sell_price=200000,
            net_profit=40000,
            roi=0.28,
            signal="STRONG_BUY",
            volume_7d=10,
            url=event.url,
        )

    async def profit_handler(event: ProfitFound) -> AlertSent | None:
        sent = AlertSent(
            alert_id=len(alerts) + 1,
            kream_product_id=event.kream_product_id,
            signal=event.signal,
            fired_at=111.0,
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
        assert sent.kream_product_id == 101
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
