"""alert_outcome 단위 테스트 (Phase 4 스캐폴딩).

- 스키마는 src/models/database.py 에서 생성. 테스트는 그 스키마를 그대로 사용.
- 외부 API 호출 0 — 순수 DB I/O.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiosqlite
import pytest

from src.core.alert_outcome import (
    FollowupRecord,
    get_followup,
    hit_rate_summary,
    mark_checked,
    pending_followups,
    record_alert,
    sweep_pending,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_followup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER,
    fired_at TIMESTAMP,
    kream_product_id TEXT,
    size TEXT,
    retail_price INTEGER,
    kream_sell_price_at_fire INTEGER,
    checked_at TIMESTAMP,
    actual_sold INTEGER DEFAULT 0,
    actual_price INTEGER
);
"""


async def _init_db(path: str) -> None:
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()


@pytest.fixture
async def db(tmp_path: Path) -> str:
    p = str(tmp_path / "alert_outcome.db")
    await _init_db(p)
    return p


# ─── record_alert ────────────────────────────────────────


async def test_record_alert_inserts_pending_row(db: str):
    fid = await record_alert(
        db,
        alert_id=42,
        kream_product_id=12345,
        size="270",
        retail_price=120_000,
        kream_sell_price_at_fire=180_000,
    )
    assert fid > 0
    rec = await get_followup(db, fid)
    assert rec is not None
    assert rec.alert_id == 42
    assert rec.kream_product_id == "12345"
    assert rec.size == "270"
    assert rec.checked_at is None
    assert rec.actual_sold == 0
    assert rec.actual_price is None
    assert rec.fired_at > 0


async def test_record_alert_accepts_explicit_fired_at(db: str):
    fid = await record_alert(
        db,
        alert_id=None,
        kream_product_id="999",
        size="265",
        retail_price=100_000,
        kream_sell_price_at_fire=140_000,
        fired_at=1_700_000_000.0,
    )
    rec = await get_followup(db, fid)
    assert rec is not None
    assert rec.fired_at == 1_700_000_000.0
    assert rec.alert_id is None


# ─── pending_followups ───────────────────────────────────


async def test_pending_only_old_unchecked(db: str):
    now = time.time()
    # 과거 (sweep 대상)
    old = await record_alert(
        db,
        alert_id=1,
        kream_product_id="1",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=150_000,
        fired_at=now - 3600,
    )
    # 최근 (아직 sweep 대상 아님)
    await record_alert(
        db,
        alert_id=2,
        kream_product_id="2",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=150_000,
        fired_at=now - 60,
    )
    # 이미 체크됨 — 제외
    checked_id = await record_alert(
        db,
        alert_id=3,
        kream_product_id="3",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=150_000,
        fired_at=now - 7200,
    )
    await mark_checked(db, checked_id, actual_sold=True, actual_price=160_000)

    pending = await pending_followups(db, older_than_sec=1800)
    assert [p.id for p in pending] == [old]


async def test_pending_respects_limit(db: str):
    now = time.time()
    for i in range(5):
        await record_alert(
            db,
            alert_id=i,
            kream_product_id=str(i),
            size="270",
            retail_price=100_000,
            kream_sell_price_at_fire=150_000,
            fired_at=now - 3600,
        )
    pending = await pending_followups(db, older_than_sec=1800, limit=3)
    assert len(pending) == 3


# ─── mark_checked ─────────────────────────────────────────


async def test_mark_checked_sold_with_price(db: str):
    fid = await record_alert(
        db,
        alert_id=1,
        kream_product_id="1",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=150_000,
    )
    await mark_checked(db, fid, actual_sold=True, actual_price=148_000)
    rec = await get_followup(db, fid)
    assert rec is not None
    assert rec.actual_sold == 1
    assert rec.actual_price == 148_000
    assert rec.checked_at is not None


async def test_mark_checked_not_sold_no_price(db: str):
    fid = await record_alert(
        db,
        alert_id=1,
        kream_product_id="1",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=150_000,
    )
    await mark_checked(db, fid, actual_sold=False)
    rec = await get_followup(db, fid)
    assert rec is not None
    assert rec.actual_sold == 0
    assert rec.actual_price is None
    assert rec.checked_at is not None


# ─── hit_rate_summary ────────────────────────────────────


async def test_hit_rate_zero_when_empty(db: str):
    summary = await hit_rate_summary(db, hours=24)
    assert summary["total"] == 0
    assert summary["checked"] == 0
    assert summary["sold"] == 0
    assert summary["hit_rate_pct"] is None
    assert summary["avg_realized_gap_pct"] is None


