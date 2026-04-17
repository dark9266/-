"""dump_ledger — record / match_rate / unmatched_sample 유닛 테스트."""

from __future__ import annotations

import aiosqlite
import pytest

from src.core.dump_ledger import match_rate, record_dump_item, unmatched_sample

pytestmark = pytest.mark.asyncio


_RETAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS retail_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT DEFAULT '',
    model_number TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    url TEXT DEFAULT '',
    image_url TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, product_id)
);
"""


@pytest.fixture
async def db(tmp_path) -> str:
    path = str(tmp_path / "test.db")
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_RETAIL_SCHEMA)
        await conn.commit()
    return path


async def test_record_dump_item_upserts(db: str):
    await record_dump_item(db, source="tnf", model_no="NA5AS41B", name="Jacket A", url="http://x/a")
    # 재기록 — seen_count=2, last_seen_at 갱신
    await record_dump_item(db, source="tnf", model_no="NA5AS41B", name="", url="")
    async with aiosqlite.connect(db) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT seen_count, name, url FROM catalog_dump_items WHERE source=? AND model_no=?",
            ("tnf", "NA5AS41B"),
        ) as cur:
            row = await cur.fetchone()
    assert row["seen_count"] == 2
    # 빈 name/url upsert 는 기존 값 유지
    assert row["name"] == "Jacket A"
    assert row["url"] == "http://x/a"


async def test_record_ignores_empty(db: str):
    # 유효 기록 1건 (스키마 생성 + 기저 데이터)
    await record_dump_item(db, source="tnf", model_no="BASE1")
    await record_dump_item(db, source="", model_no="X")         # empty source — 무시
    await record_dump_item(db, source="tnf", model_no="")       # empty model — 무시
    async with aiosqlite.connect(db) as conn:
        async with conn.execute("SELECT COUNT(*) FROM catalog_dump_items") as cur:
            n = (await cur.fetchone())[0]
    assert n == 1  # BASE1 만 남음


async def test_match_rate_and_unmatched_sample(db: str):
    # 덤프 5건, retail 에는 2건만 매칭 기록
    for i, m in enumerate(["A1", "A2", "A3", "A4", "A5"]):
        await record_dump_item(db, source="tnf", model_no=m, name=f"item {m}", url=f"http://x/{m}")
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "INSERT INTO retail_products (source, product_id, model_number) VALUES (?, ?, ?)",
            ("tnf", "A1", "A1"),
        )
        await conn.execute(
            "INSERT INTO retail_products (source, product_id, model_number) VALUES (?, ?, ?)",
            ("tnf", "A2", "A2"),
        )
        await conn.commit()

    r = await match_rate(db, "tnf")
    assert r == {"source": "tnf", "dumped": 5, "matched": 2, "match_rate_pct": 40.0}

    unmatched = await unmatched_sample(db, "tnf", limit=10)
    unmatched_models = {u["model_no"] for u in unmatched}
    assert unmatched_models == {"A3", "A4", "A5"}


async def test_match_rate_zero_dump(db: str):
    r = await match_rate(db, "empty_source")
    assert r["dumped"] == 0 and r["matched"] == 0 and r["match_rate_pct"] is None
