"""웍스아웃 푸시 어댑터 테스트 (Phase 3 배치 4).

실호출 금지: HTTP 클라이언트·크림 API 전부 mock.
웍스아웃은 모델번호가 없으므로 **이름 기반 엄격 매칭** 의 정확도가
핵심이다. 아래 케이스로 거짓 매칭 0 을 방어:

    - 브랜드 불일치 → drop
    - 토큰 1개만 교차 → drop
    - 콜라보 키워드 불일치 → drop
    - 서브타입(PRM/QS) 불일치 → drop
    - 정상 매칭 → pass
    - 품절 → drop
"""

from __future__ import annotations

import sqlite3

import pytest

from src.adapters.worksout_adapter import WorksoutAdapter, WorksoutMatchStats
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus

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
                    r.get("model_number", ""),
                    r.get("brand", ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ─── mock HTTP ────────────────────────────────────────────


class _FakeWorksoutHttp:
    """fetch_catalog_page(page, size) 만 mock.

    pages 는 (items, has_next) 튜플 시퀀스.
    """

    def __init__(self, pages: list[tuple[list[dict], bool]]):
        self._pages = list(pages)
        self.calls: list[tuple[int, int]] = []

    async def fetch_catalog_page(
        self, page: int, size: int = 60
    ) -> tuple[list[dict], bool]:
        self.calls.append((page, size))
        if not self._pages:
            return [], False
        return self._pages.pop(0)


def _mk_source(
    pid: str,
    name: str,
    brand: str = "Nike",
    price: int = 180000,
    sold_out: bool = False,
) -> dict:
    return {
        "product_id": pid,
        "name": name,
        "brand": brand,
        "price": price,
        "original_price": price,
        "url": f"https://www.worksout.co.kr/product/{pid}",
        "image_url": "",
        "is_sold_out": sold_out,
        "sizes": [
            {"size": "270", "price": price, "original_price": price, "in_stock": True},
        ],
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
            # 1) 일반 AF1 — 정상 매칭 타겟
            {
                "product_id": "1001",
                "name": "Nike Air Force 1 Low White",
                "model_number": "CW2288-111",
                "brand": "Nike",
            },
            # 2) Supreme 콜라보 — 콜라보 가드 테스트용
            {
                "product_id": "1002",
                "name": "Nike Air Force 1 Low Supreme White",
                "model_number": "CU9225-100",
                "brand": "Nike",
            },
            # 3) Dunk Low Panda — 서브타입 가드 테스트용 (일반)
            {
                "product_id": "1003",
                "name": "Nike Dunk Low Retro Black White",
                "model_number": "DD1391-100",
                "brand": "Nike",
            },
            # 4) Adidas Samba OG — 브랜드 불일치 타겟 후보
            {
                "product_id": "1004",
                "name": "Adidas Samba OG Cloud White",
                "model_number": "B75806",
                "brand": "Adidas",
            },
        ],
    )
    return str(path)


# ─── (a) dump_catalog 페이지네이션 + 이벤트 발행 ──────────