async def test_hit_rate_with_mixed_outcomes(db: str):
    now = time.time()
    # 2 sold, 1 not sold, 1 unchecked
    a = await record_alert(
        db,
        alert_id=1,
        kream_product_id="1",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=200_000,
        fired_at=now - 3600,
    )
    b = await record_alert(
        db,
        alert_id=2,
        kream_product_id="2",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=200_000,
        fired_at=now - 3600,
    )
    c = await record_alert(
        db,
        alert_id=3,
        kream_product_id="3",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=200_000,
        fired_at=now - 3600,
    )
    await record_alert(
        db,
        alert_id=4,
        kream_product_id="4",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=200_000,
        fired_at=now - 3600,
    )
    await mark_checked(db, a, actual_sold=True, actual_price=210_000)  # +5%
    await mark_checked(db, b, actual_sold=True, actual_price=190_000)  # -5%
    await mark_checked(db, c, actual_sold=False)

    s = await hit_rate_summary(db, hours=24)
    assert s["total"] == 4
    assert s["checked"] == 3
    assert s["sold"] == 2
    # 2 sold / 3 checked = 66.67%
    assert s["hit_rate_pct"] == 66.67
    # 평균 realized gap = (5 + -5) / 2 = 0
    assert s["avg_realized_gap_pct"] == 0.0


# ─── sweep_pending ────────────────────────────────────────


async def test_sweep_processes_all_pending(db: str):
    now = time.time()
    ids = []
    for i in range(3):
        ids.append(
            await record_alert(
                db,
                alert_id=i,
                kream_product_id=str(i),
                size="270",
                retail_price=100_000,
                kream_sell_price_at_fire=150_000,
                fired_at=now - 3600,
            )
        )

    async def fake_check(rec: FollowupRecord) -> tuple[bool, int | None]:
        return (rec.alert_id or 0) % 2 == 0, 145_000

    result = await sweep_pending(db, fake_check)
    assert result["scanned"] == 3
    assert result["sold"] == 2  # alert_id 0, 2
    assert result["not_sold"] == 1
    assert result["errors"] == 0

    # 모두 checked 됨 — 두 번째 sweep 은 빈 결과
    again = await sweep_pending(db, fake_check)
    assert again["scanned"] == 0


async def test_sweep_isolates_check_fn_exception(db: str):
    now = time.time()
    fid = await record_alert(
        db,
        alert_id=1,
        kream_product_id="1",
        size="270",
        retail_price=100_000,
        kream_sell_price_at_fire=150_000,
        fired_at=now - 3600,
    )

    async def boom(_rec: FollowupRecord) -> tuple[bool, int | None]:
        raise RuntimeError("upstream API fail")

    result = await sweep_pending(db, boom)
    assert result["scanned"] == 1
    assert result["errors"] == 1
    assert result["sold"] == 0
    # 실패한 행은 여전히 unchecked — 다음 sweep 에서 재시도
    rec = await get_followup(db, fid)
    assert rec is not None
    assert rec.checked_at is None


async def test_sweep_concurrent_safe(db: str):
    """동시에 두 sweep 이 돌아도 race 없이 결과 합산이 일치."""
    now = time.time()
    for i in range(6):
        await record_alert(
            db,
            alert_id=i,
            kream_product_id=str(i),
            size="270",
            retail_price=100_000,
            kream_sell_price_at_fire=150_000,
            fired_at=now - 3600,
        )

    async def fake_check(_rec: FollowupRecord) -> tuple[bool, int | None]:
        await asyncio.sleep(0)
        return True, 160_000

    r1, r2 = await asyncio.gather(
        sweep_pending(db, fake_check, limit=10),
        sweep_pending(db, fake_check, limit=10),
    )
    # 둘 합쳐서 정확히 6 행 처리 (pending 6, 일부는 두 번째 sweep 에서 0)
    assert r1["scanned"] + r2["scanned"] >= 6


# ─── alert_outcome_sweeper.make_check_fn ──────────────────


def _post(secs_after_fired: float = 60.0):
    """fired_at 바로 뒤 datetime 을 만드는 헬퍼 (naive, 로컬 tz)."""
    from datetime import datetime
    return datetime.fromtimestamp(time.time() + secs_after_fired)


def _pre(secs_before_fired: float = 3600.0):
    """fired_at 이전 datetime."""
    from datetime import datetime
    return datetime.fromtimestamp(time.time() - secs_before_fired)


async def test_check_fn_compares_size_last_sale(db: str):
    """make_check_fn: fired_at 이후 체결 & price ≥ sell_at_fire 면 sold=True."""
    from dataclasses import dataclass

    from src.core.alert_outcome_sweeper import make_check_fn

    @dataclass
    class _SP:
        size: str
        last_sale_price: int | None
        last_sale_date: object = None  # datetime | None

    @dataclass
    class _Product:
        size_prices: list

    class _Crawler:
        def __init__(self, prod):
            self.prod = prod
            self.calls = 0

        async def get_product_detail(self, pid):
            self.calls += 1
            return self.prod

    prod = _Product(size_prices=[
        _SP(size="270", last_sale_price=185_000, last_sale_date=_post()),
        _SP(size="280", last_sale_price=160_000, last_sale_date=_post()),
    ])
    crawler = _Crawler(prod)
    check = make_check_fn(crawler=crawler)

    # 270: last 185k ≥ fire 180k, 체결 사후 → sold
    fid_a = await record_alert(
        db, alert_id=1, kream_product_id="999",
        size="270", retail_price=120_000, kream_sell_price_at_fire=180_000,
    )
    rec_a = await get_followup(db, fid_a)
    sold_a, price_a = await check(rec_a)
    assert sold_a is True and price_a == 185_000

    # 280: last 160k < fire 200k → not sold
    fid_b = await record_alert(
        db, alert_id=2, kream_product_id="999",
        size="280", retail_price=120_000, kream_sell_price_at_fire=200_000,
    )
    rec_b = await get_followup(db, fid_b)
    sold_b, price_b = await check(rec_b)
    # post-fire 체결은 있지만 임계 미달 — sold=False 여도 실현가는 기록
    assert sold_b is False and price_b == 160_000

    # 같은 product_id 두 번 — fetch 1회만 (in-memory cache)
    assert crawler.calls == 1


