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

            cursor = await self.db.execute(
                "SELECT product_id FROM kream_products WHERE product_id = ?", (pid,)
            )
            exists = await cursor.fetchone()

            volume = int(p.get("trading_volume", 0) or 0)

            if exists:
                await self.db.execute(
                    """UPDATE kream_products SET
                        name = ?, brand = ?, volume_7d = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE product_id = ?""",
                    (p.get("name", ""), p.get("brand", ""), volume, pid),
                )
            else:
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

    async def collect_category(self, category_name: str, keyword: str, max_pages: int = 5) -> int:
        """단일 카테고리+키워드 수집. 신규 건수 반환."""
        total_new = 0
        empty_streak = 0

        for page in range(1, max_pages + 1):
            await _random_delay()

            url = f"{KREAM_BASE}/search?keyword={keyword}&tab=products&sort=date&page={page}"
            html = await kream_crawler._request("GET", url, parse_json=False, max_retries=2)
            if not html:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue

            data = kream_crawler._extract_page_data(html)
            if not data:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue

            raw_products = kream_crawler._extract_listing_products(data)
            products = self._enrich_listing_products(raw_products, category_name)
            if not products:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue

            empty_streak = 0
            new_count = await self.save_products(products)
            total_new += new_count

            logger.info("%s [%s] page %d: %d건 (신규 %d)", category_name, keyword, page, len(products), new_count)

            if new_count == 0:
                break

        return total_new

    async def run(self, max_pages_per_keyword: int | None = None) -> dict:
        """전체 카테고리 수집 실행."""
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
    def _enrich_listing_products(raw_products: list[dict], category: str) -> list[dict]:
        """_extract_listing_products 결과에 category 추가 및 키 정규화."""
        results = []
        for p in raw_products:
            pid = str(p.get("product_id") or p.get("id") or "")
            if not pid or pid == "None":
                continue
            results.append({
                "product_id": pid,
                "name": str(p.get("name", "")).strip(),
                "brand": str(p.get("brand", "")).strip(),
                "model_number": str(p.get("model_number", "")).strip(),
                "category": category,
                "image_url": p.get("image_url", ""),
                "url": p.get("url", f"{KREAM_BASE}/products/{pid}"),
                "trading_volume": int(p.get("trading_volume", 0) or 0),
            })
        return results

    @staticmethod
    def _parse_search_response(data: dict, category: str) -> list[dict]:
        """검색 API 응답 파싱."""
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
                item.get("style_code") or item.get("styleCode") or item.get("model_number") or ""
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
