# 크림 실시간 DB 구축 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 크림 DB를 정적 참조 테이블에서 살아있는 실시간 레이어로 전환 — 신규 상품 자동 수집, 거래량 기반 우선순위 시세 갱신, 거래량 급등 감지.

**Architecture:** 3개의 독립 모듈을 `src/kream_realtime/` 패키지에 구현. 기존 `scheduler.py`의 `discord.ext.tasks` 루프에 새 루프 3개 추가. 기존 `kream_db_builder.py`의 API 수집 로직(`_fetch_category_page_api`)과 `kream.py`의 상품 상세/거래량 API를 재활용. DB 스키마는 `kream_products` 테이블에 `volume_7d`, `last_volume_check`, `refresh_tier` 컬럼 추가 + `kream_volume_snapshots` 테이블 신설.

**Tech Stack:** Python 3.12+, aiosqlite, aiohttp, discord.ext.tasks, 기존 kream_crawler 인스턴스

---

## 현재 상태 요약

| 항목 | 수치 |
|------|------|
| kream_products | 47,180개 (마지막 갱신: 2026-03-28) |
| kream_price_history | 387건 (0.8%) |
| kream_volume_history | 0건 |
| 수집 방식 | kream_db_builder.py → JSON 일괄 → SQLite (수동) |

## 파일 구조

```
src/kream_realtime/           # 새 패키지
  __init__.py                 # 패키지 init
  collector.py                # Task 1: 신규 상품 자동 수집
  price_refresher.py          # Task 2: 우선순위 시세 갱신
  volume_spike_detector.py    # Task 3: 거래량 급등 감지

src/models/database.py        # 스키마 확장 + 새 메서드
src/scheduler.py              # 새 루프 3개 추가
src/config.py                 # 새 설정값 추가
tests/test_kream_realtime.py  # 통합 테스트
```

## API 엔드포인트 정리 (기존 코드에서 확인됨, GET 전용)

| 용도 | 엔드포인트 | 파라미터 |
|------|-----------|---------|
| 카테고리별 상품 리스트 | `/api/p/e/search/products` | keyword, sort, page, per_page, tab=products |
| 상품 상세 | `/api/p/e/products/{id}` | - |
| 사이즈별 시세 | `/api/p/options/display` | product_id |
| 거래 내역 | `/api/p/e/products/{id}/sales` | cursor, per_page |
| 카테고리별 인기 | `/api/p/e/categories/products` | category, sort, page, per_page |

---

## Task 1: DB 스키마 확장

**Files:**
- Modify: `src/models/database.py` (SCHEMA_SQL + 새 메서드)
- Test: `tests/test_kream_realtime.py`

- [ ] **Step 1: Write failing test for new schema**