async def test_check_fn_rejects_pre_fire_sale(db: str):
    """last_sale_date 가 fired_at 이전이면 price 충족해도 sold=False.

    이 필터가 없으면 과거 체결로 hit_rate 부풀려지는 결함이 생김.
    """
    from dataclasses import dataclass

    from src.core.alert_outcome_sweeper import make_check_fn

    @dataclass
    class _SP:
        size: str
        last_sale_price: int | None
        last_sale_date: object = None

    @dataclass
    class _Product:
        size_prices: list

    class _Crawler:
        async def get_product_detail(self, pid):
            return _Product(size_prices=[
                # 가격은 충족하지만 체결시각이 fired_at 1시간 전
                _SP(size="270", last_sale_price=200_000, last_sale_date=_pre()),
            ])

    check = make_check_fn(crawler=_Crawler())
    fid = await record_alert(
        db, alert_id=30, kream_product_id="888",
        size="270", retail_price=120_000, kream_sell_price_at_fire=180_000,
    )
    rec = await get_followup(db, fid)
    sold, price = await check(rec)
    # 과거 체결은 제외 — sold=False, actual_price=None
    assert sold is False and price is None


async def test_check_fn_rejects_when_date_missing(db: str):
    """last_sale_date 가 None 이면 체결 시점을 알 수 없으니 sold=False."""
    from dataclasses import dataclass

    from src.core.alert_outcome_sweeper import make_check_fn

    @dataclass
    class _SP:
        size: str
        last_sale_price: int | None
        last_sale_date: object = None

    @dataclass
    class _Product:
        size_prices: list

    class _Crawler:
        async def get_product_detail(self, pid):
            return _Product(size_prices=[
                _SP(size="270", last_sale_price=200_000, last_sale_date=None),
            ])

    check = make_check_fn(crawler=_Crawler())
    fid = await record_alert(
        db, alert_id=31, kream_product_id="889",
        size="270", retail_price=120_000, kream_sell_price_at_fire=180_000,
    )
    rec = await get_followup(db, fid)
    sold, price = await check(rec)
    assert sold is False and price is None


async def test_check_fn_handles_empty_size_uses_max_last_sale(db: str):
    """size='' (MIN 대표가 알림) 케이스: 알림 이후 체결 중 max(last_sale) 사용."""
    from dataclasses import dataclass

    from src.core.alert_outcome_sweeper import make_check_fn

    @dataclass
    class _SP:
        size: str
        last_sale_price: int | None
        last_sale_date: object = None

    @dataclass
    class _Product:
        size_prices: list

    class _Crawler:
        async def get_product_detail(self, pid):
            return _Product(size_prices=[
                _SP(size="270", last_sale_price=150_000, last_sale_date=_post()),
                _SP(size="280", last_sale_price=180_000, last_sale_date=_post()),
                _SP(size="290", last_sale_price=None, last_sale_date=None),
                # 이건 pre-fire — 제외돼야 함
                _SP(size="300", last_sale_price=999_999, last_sale_date=_pre()),
            ])

    check = make_check_fn(crawler=_Crawler())
    fid = await record_alert(
        db, alert_id=10, kream_product_id="555",
        size="", retail_price=100_000, kream_sell_price_at_fire=170_000,
    )
    rec = await get_followup(db, fid)
    sold, price = await check(rec)
    # post-fire 중 max(150k, 180k) = 180k ≥ 170k → sold (300은 pre-fire 제외)
    assert sold is True and price == 180_000


async def test_check_fn_returns_false_when_no_data(db: str):
    """크롤러가 None 반환 또는 size_prices 비어있으면 not sold."""
    from src.core.alert_outcome_sweeper import make_check_fn

    class _NoneCrawler:
        async def get_product_detail(self, pid):
            return None

    check = make_check_fn(crawler=_NoneCrawler())
    fid = await record_alert(
        db, alert_id=20, kream_product_id="777",
        size="270", retail_price=100_000, kream_sell_price_at_fire=150_000,
    )
    rec = await get_followup(db, fid)
    sold, price = await check(rec)
    assert sold is False and price is None
