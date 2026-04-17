"""decision_report — summary 집계 유닛 테스트."""

from __future__ import annotations

import time

import aiosqlite
import pytest

from src.core.decision_report import decision_summary

pytestmark = pytest.mark.asyncio


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT DEFAULT '',
    kream_product_id INTEGER DEFAULT 0,
    model_no TEXT DEFAULT '',
    extra TEXT DEFAULT ''
);
"""


@pytest.fixture
async def db(tmp_path) -> str:
    path = str(tmp_path / "test.db")
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()
    return path


async def _insert(db: str, ts: float, stage: str, decision: str, reason: str):
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "INSERT INTO decision_log (ts, stage, decision, reason) VALUES (?, ?, ?, ?)",
            (ts, stage, decision, reason),
        )
        await conn.commit()


async def test_empty_db_returns_zero_total(db: str):
    s = await decision_summary(db, hours=24.0)
    assert s.total == 0 and s.by_stage == {} and s.top_drops == []


async def test_aggregates_by_stage_and_reason(db: str):
    now = time.time()
    # candidate: pass 2, block 5 (block 중 size_intersection_empty=3, throttle=2)
    for _ in range(2):
        await _insert(db, now - 60, "candidate", "pass", "profit_emitted")
    for _ in range(3):
        await _insert(db, now - 60, "candidate", "block", "size_intersection_empty")
    for _ in range(2):
        await _insert(db, now - 60, "candidate", "block", "throttle_exhausted")
    # profit: pass 1, block 1
    await _insert(db, now - 60, "profit", "pass", "alert_sent")
    await _insert(db, now - 60, "profit", "block", "cooldown")

    s = await decision_summary(db, hours=24.0, top_n=10)
    assert s.total == 9
    assert s.by_stage["candidate"]["pass"] == 2
    assert s.by_stage["candidate"]["block"] == 5
    assert s.by_stage["profit"]["pass"] == 1
    assert s.by_stage["profit"]["block"] == 1

    # top drop 은 size_intersection_empty=3 이 1위
    assert s.top_drops[0].reason == "size_intersection_empty"
    assert s.top_drops[0].count == 3
    # throttle=2 가 2위
    assert s.top_drops[1].reason == "throttle_exhausted"
    assert s.top_drops[1].count == 2


async def test_window_excludes_old_rows(db: str):
    now = time.time()
    await _insert(db, now - 60, "candidate", "block", "profit_floor")
    await _insert(db, now - (48 * 3600), "candidate", "block", "profit_floor")  # 48h 전

    s = await decision_summary(db, hours=24.0)
    assert s.total == 1  # 24h 윈도우 밖 1건 제외