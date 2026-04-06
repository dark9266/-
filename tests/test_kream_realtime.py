"""kream_products 실시간 컬럼 & kream_volume_snapshots 테이블 테스트."""

import pytest
import aiosqlite

DB_PATH = ":memory:"

MIGRATION_SQL = """
ALTER TABLE kream_products ADD COLUMN volume_7d INTEGER DEFAULT 0;
ALTER TABLE kream_products ADD COLUMN volume_30d INTEGER DEFAULT 0;
ALTER TABLE kream_products ADD COLUMN last_volume_check TIMESTAMP;
ALTER TABLE kream_products ADD COLUMN refresh_tier TEXT DEFAULT 'cold';
ALTER TABLE kream_products ADD COLUMN last_price_refresh TIMESTAMP;
"""


async def _create_db():
    from src.models.database import SCHEMA_SQL
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA_SQL)
    for stmt in MIGRATION_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                await db.execute(stmt)
            except Exception:
                pass
    await db.commit()
    return db


@pytest.fixture
async def db():
    conn = await _create_db()
    yield conn
    await conn.close()


# -- kream_products 실시간 컬럼 존재 확인 --

async def test_kream_products_has_volume_7d(db):
    """volume_7d 컬럼이 kream_products 테이블에 존재해야 한다."""
    cursor = await db.execute("PRAGMA table_info(kream_products)")
    columns = {row["name"] for row in await cursor.fetchall()}
    assert "volume_7d" in columns


async def test_kream_products_has_refresh_tier(db):
    """refresh_tier 컬럼이 kream_products 테이블에 존재해야 한다."""
    cursor = await db.execute("PRAGMA table_info(kream_products)")
    columns = {row["name"] for row in await cursor.fetchall()}
    assert "refresh_tier" in columns


async def test_kream_products_has_last_price_refresh(db):
    """last_price_refresh 컬럼이 kream_products 테이블에 존재해야 한다."""
    cursor = await db.execute("PRAGMA table_info(kream_products)")
    columns = {row["name"] for row in await cursor.fetchall()}
    assert "last_price_refresh" in columns


# -- kream_volume_snapshots 테이블 동작 확인 --

async def test_volume_snapshots_insert_and_query(db):
    """kream_volume_snapshots 테이블에 INSERT 후 조회가 가능해야 한다."""
    # 먼저 참조할 상품 삽입
    await db.execute(
        "INSERT INTO kream_products (product_id, name, model_number) VALUES (?, ?, ?)",
        ("P001", "Test Shoe", "ABC-123"),
    )
    await db.execute(
        "INSERT INTO kream_volume_snapshots (product_id, volume_7d, volume_30d) VALUES (?, ?, ?)",
        ("P001", 150, 500),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT * FROM kream_volume_snapshots WHERE product_id = ?", ("P001",)
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["volume_7d"] == 150
    assert rows[0]["volume_30d"] == 500
    assert rows[0]["snapshot_at"] is not None
