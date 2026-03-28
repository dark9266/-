"""SQLite 데이터베이스 매니저."""

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

    async def search_kream_by_model_like(self, model_number: str) -> aiosqlite.Row | None:
        """모델번호 유연 검색 (슬래시 구분 모델번호 등 대응).

        kream_db.json의 모델번호가 '315122-111/CW2288-111' 형태일 때,
        'CW2288-111'로 검색해도 찾을 수 있도록 LIKE 검색을 수행한다.
        """
        if not model_number:
            return None
        cursor = await self.db.execute(
            "SELECT * FROM kream_products WHERE model_number LIKE ? LIMIT 1",
            (f"%{model_number}%",),
        )
        return await cursor.fetchone()

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
