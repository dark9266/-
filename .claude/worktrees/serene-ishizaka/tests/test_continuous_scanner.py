"""v2 연속 배치 스캐너 테스트."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.continuous_scanner import ContinuousScanner, ContinuousScanResult


class FakeConfig:
    """테스트용 설정."""

    continuous_scan_interval_minutes = 5
    continuous_scan_batch_size = 50
    continuous_hot_ttl_hours = 2
    continuous_warm_ttl_hours = 8
    continuous_cold_ttl_hours = 48
    httpx_concurrency = 10


class TestCalculateNextScan:
    """TTL 계산 테스트."""

    def _make_scanner(self):
        db = MagicMock()
        scanner_mock = MagicMock()
        return ContinuousScanner(db=db, scanner=scanner_mock, config=FakeConfig())

    def test_hot_ttl(self):
        """hot 상품: 2시간 TTL."""
        cs = self._make_scanner()
        now = datetime.now()
        result = cs._calculate_next_scan("hot", profitable=False)
        expected = now + timedelta(hours=2)
        assert abs((result - expected).total_seconds()) < 2

    def test_warm_ttl(self):
        """warm 상품: 8시간 TTL."""
        cs = self._make_scanner()
        now = datetime.now()
        result = cs._calculate_next_scan("warm", profitable=False)
        expected = now + timedelta(hours=8)
        assert abs((result - expected).total_seconds()) < 2

    def test_cold_ttl(self):
        """cold 상품: 48시간 TTL."""
        cs = self._make_scanner()
        now = datetime.now()
        result = cs._calculate_next_scan("cold", profitable=False)
        expected = now + timedelta(hours=48)
        assert abs((result - expected).total_seconds()) < 2

    def test_profitable_halves_ttl(self):
        """수익 발견 시 TTL 절반."""
        cs = self._make_scanner()
        now = datetime.now()
        result = cs._calculate_next_scan("warm", profitable=True)
        # warm 8h → 4h
        expected = now + timedelta(hours=4)
        assert abs((result - expected).total_seconds()) < 2

    def test_profitable_hot_min_1h(self):
        """hot 수익: 2h/2=1h (최소 1시간)."""
        cs = self._make_scanner()
        now = datetime.now()
        result = cs._calculate_next_scan("hot", profitable=True)
        expected = now + timedelta(hours=1)
        assert abs((result - expected).total_seconds()) < 2

    def test_unknown_priority_defaults_48h(self):
        """알 수 없는 우선순위: 48시간."""
        cs = self._make_scanner()
        now = datetime.now()
        result = cs._calculate_next_scan("unknown", profitable=False)
        expected = now + timedelta(hours=48)
        assert abs((result - expected).total_seconds()) < 2


class TestContinuousScanResult:
    """결과 dataclass 테스트."""

    def test_defaults(self):
        r = ContinuousScanResult()
        assert r.batch_size == 0
        assert r.processed == 0
        assert r.sourced == 0
        assert r.profitable == 0
        assert r.errors == 0
        assert r.elapsed_sec == 0.0
        assert r.opportunities == []


class TestRunBatchEmptyQueue:
    """빈 큐 테스트."""

    @pytest.mark.asyncio
    async def test_empty_queue_returns_zero(self):
        """큐가 비면 batch_size=0 반환."""
        db = AsyncMock()
        db.db = AsyncMock()

        # backfill: NULL 없음
        cursor_mock = AsyncMock()
        cursor_mock.fetchone = AsyncMock(return_value={"cnt": 0})
        db.db.execute = AsyncMock(return_value=cursor_mock)

        # 빈 큐
        db.get_continuous_scan_queue = AsyncMock(return_value=[])

        scanner_mock = MagicMock()
        cs = ContinuousScanner(db=db, scanner=scanner_mock, config=FakeConfig())

        result = await cs.run_batch()
        assert result.batch_size == 0
        assert result.processed == 0


class TestPriorityClassification:
    """volume_7d → scan_priority 매핑 (DB backfill 로직)."""

    @pytest.mark.asyncio
    async def test_priority_by_volume(self):
        """volume_7d >= 10 → hot, 3~9 → warm, <3 → cold."""
        import aiosqlite

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name

        try:
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("""
                    CREATE TABLE kream_products (
                        product_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        model_number TEXT NOT NULL,
                        brand TEXT DEFAULT '',
                        category TEXT DEFAULT 'sneakers',
                        image_url TEXT DEFAULT '',
                        url TEXT DEFAULT '',
                        volume_7d INTEGER DEFAULT 0,
                        volume_30d INTEGER DEFAULT 0,
                        scan_priority TEXT DEFAULT 'cold',
                        next_scan_at TIMESTAMP,
                        last_batch_scan_at TIMESTAMP
                    )
                """)
                await conn.execute(
                    "INSERT INTO kream_products (product_id, name, model_number, volume_7d) "
                    "VALUES ('1', 'Hot', 'HOT-001', 15)"
                )
                await conn.execute(
                    "INSERT INTO kream_products (product_id, name, model_number, volume_7d) "
                    "VALUES ('2', 'Warm', 'WARM-001', 5)"
                )
                await conn.execute(
                    "INSERT INTO kream_products (product_id, name, model_number, volume_7d) "
                    "VALUES ('3', 'Cold', 'COLD-001', 1)"
                )
                await conn.commit()

                # backfill SQL 직접 실행
                await conn.execute(
                    "UPDATE kream_products SET scan_priority = 'hot' "
                    "WHERE model_number != '' AND volume_7d >= 10 AND next_scan_at IS NULL"
                )
                await conn.execute(
                    "UPDATE kream_products SET scan_priority = 'warm' "
                    "WHERE model_number != '' AND volume_7d >= 3 AND volume_7d < 10 "
                    "AND next_scan_at IS NULL"
                )
                await conn.execute(
                    "UPDATE kream_products SET scan_priority = 'cold' "
                    "WHERE model_number != '' AND volume_7d < 3 AND next_scan_at IS NULL"
                )
                await conn.commit()

                cursor = await conn.execute(
                    "SELECT product_id, scan_priority FROM kream_products ORDER BY product_id"
                )
                rows = await cursor.fetchall()
                results = {row["product_id"]: row["scan_priority"] for row in rows}

                assert results["1"] == "hot"
                assert results["2"] == "warm"
                assert results["3"] == "cold"
        finally:
            Path(db_path).unlink(missing_ok=True)


class TestQueueOrdering:
    """큐 정렬 순서 테스트."""

    @pytest.mark.asyncio
    async def test_hot_before_cold(self):
        """hot > warm > cold 순서."""
        import aiosqlite

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name

        try:
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("""
                    CREATE TABLE kream_products (
                        product_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        model_number TEXT NOT NULL,
                        brand TEXT DEFAULT '',
                        category TEXT DEFAULT 'sneakers',
                        image_url TEXT DEFAULT '',
                        url TEXT DEFAULT '',
                        volume_7d INTEGER DEFAULT 0,
                        volume_30d INTEGER DEFAULT 0,
                        scan_priority TEXT DEFAULT 'cold',
                        next_scan_at TIMESTAMP,
                        last_batch_scan_at TIMESTAMP
                    )
                """)
                # cold 먼저 삽입, hot 나중에 — 정렬이 제대로 되는지 확인
                # next_scan_at을 NULL로 설정 (항상 큐에 포함됨)
                await conn.execute(
                    "INSERT INTO kream_products "
                    "(product_id, name, model_number, scan_priority) "
                    "VALUES ('1', 'Cold', 'C-001', 'cold')"
                )
                await conn.execute(
                    "INSERT INTO kream_products "
                    "(product_id, name, model_number, scan_priority) "
                    "VALUES ('2', 'Hot', 'H-001', 'hot')"
                )
                await conn.execute(
                    "INSERT INTO kream_products "
                    "(product_id, name, model_number, scan_priority) "
                    "VALUES ('3', 'Warm', 'W-001', 'warm')"
                )
                await conn.commit()

                cursor = await conn.execute("""
                    SELECT product_id, scan_priority FROM kream_products
                    WHERE model_number != ''
                      AND (next_scan_at IS NULL OR next_scan_at <= datetime('now'))
                    ORDER BY
                      CASE scan_priority WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END,
                      CASE WHEN next_scan_at IS NULL THEN 0 ELSE 1 END,
                      next_scan_at ASC
                """)
                rows = await cursor.fetchall()
                priorities = [row["scan_priority"] for row in rows]

                assert priorities == ["hot", "warm", "cold"]
        finally:
            Path(db_path).unlink(missing_ok=True)