```python
# tests/test_kream_realtime.py
import pytest
import aiosqlite

DB_PATH = ":memory:"

MIGRATION_SQL = """
ALTER TABLE kream_products ADD COLUMN volume_7d INTEGER DEFAULT 0;
ALTER TABLE kream_products ADD COLUMN volume_30d INTEGER DEFAULT 0;
ALTER TABLE kream_products ADD COLUMN last_volume_check TIMESTAMP;
ALTER TABLE kream_products ADD COLUMN refresh_tier TEXT DEFAULT 'cold';
ALTER TABLE kream_products ADD COLUMN last_price_refresh TIMESTAMP;

CREATE TABLE IF NOT EXISTS kream_volume_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    volume_7d INTEGER DEFAULT 0,
    volume_30d INTEGER DEFAULT 0,
    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES kream_products(product_id)
);
CREATE INDEX IF NOT EXISTS idx_vol_snap_product ON kream_volume_snapshots(product_id, snapshot_at);
"""


async def _create_db():
    """테스트용 DB 생성 (기존 스키마 + 마이그레이션)."""
    from src.models.database import SCHEMA_SQL
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA_SQL)
    # 마이그레이션 적용
    for stmt in MIGRATION_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                await db.execute(stmt)
            except Exception:
                pass  # ALTER 중복 실행 무시
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_schema_has_new_columns():
    db = await _create_db()
    cursor = await db.execute("PRAGMA table_info(kream_products)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "volume_7d" in cols
    assert "refresh_tier" in cols
    assert "last_price_refresh" in cols
    await db.close()


@pytest.mark.asyncio
async def test_volume_snapshots_table():
    db = await _create_db()
    await db.execute(
        "INSERT INTO kream_products (product_id, name, model_number) VALUES ('1', 'Test', 'TEST-001')"
    )
    await db.execute(
        "INSERT INTO kream_volume_snapshots (product_id, volume_7d, volume_30d) VALUES ('1', 10, 50)"
    )
    await db.commit()
    cursor = await db.execute("SELECT * FROM kream_volume_snapshots WHERE product_id = '1'")
    row = await cursor.fetchone()
    assert row is not None
    assert row["volume_7d"] == 10
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py -v`
Expected: FAIL (file doesn't exist yet)

- [ ] **Step 3: Implement schema migration in database.py**

`src/models/database.py` — SCHEMA_SQL 끝에 `kream_volume_snapshots` 테이블 추가, `Database` 클래스에 마이그레이션 메서드 추가:

```python
# SCHEMA_SQL 끝에 추가
"""
-- 크림 거래량 스냅샷 (급등 감지용)
CREATE TABLE IF NOT EXISTS kream_volume_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    volume_7d INTEGER DEFAULT 0,
    volume_30d INTEGER DEFAULT 0,
    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES kream_products(product_id)
);
CREATE INDEX IF NOT EXISTS idx_vol_snap_product ON kream_volume_snapshots(product_id, snapshot_at);
"""

# Database 클래스에 추가
async def migrate_realtime_columns(self) -> None:
    """kream_products에 실시간 DB용 컬럼 추가 (멱등)."""
    migrations = [
        "ALTER TABLE kream_products ADD COLUMN volume_7d INTEGER DEFAULT 0",
        "ALTER TABLE kream_products ADD COLUMN volume_30d INTEGER DEFAULT 0",
        "ALTER TABLE kream_products ADD COLUMN last_volume_check TIMESTAMP",
        "ALTER TABLE kream_products ADD COLUMN refresh_tier TEXT DEFAULT 'cold'",
        "ALTER TABLE kream_products ADD COLUMN last_price_refresh TIMESTAMP",
    ]
    for sql in migrations:
        try:
            await self.db.execute(sql)
        except Exception:
            pass  # 이미 존재하는 컬럼 무시
    await self.db.commit()
    logger.info("실시간 DB 마이그레이션 완료")
```

`Database.connect()` 끝에 `await self.migrate_realtime_columns()` 호출 추가.

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/models/database.py tests/test_kream_realtime.py
git commit -m "feat: add realtime DB schema — volume_snapshots table + kream_products migration columns"
```

---

## Task 2: 설정값 추가

**Files:**
- Modify: `src/config.py`

- [ ] **Step 1: Add realtime DB settings to config.py**

`src/config.py` `Settings` 클래스에 추가:

```python
# 실시간 DB
realtime_collect_interval_hours: int = 6       # 신규 상품 수집 주기
realtime_hot_refresh_minutes: int = 30         # hot 상품 시세 갱신 주기
realtime_cold_refresh_hours: int = 6           # cold 상품 시세 갱신 주기
realtime_volume_check_minutes: int = 60        # 거래량 체크 주기
realtime_spike_threshold: float = 2.0          # 거래량 급등 판정 배율 (이전 대비)
realtime_hot_volume_min: int = 5               # hot tier 최소 7일 거래량
realtime_collect_pages_per_keyword: int = 5    # 수집 시 키워드당 페이지 수
realtime_refresh_batch_size: int = 20          # 1회 시세 갱신 배치 크기
```

- [ ] **Step 2: Verify config loads**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && python3 -c "from src.config import settings; print(settings.realtime_hot_refresh_minutes)"`
Expected: `30`

- [ ] **Step 3: Commit**

```bash
git add src/config.py
git commit -m "feat: add realtime DB config settings"
```

---

## Task 3: 신규 상품 자동 수집 (`collector.py`)

**Files:**
- Create: `src/kream_realtime/__init__.py`
- Create: `src/kream_realtime/collector.py`
- Test: `tests/test_kream_realtime.py` (추가)

- [ ] **Step 1: Write failing test for collector**

`tests/test_kream_realtime.py`에 추가:

```python
@pytest.mark.asyncio
async def test_collector_saves_new_products():
    """수집된 상품이 DB에 저장되는지 확인."""
    from src.kream_realtime.collector import KreamCollector

    db = await _create_db()

    # 모의 크롤러 — _fetch_category_page_api를 시뮬레이션
    mock_products = [
        {
            "product_id": "999001",
            "name": "Test Shoe 1",
            "brand": "Nike",
            "model_number": "TEST-001",
            "category": "신발",
            "buy_now_price": 150000,
            "sell_now_price": 120000,
            "trading_volume": 15,
            "image_url": "",
            "url": "https://kream.co.kr/products/999001",
        },
        {
            "product_id": "999002",
            "name": "Test Shoe 2",
            "brand": "Adidas",
            "model_number": "TEST-002",
            "category": "신발",
            "buy_now_price": 200000,
            "sell_now_price": 180000,
            "trading_volume": 3,
            "image_url": "",
            "url": "https://kream.co.kr/products/999002",
        },
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


@pytest.mark.asyncio
async def test_collector_skips_existing():
    """이미 존재하는 상품은 업데이트만, 카운트 안 함."""
    from src.kream_realtime.collector import KreamCollector

    db = await _create_db()
    await db.execute(
        "INSERT INTO kream_products (product_id, name, model_number) VALUES ('999001', 'Old', 'TEST-001')"
    )
    await db.commit()

    mock_products = [
        {
            "product_id": "999001",
            "name": "Updated Name",
            "brand": "Nike",
            "model_number": "TEST-001",
            "category": "신발",
            "buy_now_price": 150000,
            "sell_now_price": 0,
            "trading_volume": 10,
            "image_url": "",
            "url": "",
        },
    ]

    collector = KreamCollector(db)
    saved = await collector.save_products(mock_products)

    # 기존 상품 업데이트 → new_count=0
    assert saved == 0

    cursor = await db.execute("SELECT name, volume_7d FROM kream_products WHERE product_id = '999001'")
    row = await cursor.fetchone()
    assert row["name"] == "Updated Name"
    assert row["volume_7d"] == 10
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py::test_collector_saves_new_products -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement collector.py**

```python
# src/kream_realtime/__init__.py
"""크림 실시간 DB 패키지."""

# src/kream_realtime/collector.py
"""신규 상품 자동 수집.

kream_db_builder.py의 _fetch_category_page_api 로직을 재활용하여
카테고리별 신규 상품을 주기적으로 수집, DB에 추가한다.
"""

import asyncio
import random
from datetime import datetime

import aiosqlite

from src.crawlers.kream import kream_crawler, KREAM_BASE, _random_delay
from src.config import settings
from src.kream_db_builder import CATEGORIES
from src.utils.logging import setup_logger

logger = setup_logger("kream_collector")


class KreamCollector:
    """크림 신규 상품 수집기."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def save_products(self, products: list[dict]) -> int:
        """상품 리스트를 DB에 upsert. 신규 삽입 건수 반환."""
        new_count = 0
        for p in products:
            pid = str(p.get("product_id", ""))
            if not pid:
                continue

            # 기존 존재 여부 확인
            cursor = await self.db.execute(
                "SELECT product_id FROM kream_products WHERE product_id = ?", (pid,)
            )
            exists = await cursor.fetchone()

            volume = int(p.get("trading_volume", 0) or 0)

            if exists:
                # 업데이트만
                await self.db.execute(
                    """UPDATE kream_products SET
                        name = ?, brand = ?, volume_7d = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE product_id = ?""",
                    (p.get("name", ""), p.get("brand", ""), volume, pid),
                )
            else:
                # 신규 삽입
                model_number = str(p.get("model_number", "")).strip()
                await self.db.execute(
                    """INSERT INTO kream_products
                    (product_id, name, model_number, brand, category, image_url, url,
                     volume_7d, refresh_tier)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pid,
                        p.get("name", ""),
                        model_number,
                        p.get("brand", ""),
                        p.get("category", "신발"),
                        p.get("image_url", ""),
                        p.get("url", f"{KREAM_BASE}/products/{pid}"),
                        volume,
                        "hot" if volume >= settings.realtime_hot_volume_min else "cold",
                    ),
                )
                new_count += 1

        await self.db.commit()
        return new_count

    async def collect_category(
        self,
        category_name: str,
        keyword: str,
        max_pages: int = 5,
    ) -> int:
        """단일 카테고리+키워드 수집. 신규 건수 반환."""
        total_new = 0
        empty_streak = 0

        for page in range(1, max_pages + 1):
            await _random_delay()

            params = {
                "keyword": keyword,
                "sort": "date",  # 최신순 — 신규 상품 우선
                "page": page,
                "per_page": 40,
                "tab": "products",
            }
            data = await kream_crawler._request(
                "GET", "/api/p/e/search/products", params=params, max_retries=2,
            )
            if not data:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue

            products = self._parse_search_response(data, category_name)
            if not products:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue

            empty_streak = 0
            new_count = await self.save_products(products)
            total_new += new_count

            logger.info(
                "%s [%s] page %d: %d건 (신규 %d)",
                category_name, keyword, page, len(products), new_count,
            )

            # 신규가 0이면 이미 수집된 영역 → 다음 키워드로
            if new_count == 0:
                break

        return total_new

    async def run(self, max_pages_per_keyword: int | None = None) -> dict:
        """전체 카테고리 수집 실행.

        Returns:
            {"total_new": int, "by_category": dict, "elapsed": float}
        """
        if max_pages_per_keyword is None:
            max_pages_per_keyword = settings.realtime_collect_pages_per_keyword

        started = datetime.now()
        stats = {}
        total_new = 0

        for cat_name, cat_config in CATEGORIES.items():
            cat_new = 0
            for keyword in cat_config["keywords"]:
                new = await self.collect_category(cat_name, keyword, max_pages_per_keyword)
                cat_new += new
            stats[cat_name] = cat_new
            total_new += cat_new

        elapsed = (datetime.now() - started).total_seconds()
        logger.info("수집 완료: 신규 %d건, %.0f초", total_new, elapsed)

        return {"total_new": total_new, "by_category": stats, "elapsed": elapsed}

    @staticmethod
    def _parse_search_response(data: dict, category: str) -> list[dict]:
        """검색 API 응답 파싱. kream_db_builder._fetch_category_page_api 로직 재활용."""
        items = (
            data.get("items")
            or data.get("products")
            or (data.get("data", {}).get("items") if isinstance(data.get("data"), dict) else None)
            or (data.get("data", {}).get("products") if isinstance(data.get("data"), dict) else None)
            or (data.get("data") if isinstance(data.get("data"), list) else None)
            or []
        )

        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            product_id = str(item.get("id") or item.get("product_id") or "")
            if not product_id or product_id == "None":
                continue

            name = item.get("name") or item.get("translated_name") or ""
            brand_raw = item.get("brand")
            if isinstance(brand_raw, dict):
                brand = brand_raw.get("name", "")
            else:
                brand = item.get("brand_name") or item.get("brandName") or str(brand_raw or "")

            model_number = (
                item.get("style_code")
                or item.get("styleCode")
                or item.get("model_number")
                or ""
            )

            volume = item.get("trading_volume") or item.get("trade_count") or item.get("total_sales") or 0

            image_url = ""
            img = item.get("image") or item.get("thumbnail") or item.get("image_url")
            if isinstance(img, dict):
                image_url = img.get("url") or img.get("path") or ""
            elif isinstance(img, str):
                image_url = img

            results.append({
                "product_id": product_id,
                "name": str(name).strip(),
                "brand": str(brand).strip(),
                "model_number": str(model_number).strip(),
                "category": category,
                "buy_now_price": int(item.get("market", {}).get("buy_now", 0) or 0) if isinstance(item.get("market"), dict) else 0,
                "sell_now_price": int(item.get("market", {}).get("sell_now", 0) or 0) if isinstance(item.get("market"), dict) else 0,
                "trading_volume": int(volume) if volume else 0,
                "image_url": image_url,
                "url": f"{KREAM_BASE}/products/{product_id}",
            })

        return results
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/kream_realtime/__init__.py src/kream_realtime/collector.py tests/test_kream_realtime.py
git commit -m "feat: add KreamCollector — auto-collect new products by category"
```

---

## Task 4: 거래량 기반 우선순위 시세 갱신 (`price_refresher.py`)

**Files:**
- Create: `src/kream_realtime/price_refresher.py`
- Modify: `src/models/database.py` (새 쿼리 메서드)
- Test: `tests/test_kream_realtime.py` (추가)

- [ ] **Step 1: Write failing tests**

`tests/test_kream_realtime.py`에 추가:

```python
@pytest.mark.asyncio
async def test_refresher_picks_hot_first():
    """hot tier 상품이 cold보다 먼저 선택되는지 확인."""
    from src.kream_realtime.price_refresher import KreamPriceRefresher

    db = await _create_db()

    # hot 상품 (volume_7d >= 5, 30분 전 갱신)
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier, last_price_refresh)
        VALUES ('hot1', 'Hot Shoe', 'HOT-001', 20, 'hot',
                datetime('now', '-31 minutes'))"""
    )
    # cold 상품 (volume_7d < 5, 6시간 전 갱신)
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier, last_price_refresh)
        VALUES ('cold1', 'Cold Shoe', 'COLD-001', 2, 'cold',
                datetime('now', '-7 hours'))"""
    )
    # 최근 갱신된 hot → 스킵
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier, last_price_refresh)
        VALUES ('hot2', 'Recent Hot', 'HOT-002', 30, 'hot',
                datetime('now', '-5 minutes'))"""
    )
    await db.commit()

    refresher = KreamPriceRefresher(db)
    queue = await refresher.build_refresh_queue(batch_size=10)

    # hot1이 먼저, cold1도 포함, hot2는 최근이라 제외
    pids = [row["product_id"] for row in queue]
    assert "hot1" in pids
    assert "cold1" in pids
    assert "hot2" not in pids
    # hot이 cold보다 앞
    assert pids.index("hot1") < pids.index("cold1")

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py::test_refresher_picks_hot_first -v`
Expected: FAIL

- [ ] **Step 3: Implement price_refresher.py**

```python
# src/kream_realtime/price_refresher.py
"""거래량 기반 우선순위 시세 갱신.

