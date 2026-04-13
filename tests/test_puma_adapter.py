"""푸마 푸시 어댑터 테스트 (Phase 3 배치 6).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from src.adapters.puma_adapter import (
    PUMA_STYLE_RE,
    PumaAdapter,
    _to_puma_model,
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
                (
                    r["product_id"],
                    r["name"],
                    r["model_number"],
                    r.get("brand", "Puma"),
                ),
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


class _FakePumaHttp:
    """fetch_category_grid(cgid, start, sz) 만 mock.

    pages[cgid] = [list_for_page0, list_for_page1, ...]
    """

    def __init__(self, pages: dict[str, list[list[dict]]]):
        self._pages = pages
        self.calls: list[tuple[str, int, int]] = []

    async def fetch_category_grid(
        self, cgid: str, *, start: int = 0, sz: int = 48
    ) -> list[dict]:
        self.calls.append((cgid, start, sz))
        bucket = self._pages.get(cgid) or []
        page_idx = start // sz if sz else 0
        if page_idx < 0 or page_idx >= len(bucket):
            return []
        return list(bucket[page_idx])


def _mk_item(
    model_no: str,
    name: str = "Puma Suede Classic",
    price: int = 139000,
    available: bool = True,
) -> dict:
    return {
        "product_id": model_no,
        "name": name,
        "brand": "Puma",
        "model_number": model_no,
        "price": price,
        "original_price": price,
        "url": f"https://kr.puma.com/q?{model_no}",
        "image_url": "",
        "color": "",
        "is_sold_out": not available,
        "available": available,
        "sizes": [],
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
            # 존재 — Puma 스피드캣 OG
            {
                "product_id": "303569",
                "name": "푸마 스피드캣 OG 블랙 화이트",
                "model_number": "398846-01",
                "brand": "Puma",
            },
            # 존재 — 슈프림 콜라보 (가드 테스트용 — COLLAB_KEYWORDS 포함)
            {
                "product_id": "556041",
                "name": "푸마 x 슈프림 스피드캣 PRM 블랙",
                "model_number": "404391-01",
                "brand": "Puma",
            },
            # 존재 — 일반 스웨이드
            {
                "product_id": "400000",
                "name": "푸마 스웨이드 클래식",
                "model_number": "403767-03",
                "brand": "Puma",
            },
        ],
    )
    return str(path)


# ─── (a) 정규식/포맷 변환 순수 함수 ──────────────────────


def test_puma_style_regex():
    assert PUMA_STYLE_RE.match("403767-03")
    assert PUMA_STYLE_RE.match("398846-01")
    # underscore 포맷은 어댑터 normalize 전에는 reject
    assert not PUMA_STYLE_RE.match("403767_03")
    assert not PUMA_STYLE_RE.match("40376-03")  # 5자리
    assert not PUMA_STYLE_RE.match("403767-3")  # 1자리
    assert not PUMA_STYLE_RE.match("")


def test_to_puma_model_normalization():
    # 소싱 포맷(`_`) → 크림 포맷(`-`)
    assert _to_puma_model("403767_03") == "403767-03"
    assert _to_puma_model("403767-03") == "403767-03"
    assert _to_puma_model(" 403767_03 ") == "403767-03"
    # 유효하지 않으면 빈 문자열
    assert _to_puma_model("XX1234-01") == ""
    assert _to_puma_model("12345-01") == ""
    assert _to_puma_model("") == ""


# ─── (b) dump_catalog publish + 카테고리/페이지네이션 ───


async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakePumaHttp(
        pages={
            "mens-shoes": [
                [_mk_item("398846-01"), _mk_item("403767-03")],
                [],  # 두 번째 페이지 비어 → 조기 종료
            ],
            "womens-shoes": [
                [_mk_item("404391-01")],
            ],
            # 나머지 카테고리는 전부 빈 응답 → 첫 페이지에서 종료
            "kids-shoes": [],
            "mens-apparel": [],
            "womens-apparel": [],
            "mens-accessories": [],
            "womens-accessories": [],
        }
    )
    adapter = PumaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        max_pages_per_category=5,
        page_size=48,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, products = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    # 3개 unique (mens-shoes 2 + womens-shoes 1)
    assert event.source == "puma"
    assert event.product_count == 3
    assert len(products) == 3
    # mens-shoes 는 page0(2건) 반환 < page_size → page1 호출 없이 조기 종료
    cgids_called = [c[0] for c in fake_http.calls]
    assert "mens-shoes" in cgids_called
    assert "womens-shoes" in cgids_called
    assert received and received[0].product_count == 3


async def test_dump_dedup_across_categories(bus, kream_db):
    """같은 상품이 여러 카테고리에 노출되어도 1회만 담긴다."""
    fake_http = _FakePumaHttp(
        pages={
            "mens-shoes": [[_mk_item("398846-01"), _mk_item("403767-03")]],
            "womens-shoes": [[_mk_item("398846-01")]],  # 중복
            "kids-shoes": [],
            "mens-apparel": [],
            "womens-apparel": [],
            "mens-accessories": [],
            "womens-accessories": [],
        }
    )
    adapter = PumaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        max_pages_per_category=2,
    )
    _, products = await adapter.dump_catalog()
    assert len(products) == 2  # 398846-01 은 1회만


# ─── (c) match_to_kream — 분류 통계 ───────────────────────


async def test_match_to_kream_classifies(bus, kream_db):
    adapter = PumaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakePumaHttp(pages={}),
    )

    products = [
        # (1) 정상 매칭 — 스피드캣 OG
        _mk_item("398846-01", "푸마 스피드캣 OG 블랙 화이트", 139000, True),
        # (2) 중복 — 같은 모델번호 dedup
        _mk_item("398846-01", "푸마 스피드캣 OG 블랙 화이트", 139000, True),
        # (3) 미등재 신상 → collect_queue
        _mk_item("409186-01", "푸마 스피드캣 고 메탈릭", 159000, True),
        # (4) 콜라보 가드 차단 — 크림은 "x 로제" 콜라보, 소싱명은 일반
        _mk_item("404391-01", "푸마 스피드캣 PRM", 199000, True),
        # (5) 품절 제외
        _mk_item("403767-03", "푸마 스웨이드 클래식", 159000, False),
        # (6) invalid style — 숫자 개수 불일치
        {
            "product_id": "XX",
            "name": "Junk",
            "brand": "Puma",
            "model_number": "12345-01",
            "price": 10000,
            "available": True,
            "is_sold_out": False,
        },
        # (7) underscore 포맷 — 어댑터가 하이픈으로 정규화해 매칭 성공해야 함
        # 단 (1) 과 중복이므로 dedup 됨 → 매칭 카운트는 증가 안 함
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(products)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.dumped == 6
    assert stats.soldout_dropped == 1
    assert stats.matched == 1  # 398846-01 1회 (dedup)
    assert stats.collected_to_queue == 1  # 409186-01
    assert stats.skipped_guard == 1  # 404391-01 콜라보 가드
    assert stats.invalid_style == 1  # 12345-01
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert cand.source == "puma"
    assert cand.kream_product_id == 303569
    assert cand.model_no == "398846-01"
    assert cand.retail_price == 139000
    assert cand.size == ""  # 그리드엔 사이즈 정보 없음

    # collect_queue 에 409186-01 쌓여야 함
    assert _count_queue(kream_db) == 1


async def test_underscore_format_matches(bus, kream_db):
    """소싱 응답에 `_` 포맷이 남아도 어댑터 normalize 로 매칭되어야 한다."""
    adapter = PumaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakePumaHttp(pages={}),
    )
    raw_item = {
        "product_id": "403767_03",
        "name": "푸마 스웨이드 찰스",
        "brand": "Puma",
        "model_number": "403767_03",  # underscore
        "price": 159000,
        "available": True,
        "is_sold_out": False,
    }
    matches, stats = await adapter.match_to_kream([raw_item])
    assert stats.matched == 1
    assert matches[0].model_no == "403767-03"
    assert matches[0].kream_product_id == 400000


# ─── (d) run_once 통계 정확성 ─────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake_http = _FakePumaHttp(
        pages={
            "mens-shoes": [
                [
                    _mk_item("398846-01"),
                    _mk_item("999999-99"),  # 신상 (미등재)
                ]
            ],
            "womens-shoes": [],
            "kids-shoes": [],
            "mens-apparel": [],
            "womens-apparel": [],
            "mens-accessories": [],
            "womens-accessories": [],
        }
    )
    adapter = PumaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        max_pages_per_category=2,
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["collected_to_queue"] == 1
    assert stats["soldout_dropped"] == 0


# ─── (e) E2E: 어댑터 → 오케스트레이터 → AlertSent ─────────


async def test_end_to_end_through_orchestrator(bus, kream_db, tmp_path):
    """어댑터가 publish 한 이벤트가 orchestrator 를 통해 AlertSent 까지 완주."""
    fake_http = _FakePumaHttp(
        pages={
            "mens-shoes": [[_mk_item("398846-01", price=139000)]],
            "womens-shoes": [],
            "kids-shoes": [],
            "mens-apparel": [],
            "womens-apparel": [],
            "mens-accessories": [],
            "womens-accessories": [],
        }
    )
    adapter = PumaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        max_pages_per_category=1,
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
            kream_sell_price=210000,
            net_profit=55000,
            roi=0.40,
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
        assert sent.kream_product_id == 303569
        assert sent.signal == "STRONG_BUY"

        stats = orch.stats()
        assert stats["candidate_processed"] >= 1
        assert stats["profit_processed"] >= 1
    finally:
        await orch.stop()
        await store.close()
