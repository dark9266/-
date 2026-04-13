"""Phase 3 runtime-sentinel 대시보드 헬스핀 테스트.

4개 위젯을 검증:
  1) 소싱처 헬스핀 (sample 데이터 있음 / 없음)
  2) 크림 캡 게이지 (사용량 0 / 50% / 90% / 100%)
  3) 파이프라인 카운터 (status × event_type)
  4) v3 알림 피드 (최근순)

추가로 읽기 전용 보장 — 쓰기 쿼리 호출 시 OperationalError 가 나야 함.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 픽스처 — 임시 DB 에 Phase 3 스키마 + 샘플 데이터 셋업
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """테스트용 최소 스키마 — queries.py 가 참조하는 테이블만."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_checkpoint (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL,
            consumed_at REAL,
            consumer TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS kream_api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            endpoint TEXT NOT NULL,
            method TEXT NOT NULL,
            status INTEGER,
            latency_ms INTEGER,
            purpose TEXT
        );

        CREATE TABLE IF NOT EXISTS alert_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_id INTEGER UNIQUE NOT NULL,
            kream_product_id INTEGER NOT NULL,
            signal TEXT NOT NULL,
            fired_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kream_products (
            product_id TEXT PRIMARY KEY,
            brand TEXT DEFAULT '',
            name TEXT DEFAULT '',
            model_number TEXT DEFAULT ''
        );

        -- 기존 대시보드 쿼리 호환 (alert_stats_24h 등이 참조)
        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            alert_type TEXT,
            best_profit INTEGER,
            signal TEXT,
            source TEXT,
            roi REAL,
            direction TEXT,
            retail_price INTEGER,
            kream_sell_price INTEGER,
            size TEXT,
            kream_product_id TEXT
        );

        CREATE TABLE IF NOT EXISTS retail_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            model_number TEXT,
            fetched_at TIMESTAMP
        );
        """
    )
    conn.commit()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """임시 DB + settings.db_path 패치."""
    db_path = tmp_path / "sentinel.db"
    conn = sqlite3.connect(str(db_path))
    _create_schema(conn)
    conn.close()

    from src.config import settings

    monkeypatch.setattr(settings, "db_path", str(db_path), raising=True)
    yield db_path


def _insert_checkpoint(
    db_path: Path,
    *,
    event_type: str,
    payload: dict,
    status: str,
    created_at: float | None = None,
    consumer: str = "test",
) -> int:
    conn = sqlite3.connect(str(db_path))
    ts = created_at if created_at is not None else time.time()
    cur = conn.execute(
        "INSERT INTO event_checkpoint "
        "(event_type, payload, created_at, consumer, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type, json.dumps(payload), ts, consumer, status),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return int(row_id or 0)


def _insert_kream_call(db_path: Path, count: int, purpose: str = "push_dump") -> None:
    conn = sqlite3.connect(str(db_path))
    for _ in range(count):
        conn.execute(
            "INSERT INTO kream_api_calls(endpoint, method, status, latency_ms, purpose) "
            "VALUES (?, ?, ?, ?, ?)",
            ("/api/p/e/products/1", "GET", 200, 120, purpose),
        )
    conn.commit()
    conn.close()


def _insert_alert_sent(
    db_path: Path,
    *,
    checkpoint_id: int,
    kream_product_id: int,
    signal: str,
    fired_at: float,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO alert_sent(checkpoint_id, kream_product_id, signal, fired_at) "
        "VALUES (?, ?, ?, ?)",
        (checkpoint_id, kream_product_id, signal, fired_at),
    )
    conn.commit()
    conn.close()


def _insert_kream_product(
    db_path: Path, *, product_id: str, brand: str, name: str, model_number: str
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO kream_products(product_id, brand, name, model_number) "
        "VALUES (?, ?, ?, ?)",
        (product_id, brand, name, model_number),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1) 어댑터 헬스핀 — queries 레벨
# ---------------------------------------------------------------------------


class TestAdapterHealth:
    def test_empty_when_no_events(self, temp_db):
        from src.dashboard import queries

        assert queries.adapter_health_24h() == []

    def test_aggregates_by_source(self, temp_db):
        from src.dashboard import queries

        # musinsa: 2 consumed + 1 failed
        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "musinsa", "dumped": 100, "matched": 80},
            status="consumed",
        )
        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "musinsa", "dumped": 50, "matched": 40},
            status="consumed",
        )
        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "musinsa", "dumped": 0, "matched": 0},
            status="failed",
        )
        # nike: 1 consumed
        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "nike", "dumped": 30, "matched": 25},
            status="consumed",
        )

        rows = queries.adapter_health_24h()
        by_src = {r["source"]: r for r in rows}
        assert set(by_src) == {"musinsa", "nike"}

        m = by_src["musinsa"]
        assert m["total"] == 3
        assert m["consumed"] == 2
        assert m["failed"] == 1
        assert m["dumped_24h"] == 150
        assert m["matched_24h"] == 120
        assert m["success_rate"] == round(2 * 100 / 3, 1)
        assert m["last_at_iso"] is not None

        n = by_src["nike"]
        assert n["total"] == 1
        assert n["success_rate"] == 100.0

    def test_ignores_old_events(self, temp_db):
        from src.dashboard import queries

        old_ts = time.time() - 86400 * 2  # 2일 전
        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "musinsa", "dumped": 100, "matched": 80},
            status="consumed",
            created_at=old_ts,
        )
        assert queries.adapter_health_24h() == []

    def test_ignores_non_catalog_events(self, temp_db):
        from src.dashboard import queries

        _insert_checkpoint(
            temp_db,
            event_type="ProfitFound",
            payload={"source": "musinsa"},
            status="consumed",
        )
        assert queries.adapter_health_24h() == []


# ---------------------------------------------------------------------------
# 2) 크림 캡 게이지
# ---------------------------------------------------------------------------


class TestKreamBudgetGauge:
    def test_zero_usage(self, temp_db):
        from src.dashboard import queries

        u = queries.kream_budget_usage(cap=10000)
        assert u["used"] == 0
        assert u["cap"] == 10000
        assert u["ratio"] == 0.0
        assert u["pct"] == 0.0
        assert u["remaining"] == 10000
        assert u["purposes"] == []

    def test_half_usage(self, temp_db):
        from src.dashboard import queries

        _insert_kream_call(temp_db, count=50)
        u = queries.kream_budget_usage(cap=100)
        assert u["used"] == 50
        assert u["ratio"] == 0.5
        assert u["pct"] == 50.0
        assert u["remaining"] == 50

    def test_ninety_pct(self, temp_db):
        from src.dashboard import queries

        _insert_kream_call(temp_db, count=90)
        u = queries.kream_budget_usage(cap=100)
        assert u["pct"] == 90.0

    def test_hundred_pct(self, temp_db):
        from src.dashboard import queries

        _insert_kream_call(temp_db, count=100)
        u = queries.kream_budget_usage(cap=100)
        assert u["used"] == 100
        assert u["ratio"] == 1.0
        assert u["pct"] == 100.0
        assert u["remaining"] == 0

    def test_purposes_grouped(self, temp_db):
        from src.dashboard import queries

        _insert_kream_call(temp_db, count=5, purpose="push_dump")
        _insert_kream_call(temp_db, count=3, purpose="collector")
        u = queries.kream_budget_usage(cap=100)
        assert u["used"] == 8
        pmap = {p["purpose"]: p["cnt"] for p in u["purposes"]}
        assert pmap == {"push_dump": 5, "collector": 3}


# ---------------------------------------------------------------------------
# 3) 파이프라인 카운터
# ---------------------------------------------------------------------------


class TestPipelineCounters:
    def test_empty(self, temp_db):
        from src.dashboard import queries

        p = queries.pipeline_counters_1h()
        assert p["total"] == 0
        assert p["by_status"] == {
            "pending": 0,
            "deferred": 0,
            "consumed": 0,
            "failed": 0,
        }
        assert p["by_type"] == []

    def test_counts_status_and_type(self, temp_db):
        from src.dashboard import queries

        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "musinsa"},
            status="consumed",
        )
        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "nike"},
            status="pending",
        )
        _insert_checkpoint(
            temp_db,
            event_type="ProfitFound",
            payload={},
            status="deferred",
        )
        _insert_checkpoint(
            temp_db,
            event_type="AlertSent",
            payload={},
            status="failed",
        )

        p = queries.pipeline_counters_1h()
        assert p["total"] == 4
        assert p["by_status"]["consumed"] == 1
        assert p["by_status"]["pending"] == 1
        assert p["by_status"]["deferred"] == 1
        assert p["by_status"]["failed"] == 1
        types = {(r["event_type"], r["status"]) for r in p["by_type"]}
        assert ("CatalogDumped", "consumed") in types
        assert ("ProfitFound", "deferred") in types


# ---------------------------------------------------------------------------
# 4) v3 알림 피드
# ---------------------------------------------------------------------------


class TestV3AlertFeed:
    def test_empty(self, temp_db):
        from src.dashboard import queries

        assert queries.recent_v3_alerts(limit=10) == []

    def test_returns_sorted_desc_by_fired_at(self, temp_db):
        from src.dashboard import queries

        now = time.time()
        _insert_kream_product(
            temp_db, product_id="101", brand="Nike",
            name="Dunk Low", model_number="DD1391-100",
        )
        _insert_kream_product(
            temp_db, product_id="102", brand="Adidas",
            name="Samba OG", model_number="B75806",
        )

        _insert_alert_sent(
            temp_db, checkpoint_id=1, kream_product_id=101,
            signal="BUY", fired_at=now - 300,
        )
        _insert_alert_sent(
            temp_db, checkpoint_id=2, kream_product_id=102,
            signal="STRONG_BUY", fired_at=now - 100,
        )
        _insert_alert_sent(
            temp_db, checkpoint_id=3, kream_product_id=101,
            signal="WATCH", fired_at=now - 10,
        )

        rows = queries.recent_v3_alerts(limit=10)
        assert len(rows) == 3
        # 최근순
        assert rows[0]["checkpoint_id"] == 3
        assert rows[1]["checkpoint_id"] == 2
        assert rows[2]["checkpoint_id"] == 1
        # JOIN 결과
        assert rows[0]["brand"] == "Nike"
        assert rows[1]["brand"] == "Adidas"
        assert rows[0]["fired_at_iso"] is not None

    def test_respects_limit(self, temp_db):
        from src.dashboard import queries

        now = time.time()
        for i in range(5):
            _insert_alert_sent(
                temp_db, checkpoint_id=i + 1, kream_product_id=999,
                signal="BUY", fired_at=now - i,
            )
        assert len(queries.recent_v3_alerts(limit=3)) == 3


# ---------------------------------------------------------------------------
# 5) 읽기 전용 보장
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_queries_use_readonly_uri(self, temp_db):
        """`_conn()` 은 `mode=ro` URI 로 연결하므로 INSERT 가 실패해야 한다."""
        from src.dashboard import queries

        conn = queries._conn()
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO kream_api_calls(endpoint, method) VALUES (?, ?)",
                    ("/x", "GET"),
                )
        finally:
            conn.close()

    def test_health_queries_dont_mutate(self, temp_db):
        """헬스핀 쿼리 4종 호출 후에도 DB 행 수가 불변."""
        from src.dashboard import queries

        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "musinsa", "dumped": 10, "matched": 5},
            status="consumed",
        )
        _insert_kream_call(temp_db, count=3)
        _insert_alert_sent(
            temp_db, checkpoint_id=1, kream_product_id=1,
            signal="BUY", fired_at=time.time(),
        )

        def _row_count(table: str) -> int:
            c = sqlite3.connect(str(temp_db))
            n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            c.close()
            return int(n)

        before = {
            "event_checkpoint": _row_count("event_checkpoint"),
            "kream_api_calls": _row_count("kream_api_calls"),
            "alert_sent": _row_count("alert_sent"),
        }

        queries.adapter_health_24h()
        queries.kream_budget_usage()
        queries.pipeline_counters_1h()
        queries.recent_v3_alerts(limit=10)

        after = {
            "event_checkpoint": _row_count("event_checkpoint"),
            "kream_api_calls": _row_count("kream_api_calls"),
            "alert_sent": _row_count("alert_sent"),
        }
        assert before == after


# ---------------------------------------------------------------------------
# 6) /health 라우터 end-to-end (TestClient)
# ---------------------------------------------------------------------------


class TestHealthRoute:
    def test_route_returns_200_with_empty_db(self, temp_db):
        from src.dashboard.app import app

        client = TestClient(app)
        r = client.get("/health")
        assert r.status_code == 200
        assert "헬스핀" in r.text
        assert "크림 호출" in r.text

    def test_route_renders_adapter_rows(self, temp_db):
        from src.dashboard.app import app

        _insert_checkpoint(
            temp_db,
            event_type="CatalogDumped",
            payload={"source": "musinsa", "dumped": 42, "matched": 33},
            status="consumed",
        )
        _insert_kream_call(temp_db, count=9000)  # 90%
        _insert_alert_sent(
            temp_db, checkpoint_id=1, kream_product_id=1,
            signal="BUY", fired_at=time.time(),
        )

        client = TestClient(app)
        r = client.get("/health")
        assert r.status_code == 200
        assert "musinsa" in r.text
        # 90% 표시
        assert "90.0%" in r.text or "90%" in r.text

    def test_nav_has_health_link(self, temp_db):
        from src.dashboard.app import app

        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200
        assert 'href="/health"' in r.text