hot tier (7일 거래량 >= 5): 30분마다 시세 갱신
cold tier (7일 거래량 < 5): 6시간마다 시세 갱신
우선순위 큐: hot 먼저, 같은 tier 내에서는 마지막 갱신이 오래된 순.
"""

from datetime import datetime

import aiosqlite

from src.crawlers.kream import kream_crawler
from src.config import settings
from src.utils.logging import setup_logger

logger = setup_logger("kream_price_refresher")


class KreamPriceRefresher:
    """크림 시세 우선순위 갱신기."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def build_refresh_queue(self, batch_size: int | None = None) -> list:
        """갱신이 필요한 상품을 우선순위 큐로 반환.

        1순위: hot tier + last_price_refresh가 30분 이상 지난 상품
        2순위: cold tier + last_price_refresh가 6시간 이상 지난 상품
        정렬: hot 먼저, 같은 tier 내에서는 last_price_refresh ASC (오래된 순)
        """
        if batch_size is None:
            batch_size = settings.realtime_refresh_batch_size

        hot_minutes = settings.realtime_hot_refresh_minutes
        cold_hours = settings.realtime_cold_refresh_hours

        cursor = await self.db.execute(
            """SELECT product_id, name, model_number, volume_7d, refresh_tier, last_price_refresh
            FROM kream_products
            WHERE model_number != ''
            AND (
                (refresh_tier = 'hot' AND (
                    last_price_refresh IS NULL
                    OR last_price_refresh < datetime('now', ?)
                ))
                OR
                (refresh_tier = 'cold' AND (
                    last_price_refresh IS NULL
                    OR last_price_refresh < datetime('now', ?)
                ))
            )
            ORDER BY
                CASE refresh_tier WHEN 'hot' THEN 0 ELSE 1 END,
                last_price_refresh ASC NULLS FIRST
            LIMIT ?""",
            (f"-{hot_minutes} minutes", f"-{cold_hours} hours", batch_size),
        )
        return await cursor.fetchall()

    async def refresh_product(self, product_id: str) -> bool:
        """단일 상품 시세 + 거래량 갱신. 성공 여부 반환."""
        try:
            detail = await kream_crawler.get_product_detail_with_prices(product_id)
            if not detail:
                logger.warning("시세 갱신 실패 (상세 없음): %s", product_id)
                return False

            # 가격 이력 저장
            if detail.size_prices:
                for sp in detail.size_prices:
                    await self.db.execute(
                        """INSERT INTO kream_price_history
                        (product_id, size, sell_now_price, buy_now_price, bid_count, last_sale_price)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            product_id,
                            sp.size,
                            sp.sell_now_price,
                            sp.buy_now_price,
                            sp.bid_count or 0,
                            sp.last_sale_price,
                        ),
                    )

            # 거래량 + 갱신 시간 업데이트
            new_volume = detail.volume_7d or 0
            new_tier = "hot" if new_volume >= settings.realtime_hot_volume_min else "cold"

            await self.db.execute(
                """UPDATE kream_products SET
                    volume_7d = ?,
                    volume_30d = ?,
                    refresh_tier = ?,
                    last_price_refresh = CURRENT_TIMESTAMP,
                    last_volume_check = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ?""",
                (new_volume, detail.volume_30d or 0, new_tier, product_id),
            )
            await self.db.commit()

            logger.debug("시세 갱신: %s (vol=%d, tier=%s)", product_id, new_volume, new_tier)
            return True

        except Exception as e:
            logger.error("시세 갱신 에러 (%s): %s", product_id, e)
            return False

    async def update_product_tier(self, product_id: str, new_volume_7d: int) -> None:
        """상품의 tier를 거래량 기준으로 업데이트."""
        new_tier = "hot" if new_volume_7d >= settings.realtime_hot_volume_min else "cold"
        await self.db.execute(
            "UPDATE kream_products SET volume_7d = ?, refresh_tier = ? WHERE product_id = ?",
            (new_volume_7d, new_tier, product_id),
        )
        await self.db.commit()

    async def run(self) -> dict:
        """배치 시세 갱신 실행.

        Returns:
            {"refreshed": int, "failed": int, "queue_size": int, "elapsed": float}
        """
        started = datetime.now()
        queue = await self.build_refresh_queue()
        queue_size = len(queue)

        refreshed = 0
        failed = 0

        for row in queue:
            success = await self.refresh_product(row["product_id"])
            if success:
                refreshed += 1
            else:
                failed += 1

        elapsed = (datetime.now() - started).total_seconds()
        logger.info(
            "시세 갱신 완료: %d/%d 성공 (큐 %d, %.0f초)",
            refreshed, queue_size, queue_size, elapsed,
        )

        return {
            "refreshed": refreshed,
            "failed": failed,
            "queue_size": queue_size,
            "elapsed": elapsed,
        }
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/kream_realtime/price_refresher.py src/models/database.py tests/test_kream_realtime.py
git commit -m "feat: add KreamPriceRefresher — priority queue based price refresh"
```

---

## Task 5: 거래량 급등 감지 (`volume_spike_detector.py`)

**Files:**
- Create: `src/kream_realtime/volume_spike_detector.py`
- Test: `tests/test_kream_realtime.py` (추가)

- [ ] **Step 1: Write failing tests**

`tests/test_kream_realtime.py`에 추가:

```python
@pytest.mark.asyncio
async def test_spike_detection():
    """이전 스냅샷 대비 2배 이상 증가하면 급등 감지."""
    from src.kream_realtime.volume_spike_detector import VolumeSpikeDetector

    db = await _create_db()

    # 상품 등록
    await db.execute(
        """INSERT INTO kream_products
        (product_id, name, model_number, volume_7d, refresh_tier)
        VALUES ('sp1', 'Spike Shoe', 'SP-001', 5, 'cold')"""
    )
    # 이전 스냅샷: volume_7d=5
    await db.execute(
        """INSERT INTO kream_volume_snapshots
        (product_id, volume_7d, volume_30d, snapshot_at)
        VALUES ('sp1', 5, 20, datetime('now', '-2 hours'))"""
    )
    await db.commit()

    detector = VolumeSpikeDetector(db)

    # 현재 거래량 12 → 5의 2.4배 → 급등
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

    # 승격 처리
    await detector.promote_spiked_products(spikes)

    cursor = await db.execute("SELECT refresh_tier FROM kream_products WHERE product_id = 'pr1'")
    row = await cursor.fetchone()
    assert row["refresh_tier"] == "hot"

    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py::test_spike_detection -v`
Expected: FAIL

- [ ] **Step 3: Implement volume_spike_detector.py**

```python
# src/kream_realtime/volume_spike_detector.py
"""거래량 급등 감지.

직전 스냅샷 대비 거래량이 threshold 배 이상 증가한 상품을 감지하고
해당 상품의 refresh_tier를 hot으로 승격한다.
"""

from datetime import datetime

import aiosqlite

from src.config import settings
from src.crawlers.kream import kream_crawler
from src.utils.logging import setup_logger

logger = setup_logger("kream_volume_spike")


class VolumeSpikeDetector:
    """거래량 급등 감지기."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def detect_spikes(
        self,
        current_volumes: list[dict],
        threshold: float | None = None,
    ) -> list[dict]:
        """현재 거래량과 직전 스냅샷을 비교하여 급등 상품 반환.

        Args:
            current_volumes: [{"product_id": str, "volume_7d": int, "volume_30d": int}]
            threshold: 급등 판정 배율 (기본 settings.realtime_spike_threshold)

        Returns:
            [{"product_id": str, "prev_volume": int, "curr_volume": int, "ratio": float}]
        """
        if threshold is None:
            threshold = settings.realtime_spike_threshold

        spikes = []

        for vol in current_volumes:
            pid = vol["product_id"]
            curr = vol["volume_7d"]

            # 직전 스냅샷 조회
            cursor = await self.db.execute(
                """SELECT volume_7d FROM kream_volume_snapshots
                WHERE product_id = ?
                ORDER BY snapshot_at DESC LIMIT 1""",
                (pid,),
            )
            prev_row = await cursor.fetchone()

            if not prev_row:
                # 스냅샷 없음 → 급등 판정 불가, 스냅샷만 저장
                continue

            prev = prev_row["volume_7d"]

            # 이전 거래량이 0이면 현재가 3건 이상이면 급등
            if prev == 0:
                if curr >= 3:
                    spikes.append({
                        "product_id": pid,
                        "prev_volume": prev,
                        "curr_volume": curr,
                        "ratio": float("inf"),
                    })
                continue

            ratio = curr / prev
            if ratio >= threshold:
                spikes.append({
                    "product_id": pid,
                    "prev_volume": prev,
                    "curr_volume": curr,
                    "ratio": round(ratio, 2),
                })

        if spikes:
            logger.info("거래량 급등 감지: %d건", len(spikes))
            for s in spikes:
                logger.info(
                    "  %s: %d → %d (x%.1f)",
                    s["product_id"], s["prev_volume"], s["curr_volume"], s["ratio"],
                )

        return spikes

    async def save_snapshots(self, volumes: list[dict]) -> None:
        """현재 거래량을 스냅샷으로 저장."""
        for vol in volumes:
            await self.db.execute(
                """INSERT INTO kream_volume_snapshots (product_id, volume_7d, volume_30d)
                VALUES (?, ?, ?)""",
                (vol["product_id"], vol.get("volume_7d", 0), vol.get("volume_30d", 0)),
            )
        await self.db.commit()

    async def promote_spiked_products(self, spikes: list[dict]) -> None:
        """급등 상품을 hot tier로 승격."""
        for spike in spikes:
            await self.db.execute(
                """UPDATE kream_products SET
                    refresh_tier = 'hot',
                    volume_7d = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ?""",
                (spike["curr_volume"], spike["product_id"]),
            )
        await self.db.commit()

        if spikes:
            logger.info("%d개 상품 hot tier 승격", len(spikes))

    async def collect_current_volumes(self, batch_size: int = 50) -> list[dict]:
        """DB에서 거래량 체크가 필요한 상품을 선별하여 크림 API로 현재 거래량 수집.

        last_volume_check가 오래된 순으로 batch_size개.
        """
        cursor = await self.db.execute(
            """SELECT product_id, model_number FROM kream_products
            WHERE model_number != ''
            AND (last_volume_check IS NULL
                 OR last_volume_check < datetime('now', ?))
            ORDER BY last_volume_check ASC NULLS FIRST
            LIMIT ?""",
            (f"-{settings.realtime_volume_check_minutes} minutes", batch_size),
        )
        rows = await cursor.fetchall()

        volumes = []
        for row in rows:
            pid = row["product_id"]
            trade = await kream_crawler.get_trade_history(pid)

            volumes.append({
                "product_id": pid,
                "volume_7d": trade.get("volume_7d", 0),
                "volume_30d": trade.get("volume_30d", 0),
            })

            # last_volume_check 갱신
            await self.db.execute(
                "UPDATE kream_products SET last_volume_check = CURRENT_TIMESTAMP WHERE product_id = ?",
                (pid,),
            )

        await self.db.commit()
        return volumes

    async def run(self) -> dict:
        """급등 감지 전체 사이클.

        1. 거래량 체크 필요 상품 수집
        2. 급등 감지
        3. 스냅샷 저장
        4. 급등 상품 hot 승격

        Returns:
            {"checked": int, "spikes": int, "promoted": list[str], "elapsed": float}
        """
        started = datetime.now()

        # 1. 현재 거래량 수집
        volumes = await self.collect_current_volumes()

        if not volumes:
            return {"checked": 0, "spikes": 0, "promoted": [], "elapsed": 0}

        # 2. 급등 감지
        spikes = await self.detect_spikes(volumes)

        # 3. 스냅샷 저장
        await self.save_snapshots(volumes)

        # 4. 급등 상품 승격
        promoted = []
        if spikes:
            await self.promote_spiked_products(spikes)
            promoted = [s["product_id"] for s in spikes]

        elapsed = (datetime.now() - started).total_seconds()

        return {
            "checked": len(volumes),
            "spikes": len(spikes),
            "promoted": promoted,
            "elapsed": elapsed,
        }
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/kream_realtime/volume_spike_detector.py tests/test_kream_realtime.py
git commit -m "feat: add VolumeSpikeDetector — detect volume spikes and promote to hot tier"
```

---

## Task 6: 스케줄러 통합

**Files:**
- Modify: `src/scheduler.py`
- Modify: `src/discord_bot/bot.py` (초기화)

- [ ] **Step 1: Write failing test for scheduler integration**

`tests/test_kream_realtime.py`에 추가:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py::test_scheduler_has_realtime_loops -v`
Expected: FAIL

