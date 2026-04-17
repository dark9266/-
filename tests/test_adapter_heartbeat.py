"""adapter_heartbeat — bump / snapshot / silent_adapters 유닛 테스트."""

from __future__ import annotations

import time

import pytest

from src.core.adapter_heartbeat import bump, silent_adapters, snapshot

pytestmark = pytest.mark.asyncio


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "test.db")


async def test_bump_creates_row_then_increments(db: str):
    await bump(db, "musinsa")
    snap = await snapshot(db)
    assert "musinsa" in snap
    last1, count1 = snap["musinsa"]
    assert count1 == 1 and last1 > 0

    await bump(db, "musinsa")
    snap = await snapshot(db)
    last2, count2 = snap["musinsa"]
    assert count2 == 2
    assert last2 >= last1  # 갱신됐거나 같음 (동일 초 가능)


async def test_bump_ignores_empty_source(db: str):
    await bump(db, "")
    assert await snapshot(db) == {}


async def test_silent_detects_never_emitted(db: str):
    await bump(db, "musinsa")
    result = await silent_adapters(db, ["musinsa", "nike"], threshold_hours=168.0)
    assert len(result) == 1
    assert result[0].source == "nike"
    assert result[0].last_emit_at is None
    assert result[0].reason == "never_emitted"


async def test_silent_detects_stale(db: str, monkeypatch):
    # musinsa 는 8일 전에 emit 한 것처럼 DB 에 직접 주입
    import aiosqlite
    from src.core.adapter_heartbeat import _ensure_schema

    old = time.time() - (8 * 24 * 3600)
    async with aiosqlite.connect(db) as conn:
        await _ensure_schema(conn)
        await conn.execute(
            "INSERT INTO adapter_heartbeat (source, last_emit_at, emit_count) VALUES (?, ?, ?)",
            ("musinsa", old, 100),
        )
        await conn.commit()

    result = await silent_adapters(db, ["musinsa"], threshold_hours=168.0)  # 7일 임계
    assert len(result) == 1
    assert result[0].source == "musinsa"
    assert result[0].reason == "stale"
    assert result[0].hours_since_last is not None
    assert result[0].hours_since_last >= 168


async def test_silent_empty_when_all_healthy(db: str):
    await bump(db, "musinsa")
    await bump(db, "nike")
    result = await silent_adapters(db, ["musinsa", "nike"], threshold_hours=168.0)
    assert result == []
