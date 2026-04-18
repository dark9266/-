"""src/core/kream_index 싱글톤 + TTL 캐시 테스트."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.core.kream_index import (
    KreamIndex,
    get_kream_index,
    reset_kream_index,
    strip_key,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    """최소 kream_products 스키마 + 시드 데이터."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE kream_products (
            product_id TEXT PRIMARY KEY,
            name TEXT,
            brand TEXT,
            model_number TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO kream_products VALUES (?, ?, ?, ?)",
        [
            ("1", "Nike AJ1", "Nike", "DZ5485-612"),
            ("2", "Adidas Samba", "Adidas", "B75807"),
            ("3", "Empty Model", "Brand", ""),
        ],
    )
    conn.commit()
    conn.close()
    yield str(db)
    reset_kream_index()


def test_strip_key_removes_dashes_and_spaces():
    assert strip_key("DZ5485-612") == "DZ5485612"
    assert strip_key("ABC 123") == "ABC123"
    assert strip_key("") == ""


def test_kream_index_loads_rows(tmp_db: str):
    idx = KreamIndex(tmp_db, ttl_sec=60.0)
    data = idx.get()
    assert "DZ5485612" in data
    assert data["DZ5485612"]["product_id"] == "1"
    assert "B75807" in data
    # empty model_number 행은 제외되어야
    assert len(data) == 2


def test_ttl_cache_returns_same_object(tmp_db: str):
    idx = KreamIndex(tmp_db, ttl_sec=60.0)
    first = idx.get()
    second = idx.get()
    assert first is second  # same object, cache hit


def test_ttl_expiry_triggers_reload(tmp_db: str):
    idx = KreamIndex(tmp_db, ttl_sec=0.01)
    first = idx.get()
    time.sleep(0.02)
    second = idx.get()
    # 재적재되었으므로 다른 객체 (내용은 동일)
    assert first is not second
    assert first == second


def test_invalidate_forces_reload(tmp_db: str):
    idx = KreamIndex(tmp_db, ttl_sec=60.0)
    first = idx.get()
    idx.invalidate()
    second = idx.get()
    assert first is not second


def test_singleton_same_db_path(tmp_db: str):
    a = get_kream_index(tmp_db)
    b = get_kream_index(tmp_db)
    assert a is b


def test_singleton_different_paths_isolated(tmp_path: Path):
    db1 = tmp_path / "a.db"
    db2 = tmp_path / "b.db"
    for p in (db1, db2):
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE kream_products (product_id TEXT, name TEXT, brand TEXT, model_number TEXT)")
        conn.commit()
        conn.close()
    a = get_kream_index(str(db1))
    b = get_kream_index(str(db2))
    assert a is not b
    reset_kream_index()


def test_reset_kream_index(tmp_db: str):
    a = get_kream_index(tmp_db)
    reset_kream_index()
    b = get_kream_index(tmp_db)
    assert a is not b