- [ ] **Step 3: Add 3 new loops to scheduler.py**

`src/scheduler.py`에 추가 (기존 `daily_report` 아래):

```python
# ─── 신규 상품 자동 수집 (6시간 주기) ─────────────
@tasks.loop(hours=settings.realtime_collect_interval_hours)
async def collect_loop(self) -> None:
    """크림 신규 상품 자동 수집."""
    if not hasattr(self.bot, '_kream_collector') or not self.bot._kream_collector:
        return

    try:
        result = await self.bot._kream_collector.run()
        if result["total_new"] > 0:
            await self.bot.log_to_channel(
                f"📦 신규 상품 수집: +{result['total_new']}건 ({result['elapsed']:.0f}초)"
            )
        logger.info("신규 수집: %d건 (%.0f초)", result["total_new"], result["elapsed"])
    except Exception as e:
        error_aggregator.add("collect_loop", e)
        logger.error("신규 수집 실패: %s", e)

@collect_loop.before_loop
async def before_collect(self) -> None:
    await self.bot.wait_until_ready()
    await asyncio.sleep(300)  # 봇 시작 5분 후

# ─── 우선순위 시세 갱신 (10분 주기 — 배치 내에서 hot/cold 구분) ───
@tasks.loop(minutes=10)
async def refresh_loop(self) -> None:
    """거래량 기반 우선순위 시세 갱신."""
    if not hasattr(self.bot, '_kream_refresher') or not self.bot._kream_refresher:
        return

    try:
        result = await self.bot._kream_refresher.run()
        if result["refreshed"] > 0:
            logger.info(
                "시세 갱신: %d/%d (%.0f초)",
                result["refreshed"], result["queue_size"], result["elapsed"],
            )
    except Exception as e:
        error_aggregator.add("refresh_loop", e)
        logger.error("시세 갱신 실패: %s", e)

@refresh_loop.before_loop
async def before_refresh(self) -> None:
    await self.bot.wait_until_ready()
    await asyncio.sleep(180)  # 봇 시작 3분 후

# ─── 거래량 급등 감지 (60분 주기) ──────────────
@tasks.loop(minutes=settings.realtime_volume_check_minutes)
async def spike_loop(self) -> None:
    """거래량 급등 감지 + hot 승격."""
    if not hasattr(self.bot, '_kream_spike_detector') or not self.bot._kream_spike_detector:
        return

    try:
        result = await self.bot._kream_spike_detector.run()
        if result["spikes"] > 0:
            await self.bot.log_to_channel(
                f"🔥 거래량 급등: {result['spikes']}건 감지 "
                f"(승격: {', '.join(result['promoted'][:5])})"
            )
        logger.info(
            "급등 감지: %d건 체크, %d건 급등 (%.0f초)",
            result["checked"], result["spikes"], result["elapsed"],
        )
    except Exception as e:
        error_aggregator.add("spike_loop", e)
        logger.error("급등 감지 실패: %s", e)

@spike_loop.before_loop
async def before_spike(self) -> None:
    await self.bot.wait_until_ready()
    await asyncio.sleep(600)  # 봇 시작 10분 후
```

