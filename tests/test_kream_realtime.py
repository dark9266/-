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


# -- KreamCollector 테스트 --


async def test_collector_saves_new_products():
    from src.kream_realtime.collector import KreamCollector
    db = await _create_db()

    mock_products = [
        {"product_id": "999001", "name": "Test Shoe 1", "brand": "Nike", "model_number": "TEST-001",
         "category": "신발", "buy_now_price": 150000, "sell_now_price": 120000, "trading_volume": 15,
         "image_url": "", "url": "https://kream.co.kr/products/999001"},
        {"product_id": "999002", "name": "Test Shoe 2", "brand": "Adidas", "model_number": "TEST-002",
         "category": "신발", "buy_now_price": 200000, "sell_now_price": 180000, "trading_volume": 3,
         "image_url": "", "url": "https://kream.co.kr/products/999002"},
    ]

    collector = KreamCollector(db)
    saved = await collector.save_products(mock_products)
    assert saved == 2

    cursor = await db.execute("SELECT * FROM kream_products WHERE product_id = '999001'")
    row = await cursor.fetchone()
    assert row is not None
    assert row["model_number"] == "TEST-001"
    assert row["volume_7d"] == 15
    await db.close()


async def test_collector_skips_existing():
    from src.kream_realtime.collector import KreamCollector
    db = await _create_db()
    await db.execute(
        "INSERT INTO kream_products (product_id, name, model_number) VALUES ('999001', 'Old', 'TEST-001')"
    )
    await db.commit()

    mock_products = [
        {"product_id": "999001", "name": "Updated Name", "brand": "Nike", "model_number": "TEST-001",
         "category": "신발", "buy_now_price": 150000, "sell_now_price": 0, "trading_volume": 10,
         "image_url": "", "url": ""},
    ]

    collector = KreamCollector(db)
    saved = await collector.save_products(mock_products)
    assert saved == 0

    cursor = await db.execute("SELECT name, volume_7d FROM kream_products WHERE product_id = '999001'")
    row = await cursor.fetchone()
    assert row["name"] == "Updated Name"
    assert row["volume_7d"] == 10
    await db.close()


def test_collector_parse_search_response():
    from src.kream_realtime.collector import KreamCollector
    data = {
        "items": [
            {"id": "123", "name": "Test", "brand": {"name": "Nike"},
             "style_code": "ABC-001", "trading_volume": 5,
             "image": {"url": "http://img.jpg"},
             "market": {"buy_now": 100000, "sell_now": 80000}},
        ]
    }
    results = KreamCollector._parse_search_response(data, "신발")
    assert len(results) == 1
    assert results[0]["product_id"] == "123"
    assert results[0]["model_number"] == "ABC-001"
    assert results[0]["brand"] == "Nike"
    assert results[0]["trading_volume"] == 5


# -- KreamPriceRefresher 테스트 --


@pytest.mark.asyncio
async def test_refresher_picks_hot_first():
    """hot tier 상품이 cold보다 먼저 선택되는지 확인."""
    from src.kream_realtime.price_refresher import KreamPriceRefresher

    db = await _create_db()

    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier, last_price_refresh)
        VALUES ('hot1', 'Hot Shoe', 'HOT-001', 20, 'hot',
                datetime('now', '-31 minutes'))"""
    )
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier, last_price_refresh)
        VALUES ('cold1', 'Cold Shoe', 'COLD-001', 2, 'cold',
                datetime('now', '-7 hours'))"""
    )
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier, last_price_refresh)
        VALUES ('hot2', 'Recent Hot', 'HOT-002', 30, 'hot',
                datetime('now', '-5 minutes'))"""
    )
    await db.commit()

    refresher = KreamPriceRefresher(db)
    queue = await refresher.build_refresh_queue(batch_size=10)

    pids = [row["product_id"] for row in queue]
    assert "hot1" in pids
    # cold tier는 price_refresher에서 제외 (연속 스캔 시세 즉석 조회로 대체)
    assert "cold1" not in pids
    # hot2는 5분 전 갱신이라 30분 미만 → 제외
    assert "hot2" not in pids
    await db.close()


@pytest.mark.asyncio
async def test_refresher_updates_tier():
    """거래량 변동 시 tier가 업데이트되는지 확인."""
    from src.kream_realtime.price_refresher import KreamPriceRefresher

    db = await _create_db()
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier)
        VALUES ('t1', 'Test', 'T-001', 2, 'cold')"""
    )
    await db.commit()

    refresher = KreamPriceRefresher(db)
    await refresher.update_product_tier("t1", new_volume_7d=10)

    cursor = await db.execute("SELECT refresh_tier FROM kream_products WHERE product_id = 't1'")
    row = await cursor.fetchone()
    assert row["refresh_tier"] == "hot"
    await db.close()


