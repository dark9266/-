"""푸시→수집기 연동: kream_collect_queue + collect_pending 회귀 테스트.

배경:
- 푸시 파일럿 결과 무신사 신상 매칭률 2% 한계가 드러남.
- 진짜 가치는 "크림 미등재 신상품 조기경보". push_dump가 큐에 적재하고
  collector가 주기적으로 크림에서 검색해 등재 시점에 DB 추가.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from src.kream_realtime.collector import KreamCollector
from src.models.database import SCHEMA_SQL


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_SQL)
    # migrate_realtime_columns 동등: 컬럼 존재 보장
    for sql in [
        "ALTER TABLE kream_products ADD COLUMN volume_7d INTEGER DEFAULT 0",
        "ALTER TABLE kream_products ADD COLUMN refresh_tier TEXT DEFAULT 'cold'",
        "ALTER TABLE kream_products ADD COLUMN scan_priority TEXT DEFAULT 'cold'",
    ]:
        try:
            await conn.execute(sql)
        except Exception:
            pass
    await conn.commit()
    yield conn
    await conn.close()


async def _enqueue(db, model, brand="Nike", name="Test"):
    await db.execute(
        """INSERT OR IGNORE INTO kream_collect_queue
           (model_number, brand_hint, name_hint, source, source_url)
           VALUES (?, ?, ?, ?, ?)""",
        (model, brand, name, "musinsa", f"https://example.com/{model}"),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_enqueue_dedupe(db):
    """동일 model_number 두 번 enqueue → INSERT OR IGNORE로 1건만 유지."""
    await _enqueue(db, "IB1234-001")
    await _enqueue(db, "IB1234-001", brand="OtherBrand")
    cur = await db.execute(
        "SELECT COUNT(*), brand_hint FROM kream_collect_queue WHERE model_number=?",
        ("IB1234-001",),
    )
    count, brand = await cur.fetchone()
    assert count == 1
    assert brand == "Nike"  # 첫 번째 적재만 살아남음


@pytest.mark.asyncio
async def test_collect_pending_happy_path(db):
    """크림 검색에 모델번호가 발견되면 DB 추가 + status='found'."""
    await _enqueue(db, "IB1234-001", brand="Nike", name="Air Max")

    collector = KreamCollector(db)

    # 크림 검색 mock — listing 결과에 일치 모델번호 포함
    fake_listing = [
        {
            "product_id": "9999",
            "id": "9999",
            "name": "Nike Air Max IB1234",
            "brand": "Nike",
            "model_number": "IB1234-001",
            "trading_volume": 0,
        },
    ]

    with patch(
        "src.kream_realtime.collector.kream_crawler",
        MagicMock(
            _request=AsyncMock(return_value="<html>fake</html>"),
            _extract_page_data=MagicMock(return_value={"x": 1}),
            _extract_listing_products=MagicMock(return_value=fake_listing),
        ),
    ), patch("src.kream_realtime.collector._random_delay", new=AsyncMock()):
        result = await collector.collect_pending(batch_size=10)

    assert result["checked"] == 1
    assert result["found"] == 1
    assert result["not_found"] == 0

    # 큐 status=found
    cur = await db.execute(
        "SELECT status, found_product_id FROM kream_collect_queue WHERE model_number=?",
        ("IB1234-001",),
    )
    status, pid = await cur.fetchone()
    assert status == "found"
    assert pid == "9999"

    # kream_products 신규 행
    cur = await db.execute(
        "SELECT product_id, model_number FROM kream_products WHERE product_id=?",
        ("9999",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[1] == "IB1234-001"


@pytest.mark.asyncio
async def test_collect_pending_attempts_cap(db):
    """3회 미발견 → status='not_found'."""
    await _enqueue(db, "ZZ9999-999")

    collector = KreamCollector(db)

    # 크림 검색이 일치 항목 없음을 반환
    fake_listing = [
        {"product_id": "1", "model_number": "OTHER-000", "name": "x", "brand": "x"},
    ]

    with patch(
        "src.kream_realtime.collector.kream_crawler",
        MagicMock(
            _request=AsyncMock(return_value="<html/>"),
            _extract_page_data=MagicMock(return_value={"x": 1}),
            _extract_listing_products=MagicMock(return_value=fake_listing),
        ),
    ), patch("src.kream_realtime.collector._random_delay", new=AsyncMock()):
        # 3번 호출 → 매번 attempts++ → 마지막에 not_found
        for _ in range(3):
            await collector.collect_pending(batch_size=10)

    cur = await db.execute(
        "SELECT status, attempts FROM kream_collect_queue WHERE model_number=?",
        ("ZZ9999-999",),
    )
    status, attempts = await cur.fetchone()
    assert attempts == 3
    assert status == "not_found"

    # 4번째 호출은 큐에서 제외 (attempts >= MAX) → checked=0
    with patch(
        "src.kream_realtime.collector.kream_crawler",
        MagicMock(
            _request=AsyncMock(return_value="<html/>"),
            _extract_page_data=MagicMock(return_value={"x": 1}),
            _extract_listing_products=MagicMock(return_value=fake_listing),
        ),
    ), patch("src.kream_realtime.collector._random_delay", new=AsyncMock()):
        result = await collector.collect_pending(batch_size=10)
    assert result["checked"] == 0