`start()` 메서드에 추가:
```python
if not self.collect_loop.is_running():
    self.collect_loop.start()
if not self.refresh_loop.is_running():
    self.refresh_loop.start()
if not self.spike_loop.is_running():
    self.spike_loop.start()
```

`stop()` 메서드에 추가:
```python
self.collect_loop.cancel()
self.refresh_loop.cancel()
self.spike_loop.cancel()
```

- [ ] **Step 4: Add initialization to bot.py**

`src/discord_bot/bot.py`의 봇 초기화 부분에 추가 (DB 연결 후):

```python
from src.kream_realtime.collector import KreamCollector
from src.kream_realtime.price_refresher import KreamPriceRefresher
from src.kream_realtime.volume_spike_detector import VolumeSpikeDetector

# DB 연결 후
self._kream_collector = KreamCollector(self.scanner.db.db)
self._kream_refresher = KreamPriceRefresher(self.scanner.db.db)
self._kream_spike_detector = VolumeSpikeDetector(self.scanner.db.db)
```

- [ ] **Step 5: Run tests**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/test_kream_realtime.py -v`
Expected: ALL PASS

- [ ] **Step 6: Import/syntax verification**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && python3 -c "import ast; ast.parse(open('src/scheduler.py').read()); ast.parse(open('src/kream_realtime/collector.py').read()); ast.parse(open('src/kream_realtime/price_refresher.py').read()); ast.parse(open('src/kream_realtime/volume_spike_detector.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add src/scheduler.py src/discord_bot/bot.py
git commit -m "feat: integrate realtime DB loops into scheduler — collect/refresh/spike"
```

