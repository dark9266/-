"""SQLite 데이터베이스 매니저."""

import re

import aiosqlite

from src.config import settings
from src.utils.logging import setup_logger

logger = setup_logger("database")

SCHEMA_SQL = """
-- 크림 상품 마스터
CREATE TABLE IF NOT EXISTS kream_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_number TEXT NOT NULL,
    brand TEXT DEFAULT '',
    category TEXT DEFAULT 'sneakers',
    image_url TEXT DEFAULT '',
    url TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kream_model ON kream_products(model_number);

-- 크림 사이즈별 가격 이력
CREATE TABLE IF NOT EXISTS kream_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    size TEXT NOT NULL,
    sell_now_price INTEGER,
    buy_now_price INTEGER,
    bid_count INTEGER DEFAULT 0,
    last_sale_price INTEGER,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES kream_products(product_id)
);
CREATE INDEX IF NOT EXISTS idx_kream_price_product ON kream_price_history(product_id, size);
CREATE INDEX IF NOT EXISTS idx_kream_price_time ON kream_price_history(fetched_at);

-- 크림 거래량 이력
CREATE TABLE IF NOT EXISTS kream_volume_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    volume_7d INTEGER DEFAULT 0,
    volume_30d INTEGER DEFAULT 0,
    last_trade_date TIMESTAMP,
    price_trend TEXT DEFAULT '',
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES kream_products(product_id)
);

-- 리테일 상품
CREATE TABLE IF NOT EXISTS retail_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    model_number TEXT NOT NULL,
    brand TEXT DEFAULT '',
    url TEXT DEFAULT '',
    image_url TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, product_id)
);
CREATE INDEX IF NOT EXISTS idx_retail_model ON retail_products(model_number);

-- 리테일 사이즈별 가격 이력
CREATE TABLE IF NOT EXISTS retail_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    product_id TEXT NOT NULL,
    size TEXT NOT NULL,
    price INTEGER NOT NULL,
    original_price INTEGER DEFAULT 0,
    in_stock INTEGER DEFAULT 1,
    discount_type TEXT DEFAULT '',
    discount_rate REAL DEFAULT 0.0,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_retail_price_product ON retail_price_history(source, product_id, size);

-- 알림 기록 (중복 알림 방지)
CREATE TABLE IF NOT EXISTS alert_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kream_product_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,  -- 'profit', 'price_change', 'daily_report'
    best_profit INTEGER DEFAULT 0,
    signal TEXT DEFAULT '',
    message_id TEXT DEFAULT '',  -- Discord 메시지 ID
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_alert_product ON alert_history(kream_product_id, alert_type);
CREATE INDEX IF NOT EXISTS idx_alert_time ON alert_history(created_at);

-- 모니터링 키워드
CREATE TABLE IF NOT EXISTS monitor_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 설정 저장 (런타임 설정 변경용)
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 카테고리 스캔 이력 (재방문 방지)
CREATE TABLE IF NOT EXISTS category_scan_history (
    goods_no TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    brand TEXT DEFAULT '',
    goods_name TEXT DEFAULT '',
    model_number TEXT DEFAULT '',
    kream_matched INTEGER DEFAULT 0,
    kream_product_id TEXT DEFAULT '',
    price INTEGER DEFAULT 0,
    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cat_scan_category ON category_scan_history(category);

-- 카테고리 스캔 진행 상황
CREATE TABLE IF NOT EXISTS category_scan_progress (
    category TEXT PRIMARY KEY,
    last_scanned_page INTEGER DEFAULT 0,
    total_items_scanned INTEGER DEFAULT 0,
    last_scan_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    """비동기 SQLite 데이터베이스 매니저."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """DB 연결 및 스키마 초기화."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info("데이터베이스 연결 완료: %s", self.db_path)

    async def close(self) -> None:
        """DB 연결 종료."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("데이터베이스 연결 종료")

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("데이터베이스가 연결되지 않았습니다. connect()를 먼저 호출하세요.")
        return self._db

    # -- 크림 상품 --

    async def upsert_kream_product(
        self,
        product_id: str,
        name: str,
        model_number: str,
        brand: str = "",
        category: str = "sneakers",
        image_url: str = "",
        url: str = "",
    ) -> None:
        await self.db.execute(
            """INSERT INTO kream_products (product_id, name, model_number, brand, category, image_url, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                name=excluded.name, model_number=excluded.model_number,
                brand=excluded.brand, image_url=excluded.image_url,
                url=excluded.url, updated_at=CURRENT_TIMESTAMP""",
            (product_id, name, model_number, brand, category, image_url, url),
        )
        await self.db.commit()

    async def save_kream_prices(
        self, product_id: str, size_prices: list[dict]
    ) -> None:
        """사이즈별 가격 이력 저장."""
        for sp in size_prices:
            await self.db.execute(
                """INSERT INTO kream_price_history
                (product_id, size, sell_now_price, buy_now_price, bid_count, last_sale_price)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    product_id,
                    sp["size"],
                    sp.get("sell_now_price"),
                    sp.get("buy_now_price"),
                    sp.get("bid_count", 0),
                    sp.get("last_sale_price"),
                ),
            )
        await self.db.commit()

    async def get_latest_kream_prices(self, product_id: str) -> list[aiosqlite.Row]:
        """상품의 최신 사이즈별 가격 조회."""
        cursor = await self.db.execute(
            """SELECT * FROM kream_price_history
            WHERE product_id = ?
            AND fetched_at = (SELECT MAX(fetched_at) FROM kream_price_history WHERE product_id = ?)
            ORDER BY size""",
            (product_id, product_id),
        )
        return await cursor.fetchall()

    async def find_kream_by_model(self, model_number: str) -> aiosqlite.Row | None:
        """모델번호로 크림 상품 검색."""
        cursor = await self.db.execute(
            "SELECT * FROM kream_products WHERE model_number = ?", (model_number,)
        )
        return await cursor.fetchone()

    async def find_kream_all_by_model(self, model_number: str) -> list[aiosqlite.Row]:
        """모델번호로 크림 상품 전체 검색 (복수 매칭 대응)."""
        cursor = await self.db.execute(
            "SELECT * FROM kream_products WHERE model_number = ?", (model_number,)
        )
        return await cursor.fetchall()

    async def search_kream_by_model_like(self, model_number: str) -> aiosqlite.Row | None:
        """모델번호 유연 검색 (슬래시 구분 모델번호만 대응).

        kream_db.json의 모델번호가 '315122-111/CW2288-111' 형태일 때,
        'CW2288-111'로 검색해도 찾을 수 있도록 슬래시 기준 LIKE 검색.
        단순 부분 문자열 매칭은 차단 (25-002 → CU9225-002 오매칭 방지).
        """
        if not model_number or len(model_number) < 6:
            return None
        cursor = await self.db.execute(
            "SELECT * FROM kream_products "
            "WHERE model_number LIKE ? OR model_number LIKE ? LIMIT 1",
            (f"%/{model_number}", f"{model_number}/%"),
        )
        return await cursor.fetchone()

    async def search_kream_all_by_model_like(self, model_number: str) -> list[aiosqlite.Row]:
        """모델번호 유연 검색 — 슬래시 구분만 허용."""
        if not model_number or len(model_number) < 6:
            return []
        cursor = await self.db.execute(
            "SELECT * FROM kream_products "
            "WHERE model_number LIKE ? OR model_number LIKE ?",
            (f"%/{model_number}", f"{model_number}/%"),
        )
        return await cursor.fetchall()

    # -- 리테일 상품 --

    async def upsert_retail_product(
        self,
        source: str,
        product_id: str,
        name: str,
        model_number: str,
        brand: str = "",
        url: str = "",
        image_url: str = "",
    ) -> None:
        await self.db.execute(
            """INSERT INTO retail_products (source, product_id, name, model_number, brand, url, image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, product_id) DO UPDATE SET
                name=excluded.name, model_number=excluded.model_number,
                brand=excluded.brand, url=excluded.url,
                image_url=excluded.image_url, updated_at=CURRENT_TIMESTAMP""",
            (source, product_id, name, model_number, brand, url, image_url),
        )
        await self.db.commit()

    # -- 배치스캔용 조회 --

    async def get_kream_product_count(self) -> int:
        """크림 상품 총 수 (모델번호 있는 것만)."""
        cursor = await self.db.execute(
            "SELECT COUNT(*) as cnt FROM kream_products WHERE model_number != ''"
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def get_kream_products_batch(self, offset: int, limit: int) -> list:
        """크림 상품 배치 조회."""
        cursor = await self.db.execute(
            """SELECT product_id, name, model_number, brand, category, image_url, url
            FROM kream_products WHERE model_number != ''
            ORDER BY product_id LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        return await cursor.fetchall()

    async def find_retail_by_model(self, model_number: str) -> list:
        """모델번호로 리테일 매칭 상품 조회."""
        if not model_number:
            return []
        cursor = await self.db.execute(
            "SELECT * FROM retail_products WHERE model_number = ?",
            (model_number,),
        )
        return await cursor.fetchall()

    async def get_matched_kream_products(self, limit: int = 200) -> list:
        """리테일 매칭이 있는 크림 상품 조회 (자동스캔용)."""
        cursor = await self.db.execute(
            """SELECT DISTINCT kp.product_id, kp.name, kp.model_number, kp.brand,
               kp.category, kp.image_url, kp.url
            FROM kream_products kp
            INNER JOIN retail_products rp ON kp.model_number = rp.model_number
            ORDER BY kp.updated_at DESC LIMIT ?""",
            (limit,),
        )
        return await cursor.fetchall()

    async def get_matched_count(self) -> int:
        """리테일 매칭이 있는 크림 상품 수."""
        cursor = await self.db.execute(
            """SELECT COUNT(DISTINCT kp.product_id) as cnt
            FROM kream_products kp
            INNER JOIN retail_products rp ON kp.model_number = rp.model_number"""
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    # -- 알림 기록 --

    async def save_alert(
        self,
        kream_product_id: str,
        alert_type: str,
        best_profit: int = 0,
        signal: str = "",
        message_id: str = "",
    ) -> None:
        await self.db.execute(
            """INSERT INTO alert_history (kream_product_id, alert_type, best_profit, signal, message_id)
            VALUES (?, ?, ?, ?, ?)""",
            (kream_product_id, alert_type, best_profit, signal, message_id),
        )
        await self.db.commit()

    async def get_recent_alert(
        self, kream_product_id: str, alert_type: str, hours: int = 1
    ) -> aiosqlite.Row | None:
        """최근 N시간 내 동일 알림이 있는지 확인 (중복 방지)."""
        cursor = await self.db.execute(
            """SELECT * FROM alert_history
            WHERE kream_product_id = ? AND alert_type = ?
            AND created_at > datetime('now', ?)
            ORDER BY created_at DESC LIMIT 1""",
            (kream_product_id, alert_type, f"-{hours} hours"),
        )
        return await cursor.fetchone()

    async def should_send_alert(
        self,
        kream_product_id: str,
        alert_type: str,
        signal: str,
        best_profit: int,
        cooldown_hours: int = 1,
    ) -> bool:
        """알림을 보내야 하는지 판단 (중복 알림 방지 강화).

        조건:
        - 쿨다운 시간 내 동일 상품/타입 알림 없음 → 전송
        - 있더라도 시그널이 업그레이드됨 → 전송
        - 있더라도 수익이 20% 이상 증가함 → 전송
        """
        recent = await self.get_recent_alert(kream_product_id, alert_type, cooldown_hours)
        if not recent:
            return True

        # 시그널 업그레이드 체크
        signal_rank = {"비추천": 0, "관망": 1, "매수": 2, "강력매수": 3}
        old_rank = signal_rank.get(recent["signal"], 0)
        new_rank = signal_rank.get(signal, 0)
        if new_rank > old_rank:
            return True

        # 수익 20% 이상 증가
        old_profit = recent["best_profit"] or 0
        if old_profit > 0 and best_profit > old_profit * 1.2:
            return True

        return False

    # -- 브랜드 --

    async def get_top_brands(self, limit: int = 30) -> list[str]:
        """크림 DB에서 상품 수 기준 상위 브랜드 목록 반환."""
        cursor = await self.db.execute(
            "SELECT brand FROM kream_products WHERE brand != '' "
            "GROUP BY brand ORDER BY COUNT(*) DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [row["brand"] for row in rows]

    async def get_all_kream_brand_slugs(self) -> set[str]:
        """크림 DB의 모든 브랜드를 정규화된 slug 셋으로 반환.

        공백/하이픈/아포스트로피를 제거하여 무신사 brand slug와 비교 가능하게 한다.
        예: "new balance" → "newbalance", "arc'teryx" → "arcteryx"
        """
        cursor = await self.db.execute(
            "SELECT DISTINCT LOWER(brand) FROM kream_products WHERE brand != ''"
        )
        rows = await cursor.fetchall()
        slugs = set()
        for row in rows:
            slug = re.sub(r"[^a-z0-9]", "", row[0])
            if slug:
                slugs.add(slug)
        return slugs

    async def get_brands_min_count(self, min_count: int = 10) -> list[str]:
        """상품 수가 min_count 이상인 모든 브랜드 반환 (상품 수 내림차순)."""
        cursor = await self.db.execute(
            "SELECT brand FROM kream_products WHERE brand != '' "
            "GROUP BY brand HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC",
            (min_count,),
        )
        rows = await cursor.fetchall()
        return [row["brand"] for row in rows]

    # -- 키워드 --

    async def get_active_keywords(self) -> list[str]:
        cursor = await self.db.execute(
            "SELECT keyword FROM monitor_keywords WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        return [row["keyword"] for row in rows]

    async def add_keyword(self, keyword: str) -> bool:
        try:
            await self.db.execute(
                "INSERT INTO monitor_keywords (keyword) VALUES (?)", (keyword,)
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_keyword(self, keyword: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM monitor_keywords WHERE keyword = ?", (keyword,)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    # -- 카테고리 스캔 --

    async def clear_category_scan_history(self) -> None:
        """카테고리 스캔 이력 전체 삭제 (초기화)."""
        await self.db.execute("DELETE FROM category_scan_history")
        await self.db.execute("DELETE FROM category_scan_progress")
        await self.db.commit()

    async def load_scanned_goods_nos(self, category: str = "") -> set[str]:
        """카테고리 스캔 이력에서 goods_no 집합 로딩 (메모리 SET)."""
        if category:
            cursor = await self.db.execute(
                "SELECT goods_no FROM category_scan_history WHERE category = ?",
                (category,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT goods_no FROM category_scan_history"
            )
        rows = await cursor.fetchall()
        return {row["goods_no"] for row in rows}

    async def save_category_scan(
        self,
        goods_no: str,
        category: str,
        brand: str = "",
        goods_name: str = "",
        model_number: str = "",
        kream_matched: bool = False,
        kream_product_id: str = "",
        price: int = 0,
    ) -> None:
        """카테고리 스캔 이력 저장 (UPSERT)."""
        await self.db.execute(
            """INSERT INTO category_scan_history
            (goods_no, category, brand, goods_name, model_number,
             kream_matched, kream_product_id, price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(goods_no) DO UPDATE SET
                model_number=excluded.model_number,
                kream_matched=excluded.kream_matched,
                kream_product_id=excluded.kream_product_id,
                price=excluded.price,
                scanned_at=CURRENT_TIMESTAMP""",
            (goods_no, category, brand, goods_name, model_number,
             1 if kream_matched else 0, kream_product_id, price),
        )
        await self.db.commit()

    async def get_category_progress(self, category: str) -> dict | None:
        """카테고리 스캔 진행 상황 조회."""
        cursor = await self.db.execute(
            "SELECT * FROM category_scan_progress WHERE category = ?",
            (category,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_category_progress(
        self, category: str, page: int, items: int,
    ) -> None:
        """카테고리 스캔 진행 상황 업데이트."""
        await self.db.execute(
            """INSERT INTO category_scan_progress
            (category, last_scanned_page, total_items_scanned)
            VALUES (?, ?, ?)
            ON CONFLICT(category) DO UPDATE SET
                last_scanned_page=excluded.last_scanned_page,
                total_items_scanned=category_scan_progress.total_items_scanned + excluded.total_items_scanned,
                last_scan_at=CURRENT_TIMESTAMP""",
            (category, page, items),
        )
        await self.db.commit()

    async def get_category_scan_stats(self) -> dict:
        """전체 카테고리 스캔 통계."""
        cursor = await self.db.execute(
            """SELECT category, last_scanned_page, total_items_scanned, last_scan_at
            FROM category_scan_progress ORDER BY last_scan_at DESC"""
        )
        rows = await cursor.fetchall()
        stats = {row["category"]: dict(row) for row in rows}

        # 총 스캔 이력 수
        cursor2 = await self.db.execute(
            "SELECT COUNT(*) as cnt FROM category_scan_history"
        )
        total = await cursor2.fetchone()
        stats["_total_scanned"] = total["cnt"] if total else 0

        # 크림 매칭 성공 수
        cursor3 = await self.db.execute(
            "SELECT COUNT(*) as cnt FROM category_scan_history WHERE kream_matched = 1"
        )
        matched = await cursor3.fetchone()
        stats["_total_matched"] = matched["cnt"] if matched else 0

        return stats