async def test_dump_catalog_publishes_event(bus, kream_db):
    fake = _FakeWorksoutHttp(
        pages=[
            (
                [
                    _mk_source("p1", "Nike Air Force 1 Low White"),
                    _mk_source("p2", "Nike Dunk Low Retro Panda"),
                ],
                True,
            ),
            (
                [_mk_source("p3", "Adidas Samba OG White", brand="Adidas")],
                False,
            ),
        ]
    )
    adapter = WorksoutAdapter(
        bus=bus,
        db_path=kream_db,
        crawler=fake,
        max_pages=5,
        page_size=60,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    event, products = await adapter.dump_catalog()
    while not queue.empty():
        received.append(queue.get_nowait())

    assert event.source == "worksout"
    assert event.product_count == 3
    assert len(products) == 3
    # 페이지 반복 진행 확인
    assert fake.calls == [(0, 60), (1, 60)]
    assert received and received[0].product_count == 3


# ─── (b) 정상 매칭 pass ───────────────────────────────────


async def test_match_normal_pass(bus, kream_db):
    adapter = WorksoutAdapter(bus=bus, db_path=kream_db, crawler=_FakeWorksoutHttp([]))
    products = [
        _mk_source("p1", "Nike Air Force 1 Low White", brand="Nike", price=160000),
    ]

    queue = bus.subscribe(CandidateMatched)
    matches, stats = await adapter.match_to_kream(products)
    received: list[CandidateMatched] = []
    while not queue.empty():
        received.append(queue.get_nowait())

    assert stats.matched == 1
    assert stats.skipped_guard == 0
    assert stats.soldout_dropped == 0
    assert len(matches) == 1
    assert len(received) == 1

    cand = matches[0]
    assert cand.source == "worksout"
    assert cand.kream_product_id == 1001
    assert cand.model_no == ""  # 이름 매칭 — 모델번호 비움
    assert cand.retail_price == 160000
    assert "worksout.co.kr/product/p1" in cand.url


# ─── (c) 브랜드 불일치 drop ────────────────────────────────


async def test_match_brand_mismatch_drops(bus, kream_db):
    adapter = WorksoutAdapter(bus=bus, db_path=kream_db, crawler=_FakeWorksoutHttp([]))
    # Adidas 브랜드 표기인데 이름은 Nike Air Force 1 (토큰 교차 되더라도
    # 브랜드 불일치 + 타 브랜드 버킷 이름 토큰으로는 교차 안 됨 → drop)
    products = [
        _mk_source("p1", "Nike Air Force 1 Low White", brand="Adidas"),
    ]
    matches, stats = await adapter.match_to_kream(products)

    # Adidas 브랜드 선언이므로 Nike AF1 크림 row (1001) 에 대해
    # _brand_match 실패. Adidas Samba (1004) 와는 토큰 교차 실패.
    assert stats.matched == 0
    assert stats.skipped_guard == 1
    assert len(matches) == 0


# ─── (d) 토큰 1개만 교차 drop ─────────────────────────────


async def test_match_single_token_overlap_drops(bus, kream_db):
    adapter = WorksoutAdapter(bus=bus, db_path=kream_db, crawler=_FakeWorksoutHttp([]))
    # "Nike" 는 stopword 로 제거. 남은 토큰은 "blazer", "mid" — 크림 DB
    # 에는 Blazer/Mid 상품 없음. AF1 와는 토큰 교차 0 → drop.
    products = [
        _mk_source("p1", "Nike Blazer Mid", brand="Nike"),
    ]
    matches, stats = await adapter.match_to_kream(products)

    assert stats.matched == 0
    assert stats.skipped_guard == 1
    assert len(matches) == 0


# ─── (e) 콜라보 키워드 불일치 drop ───────────────────────


async def test_match_collab_mismatch_drops(bus, kream_db):
    """크림=Supreme 콜라보 인데 소싱 이름은 일반 → 콜라보 가드 차단.

    1002 (콜라보) 와 소싱 Air Force 1 White 는 토큰 교차 (force, 1, low,
    white) 가 크지만, 크림 이름에 'supreme' 이 있고 소싱에는 없으므로
    collab_match_fails=True → drop.

    다만 1001 (일반 AF1) 과 매칭될 수 있으므로, 콜라보 가드 단독 테스트를
    위해 일반 AF1 을 DB 에서 제외한 고립 DB 를 쓴다.
    """
    # 콜라보-only DB
    tmp = kream_db + ".collab.db"
    _init_kream_db(
        tmp,
        rows=[
            {
                "product_id": "9001",
                "name": "Nike Air Force 1 Low Supreme White",
                "model_number": "CU9225-100",
                "brand": "Nike",
            },
        ],
    )
    adapter = WorksoutAdapter(bus=bus, db_path=tmp, crawler=_FakeWorksoutHttp([]))
    products = [
        _mk_source("p1", "Nike Air Force 1 Low White", brand="Nike"),
    ]
    matches, stats = await adapter.match_to_kream(products)

    assert stats.matched == 0
    assert stats.skipped_guard == 1
    assert len(matches) == 0


# ─── (f) 서브타입(PRM/QS) 불일치 drop ─────────────────────


async def test_match_subtype_mismatch_drops(bus, kream_db):
    """소싱 이름에만 'PRM' 이 있고 크림 이름은 일반이면 → 서브타입 가드 차단.

    크림 1003 = "Nike Dunk Low Retro Black White" (일반, retro 포함)
    소싱 = "Nike Dunk Low PRM Black White" → 토큰 교차 ≥ 2 (dunk, low,
    black, white) 이지만 소싱에만 'prm' 이 있으므로 drop.
    """
    adapter = WorksoutAdapter(bus=bus, db_path=kream_db, crawler=_FakeWorksoutHttp([]))
    products = [
        _mk_source("p1", "Nike Dunk Low PRM Black White", brand="Nike"),
    ]
    matches, stats = await adapter.match_to_kream(products)

    assert stats.matched == 0
    assert stats.skipped_guard == 1
    assert len(matches) == 0


# ─── (g) 품절 drop ────────────────────────────────────────


async def test_match_soldout_drops(bus, kream_db):
    adapter = WorksoutAdapter(bus=bus, db_path=kream_db, crawler=_FakeWorksoutHttp([]))
    products = [
        _mk_source("p1", "Nike Air Force 1 Low White", sold_out=True),
    ]
    matches, stats = await adapter.match_to_kream(products)

    assert stats.matched == 0
    assert stats.soldout_dropped == 1
    assert len(matches) == 0


# ─── (h) run_once 통계 통합 ───────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake = _FakeWorksoutHttp(
        pages=[
            (
                [
                    _mk_source("p1", "Nike Air Force 1 Low White"),           # match
                    _mk_source("p2", "Nike Dunk Low PRM Black White"),       # subtype drop
                    _mk_source("p3", "Nike Blazer Mid"),                      # no overlap
                    _mk_source("p4", "Nike Air Force 1 Low", sold_out=True), # soldout
                ],
                False,
            ),
        ]
    )
    adapter = WorksoutAdapter(
        bus=bus, db_path=kream_db, crawler=fake, max_pages=2, page_size=60
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 4
    assert stats["matched"] == 1
    assert stats["soldout_dropped"] == 1
    assert stats["skipped_guard"] == 2
    assert stats["collected_to_queue"] == 0  # 모델번호 없어 큐 적재 불가


# ─── (i) MatchStats as_dict 직렬화 ────────────────────────


def test_match_stats_as_dict_keys():
    stats = WorksoutMatchStats(
        dumped=10,
        soldout_dropped=2,
        matched=3,
        collected_to_queue=0,
        skipped_guard=5,
    )
    d = stats.as_dict()
    assert set(d.keys()) == {
        "dumped",
        "soldout_dropped",
        "matched",
        "collected_to_queue",
        "skipped_guard",
    }
    assert d["matched"] == 3
    assert d["collected_to_queue"] == 0