---

## Task 7: 전체 검증 + CLAUDE.md 업데이트

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Run verify.py**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && PYTHONPATH=. python scripts/verify.py`
Expected: PASS

- [ ] **Step 2: Run full pytest**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: Run ruff**

Run: `cd /mnt/c/Users/USER/Desktop/크림봇 && ruff check src/kream_realtime/ tests/test_kream_realtime.py`
Expected: No errors (또는 자동 수정)

- [ ] **Step 4: Update CLAUDE.md**

CLAUDE.md `Architecture` 섹션에 추가:

```markdown
### 실시간 DB 레이어

3개 루프가 `scheduler.py`에서 독립 실행:

| 루프 | 주기 | 모듈 | 역할 |
|------|------|------|------|
| `collect_loop` | 6시간 | `src/kream_realtime/collector.py` | 카테고리별 신규 상품 수집 (date순) |
| `refresh_loop` | 10분 | `src/kream_realtime/price_refresher.py` | 우선순위 큐 시세 갱신 (hot 30분/cold 6시간) |
| `spike_loop` | 60분 | `src/kream_realtime/volume_spike_detector.py` | 거래량 급등 감지 + hot 승격 |

**Tier 분류:**
- `hot`: 7일 거래량 >= 5 → 30분마다 시세 갱신
- `cold`: 7일 거래량 < 5 → 6시간마다 시세 갱신
- 급등 감지 시 cold → hot 자동 승격

**DB 확장:**
- `kream_products`: +`volume_7d`, `volume_30d`, `last_volume_check`, `refresh_tier`, `last_price_refresh`
- `kream_volume_snapshots`: 거래량 시계열 스냅샷 (급등 비교용)

**API (GET 전용):**
- `/api/p/e/search/products` (keyword, sort=date, page, per_page) — 신규 수집
- `/api/p/e/products/{id}` — 상세
- `/api/p/options/display?product_id={id}` — 사이즈별 시세
- `/api/p/e/products/{id}/sales` — 거래 내역
```

- [ ] **Step 5: Final commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with realtime DB layer architecture"
```