# -- VolumeSpikeDetector 테스트 --


@pytest.mark.asyncio
async def test_spike_detection():
    """이전 스냅샷 대비 2배 이상 증가하면 급등 감지."""
    from src.kream_realtime.volume_spike_detector import VolumeSpikeDetector

    db = await _create_db()
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier)
        VALUES ('sp1', 'Spike Shoe', 'SP-001', 5, 'cold')"""
    )
    await db.execute(
        """INSERT INTO kream_volume_snapshots
        (product_id, volume_7d, volume_30d, snapshot_at)
        VALUES ('sp1', 5, 20, datetime('now', '-2 hours'))"""
    )
    await db.commit()

    detector = VolumeSpikeDetector(db)
    spikes = await detector.detect_spikes(
        current_volumes=[{"product_id": "sp1", "volume_7d": 12, "volume_30d": 40}]
    )
    assert len(spikes) == 1
    assert spikes[0]["product_id"] == "sp1"
    assert spikes[0]["ratio"] > 2.0
    await db.close()


@pytest.mark.asyncio
async def test_no_spike_for_small_change():
    """1.5배 증가는 급등 아님 (threshold=2.0)."""
    from src.kream_realtime.volume_spike_detector import VolumeSpikeDetector

    db = await _create_db()
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier)
        VALUES ('ns1', 'Normal Shoe', 'NS-001', 10, 'hot')"""
    )
    await db.execute(
        """INSERT INTO kream_volume_snapshots
        (product_id, volume_7d, volume_30d, snapshot_at)
        VALUES ('ns1', 10, 30, datetime('now', '-2 hours'))"""
    )
    await db.commit()

    detector = VolumeSpikeDetector(db)
    spikes = await detector.detect_spikes(
        current_volumes=[{"product_id": "ns1", "volume_7d": 15, "volume_30d": 35}]
    )
    assert len(spikes) == 0
    await db.close()


@pytest.mark.asyncio
async def test_spike_promotes_to_hot():
    """급등 감지 시 cold → hot으로 승격되는지 확인."""
    from src.kream_realtime.volume_spike_detector import VolumeSpikeDetector

    db = await _create_db()
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier)
        VALUES ('pr1', 'Promote Shoe', 'PR-001', 3, 'cold')"""
    )
    await db.execute(
        """INSERT INTO kream_volume_snapshots
        (product_id, volume_7d, volume_30d, snapshot_at)
        VALUES ('pr1', 3, 10, datetime('now', '-2 hours'))"""
    )
    await db.commit()

    detector = VolumeSpikeDetector(db)
    spikes = await detector.detect_spikes(
        current_volumes=[{"product_id": "pr1", "volume_7d": 8, "volume_30d": 25}]
    )
    assert len(spikes) == 1

    await detector.promote_spiked_products(spikes)

    cursor = await db.execute("SELECT refresh_tier FROM kream_products WHERE product_id = 'pr1'")
    row = await cursor.fetchone()
    assert row["refresh_tier"] == "hot"
    await db.close()


@pytest.mark.asyncio
async def test_spike_save_snapshots():
    """스냅샷 저장 후 조회 확인."""
    from src.kream_realtime.volume_spike_detector import VolumeSpikeDetector

    db = await _create_db()
    await db.execute(
        "INSERT INTO kream_products (product_id, name, model_number) VALUES ('ss1', 'Snap', 'SS-001')"
    )
    await db.commit()

    detector = VolumeSpikeDetector(db)
    await detector.save_snapshots([
        {"product_id": "ss1", "volume_7d": 7, "volume_30d": 25}
    ])

    cursor = await db.execute("SELECT volume_7d FROM kream_volume_snapshots WHERE product_id = 'ss1'")
    row = await cursor.fetchone()
    assert row["volume_7d"] == 7
    await db.close()


def test_scheduler_has_realtime_loops():
    """Scheduler에 실시간 DB 루프가 정의되어 있는지 확인."""
    import ast
    source = open("src/scheduler.py").read()
    tree = ast.parse(source)

    method_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method_names.add(node.name)

    assert "collect_loop" in method_names, "collect_loop 없음"
    assert "refresh_loop" in method_names, "refresh_loop 없음"
    assert "spike_loop" in method_names, "spike_loop 없음"
