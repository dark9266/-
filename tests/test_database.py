"""데이터베이스 단위 테스트."""

import asyncio
import os
import tempfile

import pytest

from src.models.database import Database


@pytest.fixture
async def db():
    """테스트용 임시 DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    database = Database(db_path=db_path)
    await database.connect()
    yield database
    await database.close()
    os.unlink(db_path)


class TestDatabase:
    async def test_connect(self, db: Database):
        assert db.db is not None

    async def test_upsert_kream_product(self, db: Database):
        await db.upsert_kream_product(
            product_id="12345",
            name="나이키 덩크 로우",
            model_number="DQ8423-100",
            brand="Nike",
        )
        row = await db.find_kream_by_model("DQ8423-100")
        assert row is not None
        assert row["product_id"] == "12345"
        assert row["name"] == "나이키 덩크 로우"

    async def test_save_and_get_prices(self, db: Database):
        await db.upsert_kream_product("12345", "Test", "XX-100")
        await db.save_kream_prices(
            "12345",
            [
                {"size": "260", "sell_now_price": 130000, "buy_now_price": 140000},
                {"size": "270", "sell_now_price": 120000, "buy_now_price": 125000},
            ],
        )
        prices = await db.get_latest_kream_prices("12345")
        assert len(prices) == 2

    async def test_keywords(self, db: Database):
        assert await db.add_keyword("덩크") is True
        assert await db.add_keyword("덩크") is False  # 중복

        keywords = await db.get_active_keywords()
        assert "덩크" in keywords

        assert await db.remove_keyword("덩크") is True
        keywords = await db.get_active_keywords()
        assert "덩크" not in keywords

    async def test_alert_history(self, db: Database):
        await db.save_alert("12345", "profit", best_profit=32000, signal="강력매수")
        alert = await db.get_recent_alert("12345", "profit", hours=1)
        assert alert is not None
        assert alert["best_profit"] == 32000
