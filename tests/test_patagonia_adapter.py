"""파타고니아 푸시 어댑터 테스트 (Phase 3 배치 6).

실호출 금지: HTTP 레이어·크림 API 전부 mock.
getGoodslist 응답 파싱은 순수 함수 `_parse_product` 로 별도 검증.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.adapters.patagonia_adapter import (
    PatagoniaAdapter,
    PatagoniaMatchStats,
)
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.crawlers.patagonia import (
    _parse_product,
    extract_style_code,
    is_soldout,
    parse_sizes_from_options,
    split_kream_model_numbers,
)

# ─── DB 헬퍼 ──────────────────────────────────────────────

_KREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS kream_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_number TEXT NOT NULL,
    brand TEXT DEFAULT '',
    category TEXT DEFAULT 'apparel',
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


class _FakePatagoniaHttp:
    """fetch_catalog(categories) 만 mock."""

    def __init__(self, catalog: list[dict]):
        self._catalog = catalog
        self.calls: list[tuple | None] = []

    async def fetch_catalog(self, categories):  # noqa: ANN001
        self.calls.append(categories)
        return list(self._catalog)


def _mk_item(
    pcode: str,
    name: str,
    price: int = 169000,
    sold_out: bool = False,
    product_id: str = "",
) -> dict:
    style = extract_style_code(pcode)
    pid = product_id or f"ID_{pcode}"
    return {
        "product_id": pid,
        "pcode": pcode,
        "style_code": style,
        "model_number": style,
        "name": name,
        "name_kr": "",
        "brand": "Patagonia",
        "price": price,
        "original_price": price,
        "url": f"https://www.patagonia.co.kr/shop/goodsView/{pid}",
        "image_url": "",
        "is_sold_out": sold_out,
        "is_specialty_only": False,
        "sizes": [
            {"size": "M", "stock": 5, "in_stock": True, "color": "BLK"},
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
            # 정상 매칭 — 단일 5자리
            {
                "product_id": "501",
                "name": "파타고니아 후디니 자켓 블랙",
                "model_number": "24142",
                "brand": "Patagonia",
            },
            # 복수 코드 — "85240/85241" → 두 코드 모두 매칭 가능
            {
                "product_id": "502",
                "name": "파타고니아 토렌쉘 3L 자켓 블랙",
                "model_number": "85240/85241",
                "brand": "Patagonia",
            },
            # 콜라보 가드 — 크림=슈프림 콜라보, 소싱=일반
            {
                "product_id": "503",
                "name": "파타고니아 x 슈프림 레트로X 플리스",
                "model_number": "23057",
                "brand": "Patagonia",
            },
        ],
    )
    return str(path)


# ─── (a) 순수 파싱 함수 ───────────────────────────────────


def test_extract_style_code():
    assert extract_style_code("44937R5") == "44937"
    assert extract_style_code("25580F4") == "25580"
    assert extract_style_code("44937") == "44937"
    assert extract_style_code("85241") == "85241"
    assert extract_style_code("") == ""
    # 비정형 fallback — 앞 5자리가 숫자면 통과
    assert extract_style_code("12345ABCD") == "12345"
    # 앞자리가 숫자 아니면 빈 문자열
    assert extract_style_code("ABCDE12345") == ""


def test_split_kream_model_numbers():
    assert split_kream_model_numbers("24142") == ["24142"]
    assert split_kream_model_numbers("85240/85241") == ["85240", "85241"]
    assert split_kream_model_numbers("25580/25551") == ["25580", "25551"]
    assert split_kream_model_numbers("") == []
    # 공백/콤마 구분도 허용
    assert split_kream_model_numbers("85240, 85241") == ["85240", "85241"]


def test_parse_sizes_from_options():
    block = {
        "options": [
            {"option_code": "CGBX|696946|S", "option_stock": 0},
            {"option_code": "CGBX|696946|M", "option_stock": 3},
            {"option_code": "CGBX|696946|L", "option_stock": 7},
        ]
    }
    sizes = parse_sizes_from_options(block)
    assert [s["size"] for s in sizes] == ["S", "M", "L"]
    assert [s["in_stock"] for s in sizes] == [False, True, True]
    assert sizes[1]["stock"] == 3


def test_is_soldout_variants():
    # status_soldout 플래그
    assert is_soldout({"status_soldout": True, "options": {"options": [
        {"option_code": "X|x|M", "option_stock": 10}
    ]}})
    # 모든 옵션 재고 0
    assert is_soldout({"status_soldout": False, "options": {"options": [
        {"option_code": "X|x|S", "option_stock": 0},
        {"option_code": "X|x|M", "option_stock": 0},
    ]}})
    # 일부 재고 있음
    assert not is_soldout({"status_soldout": False, "options": {"options": [
        {"option_code": "X|x|S", "option_stock": 0},
        {"option_code": "X|x|M", "option_stock": 2},
    ]}})
    # 사이즈 정보 없으면 보수적으로 품절
    assert is_soldout({"status_soldout": False, "options": {}})


def test_parse_product_happy_path():
    raw = {
        "id": "0000003243",
        "pcode": "44937R5",
        "pname": "Men's Capilene Cool",
        "pname_kr": "멘즈 캐필린",
        "sellprice": "169,000",
        "listprice": "189,000",
        "status_soldout": False,
        "image_src": "https://cdn/x.jpg",
        "options": {
            "options": [
                {"option_code": "CGBX|696946|M", "option_stock": 3},
            ]
        },
    }
    parsed = _parse_product(raw)
    assert parsed["product_id"] == "0000003243"
    assert parsed["pcode"] == "44937R5"
    assert parsed["style_code"] == "44937"
    assert parsed["model_number"] == "44937"
    assert parsed["price"] == 169000
    assert parsed["original_price"] == 189000
    assert parsed["is_sold_out"] is False
    assert parsed["url"].endswith("/0000003243")
    assert parsed["sizes"][0]["size"] == "M"


# ─── (b) dump_catalog — publish ──────────────────────────


async def test_dump_catalog_publishes_event(bus, kream_db):
    fake_http = _FakePatagoniaHttp(
        catalog=[
            _mk_item("24142R5", "Houdini Jacket"),
            _mk_item("85240R5", "Torrentshell 3L"),
        ]
    )
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )

    received: list[CatalogDumped] = []
    queue = bus.subscribe(CatalogDumped)

    async def consume():
        ev = await queue.get()
        received.append(ev)

    task = asyncio.create_task(consume())
    event, catalog = await adapter.dump_catalog()
    await asyncio.wait_for(task, timeout=1.0)

    assert event.source == "patagonia"
    assert event.product_count == 2
    assert len(catalog) == 2
    assert fake_http.calls == [None]  # categories=None 기본값 전달
    assert received and received[0].product_count == 2


# ─── (c) match_to_kream — 매칭/큐/가드 분류 ───────────────


async def test_match_to_kream_classifies(bus, kream_db):
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakePatagoniaHttp(catalog=[]),
    )

    catalog = [
        # (1) 정상 매칭 — 24142 → Houdini
        _mk_item("24142R5", "Houdini Jacket"),
        # (2) 복수 코드 매칭 — 85241 (크림에 "85240/85241") → 토렌쉘
        _mk_item("85241R5", "Torrentshell 3L Jacket"),
        # (3) 미등재 신상 → collect_queue
        _mk_item("99999R5", "Brand New Item"),
        # (4) 콜라보 가드 — 크림=슈프림 콜라보, 소싱=일반 레트로X → 차단
        _mk_item("23057R5", "Classic Retro-X Fleece"),
        # (5) 품절 → soldout_dropped
        _mk_item("24142R5", "Houdini Jacket", sold_out=True),
        # (6) 모델번호 없음
        {
            "product_id": "X",
            "pcode": "",
            "style_code": "",
            "model_number": "",
            "name": "Broken",
            "name_kr": "",
            "price": 0,
            "original_price": 0,
            "url": "",
            "image_url": "",
            "is_sold_out": False,
            "is_specialty_only": False,
            "sizes": [],
        },
    ]

    received: list[CandidateMatched] = []
    queue = bus.subscribe(CandidateMatched)

    matches, stats = await adapter.match_to_kream(catalog)

    while not queue.empty():
        received.append(queue.get_nowait())

    assert isinstance(stats, PatagoniaMatchStats)
    assert stats.dumped == 6
    assert stats.soldout_dropped == 1
    assert stats.no_model_number == 1
    assert stats.matched == 2  # 24142, 85241
    assert stats.skipped_guard == 1  # 23057 콜라보
    assert stats.collected_to_queue == 1  # 99999

    assert len(matches) == 2
    by_model = {m.model_no: m for m in matches}
    assert "24142" in by_model
    assert "85241" in by_model
    assert by_model["24142"].kream_product_id == 501
    assert by_model["85241"].kream_product_id == 502
    assert by_model["24142"].source == "patagonia"
    assert by_model["24142"].size == ""
    assert "24142" in by_model["24142"].model_no
    assert len(received) == 2

    # collect_queue: 99999 1건
    assert _count_queue(kream_db) == 1


# ─── (d) run_once 통계 정확성 ──────────────────────────────


async def test_run_once_stats(bus, kream_db):
    fake_http = _FakePatagoniaHttp(
        catalog=[
            _mk_item("24142R5", "Houdini Jacket"),
            _mk_item("99999R5", "New Unknown"),
        ]
    )
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
    )
    stats = await adapter.run_once()
    assert stats["dumped"] == 2
    assert stats["matched"] == 1
    assert stats["collected_to_queue"] == 1
    assert stats["soldout_dropped"] == 0


# ─── (e) 카테고리 override 전달 ────────────────────────────


async def test_categories_override(bus, kream_db):
    fake_http = _FakePatagoniaHttp(catalog=[])
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=fake_http,
        categories=("001001000000000", "001002000000000"),
    )
    await adapter.dump_catalog()
    assert fake_http.calls == [("001001000000000", "001002000000000")]


# ─── (f) HTTP 덤프 실패 시 빈 카탈로그 ─────────────────────


async def test_dump_catalog_handles_http_failure(bus, kream_db):
    class _Broken:
        async def fetch_catalog(self, categories):  # noqa: ANN001
            raise RuntimeError("boom")

    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_Broken(),
    )
    event, catalog = await adapter.dump_catalog()
    assert event.product_count == 0
    assert catalog == []


# ─── (g) 색상별 fan-out + tooltip 매칭 — 정확성 1순위 ─────


def _mk_color_item(
    pcode: str,
    name: str,
    color_variants: list[dict],
    sizes: list[dict],
    price: int = 169000,
) -> dict:
    """색상 메타 + 색상별 사이즈 그룹 fixture helper."""
    style = extract_style_code(pcode)
    pid = f"ID_{pcode}"
    return {
        "product_id": pid,
        "pcode": pcode,
        "style_code": style,
        "model_number": style,
        "name": name,
        "name_kr": "",
        "brand": "Patagonia",
        "price": price,
        "original_price": price,
        "url": f"https://www.patagonia.co.kr/shop/goodsView/{pid}",
        "image_url": "",
        "is_sold_out": False,
        "is_specialty_only": False,
        "color_variants": color_variants,
        "sizes": sizes,
    }


def _row(pid: str, color_kr: str) -> dict:
    return {
        "product_id": pid,
        "name": f"파타고니아 다운 스웨터 후디 {color_kr}",
        "model_number": "84702",
        "brand": "Patagonia",
    }


@pytest.fixture
def kream_db_84702(tmp_path):
    """84702 14색상 등록 — 실제 크림 DB 데이터 mirror."""
    path = tmp_path / "kream84702.db"
    _init_kream_db(
        str(path),
        rows=[
            _row("193126", "블랙"),
            _row("591409", "포지 그레이"),
            _row("742892", "플럼멧 퍼플"),
            _row("742886", "케스케이드 그린"),
            _row("697212", "아마니타 레드"),
            _row("439666", "씨버드 그레이"),
            _row("432807", "뉴 네이비"),
            _row("422515", "레드테일 러스트"),
            _row("231657", "베이슨 그린"),
            _row("744686", "피논 그린"),
            _row("742890", "클레멘트 블루"),
            _row("432814", "파인 니들 그린"),
            _row("407009", "패시지 블루"),
            _row("268828", "포지 그레이"),  # 시즌 중복 — Forge Grey 두 row
        ],
    )
    return str(path)


async def test_color_fanout_blk_soldout_only_feg_alerted(bus, kream_db_84702):
    """84702 — Forge Grey XXL 재고만 있고 Black 전 사이즈 품절 → 포지 그레이 알림만, 블랙 차단."""
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db_84702,
        http_client=_FakePatagoniaHttp(catalog=[]),
    )
    item = _mk_color_item(
        pcode="84702Q7",
        name="Men's Down Sweater Hoody",
        price=399200,
        color_variants=[
            {"code": "FEG", "tooltip": "Forge Grey w/Forge Grey"},
            {"code": "BLK", "tooltip": "Black"},
        ],
        sizes=[
            # FEG: XXL 만 재고
            {"size": "XS", "stock": 0, "in_stock": False, "color": "FEG"},
            {"size": "S",  "stock": 0, "in_stock": False, "color": "FEG"},
            {"size": "M",  "stock": 0, "in_stock": False, "color": "FEG"},
            {"size": "L",  "stock": 0, "in_stock": False, "color": "FEG"},
            {"size": "XL", "stock": 0, "in_stock": False, "color": "FEG"},
            {"size": "XXL","stock": 2, "in_stock": True,  "color": "FEG"},
            # BLK: 전 사이즈 품절
            {"size": "XS", "stock": 0, "in_stock": False, "color": "BLK"},
            {"size": "S",  "stock": 0, "in_stock": False, "color": "BLK"},
            {"size": "M",  "stock": 0, "in_stock": False, "color": "BLK"},
            {"size": "L",  "stock": 0, "in_stock": False, "color": "BLK"},
            {"size": "XL", "stock": 0, "in_stock": False, "color": "BLK"},
            {"size": "XXL","stock": 0, "in_stock": False, "color": "BLK"},
        ],
    )
    matches, stats = await adapter.match_to_kream([item])
    # 포지 그레이 = 시즌 중복 2개 등록 → 모두 발행 (≤AMBIGUOUS_THRESHOLD)
    assert stats.matched == 2
    assert stats.matched_by_color == 2
    assert stats.ambiguous_color_unresolved == 0
    assert stats.unknown_color_code == 0
    # 모두 포지 그레이 + XXL — 블랙(193126) 절대 발행 안 됨
    pids = {m.kream_product_id for m in matches}
    assert 193126 not in pids, "블랙(193126) 잘못 발행 — 색상 매칭 버그"
    assert pids == {591409, 268828}
    for m in matches:
        assert m.color_name == "포지 그레이"
        assert m.available_sizes == ("XXL",)


async def test_color_fanout_both_colors_in_stock(bus, kream_db_84702):
    """84702 — Forge Grey + Cascade Green 둘 다 재고 → 색상별 별개 알림."""
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db_84702,
        http_client=_FakePatagoniaHttp(catalog=[]),
    )
    item = _mk_color_item(
        pcode="84702Q7",
        name="Men's Down Sweater Hoody",
        color_variants=[
            {"code": "FEG", "tooltip": "Forge Grey w/Forge Grey"},
            {"code": "CASG", "tooltip": "Cascade Green"},
        ],
        sizes=[
            {"size": "M", "stock": 1, "in_stock": True, "color": "FEG"},
            {"size": "L", "stock": 2, "in_stock": True, "color": "CASG"},
        ],
    )
    matches, _ = await adapter.match_to_kream([item])
    by_color = {m.color_name: m for m in matches}
    # 포지 그레이는 시즌 중복 2개 모두 발행
    assert "포지 그레이" in by_color
    assert "케스케이드 그린" in by_color
    # 케스케이드 그린은 단일 후보 (742886) → exact 매칭
    cas = by_color["케스케이드 그린"]
    assert cas.kream_product_id == 742886
    assert cas.available_sizes == ("L",)


async def test_color_unknown_tooltip_skipped(bus, kream_db_84702):
    """사이트 tooltip 이 사전에 없는 색상 → unknown_color_code +1, 발행 안 됨."""
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db_84702,
        http_client=_FakePatagoniaHttp(catalog=[]),
    )
    item = _mk_color_item(
        pcode="84702Q7",
        name="Men's Down Sweater Hoody",
        color_variants=[
            {"code": "MJVK", "tooltip": "Mojave Khaki"},  # 사전 미등록
        ],
        sizes=[
            {"size": "M", "stock": 1, "in_stock": True, "color": "MJVK"},
        ],
    )
    matches, stats = await adapter.match_to_kream([item])
    assert matches == []
    assert stats.matched == 0
    assert stats.unknown_color_code == 1


async def test_color_no_hits_when_tooltip_kr_absent_in_kream(bus, tmp_path):
    """사이트 색상 사전엔 있으나 크림 row 의 한글명에 해당 토큰 없음 → unknown."""
    path = tmp_path / "amb.db"
    _init_kream_db(
        str(path),
        rows=[
            {"product_id": "1001", "name": "파타고니아 자켓 케스케이드 그린",
             "model_number": "11111", "brand": "Patagonia"},
            {"product_id": "1002", "name": "파타고니아 자켓 베이슨 그린",
             "model_number": "11111", "brand": "Patagonia"},
            {"product_id": "1003", "name": "파타고니아 자켓 피논 그린",
             "model_number": "11111", "brand": "Patagonia"},
            {"product_id": "1004", "name": "파타고니아 자켓 파인 니들 그린",
             "model_number": "11111", "brand": "Patagonia"},
        ],
    )
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=str(path),
        http_client=_FakePatagoniaHttp(catalog=[]),
    )
    # tooltip "Forge Grey" → ("포지 그레이",) — kream 4개 모두 그린 계열 → 0 hit → unknown
    item = _mk_color_item(
        pcode="11111R5",
        name="Test Multi-Green",
        color_variants=[
            {"code": "FEG", "tooltip": "Forge Grey w/Forge Grey"},
        ],
        sizes=[
            {"size": "M", "stock": 1, "in_stock": True, "color": "FEG"},
        ],
    )
    matches, stats = await adapter.match_to_kream([item])
    assert matches == []
    assert stats.matched == 0
    assert stats.unknown_color_code == 1


async def test_color_single_candidate_extracts_suffix(bus, kream_db):
    """후보 1개 (24142 Houdini 블랙) → color_name 자동 추출 OK."""
    adapter = PatagoniaAdapter(
        bus=bus,
        db_path=kream_db,
        http_client=_FakePatagoniaHttp(catalog=[]),
    )
    item = _mk_color_item(
        pcode="24142R5",
        name="Houdini Jacket",
        color_variants=[{"code": "BLK", "tooltip": "Black"}],
        sizes=[{"size": "M", "stock": 1, "in_stock": True, "color": "BLK"}],
    )
    matches, _ = await adapter.match_to_kream([item])
    assert len(matches) == 1
    assert matches[0].kream_product_id == 501
    assert matches[0].color_name == "블랙"  # _extract_color_suffix 로 추출


# ─── (h) tooltip 정규화 단위 테스트 ─────────────────────────


def test_normalize_color_tooltip():
    from src.adapters.patagonia_adapter import _normalize_color_tooltip
    assert _normalize_color_tooltip("Black") == "BLACK"
    assert _normalize_color_tooltip("Forge Grey w/Forge Grey") == "FORGE GREY"
    # 콜라보 패턴 — 콜론 뒤만
    assert _normalize_color_tooltip("Bee You: New Navy") == "NEW NAVY"
    # HTML escape 일괄 복원 (&apos;/&amp;/&quot; 등)
    assert _normalize_color_tooltip("&apos;73 Skyline Black") == "'73 SKYLINE BLACK"
    assert _normalize_color_tooltip("Salt &amp; Pepper") == "SALT & PEPPER"
    assert _normalize_color_tooltip("&quot;Test&quot;") == '"TEST"'
    assert _normalize_color_tooltip("") == ""


def test_extract_color_suffix():
    from src.adapters.patagonia_adapter import _extract_color_suffix
    assert _extract_color_suffix("파타고니아 다운 스웨터 후디 블랙") == "블랙"
    assert _extract_color_suffix("파타고니아 다운 스웨터 후디 포지 그레이") == "포지 그레이"
    # 사전에 없는 색상은 빈 문자열
    assert _extract_color_suffix("파타고니아 R5 자켓 라일락") == ""
    assert _extract_color_suffix("") == ""
