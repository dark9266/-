"""kream_db.json → SQLite 임포트 스크립트."""

import asyncio
import json
import time
from collections import Counter
from pathlib import Path

from src.models.database import Database

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JSON_PATH = DATA_DIR / "kream_db.json"
DB_PATH = DATA_DIR / "kream_bot.db"


async def main():
    print(f"[1] JSON 로드: {JSON_PATH}")
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    products = data["products"]
    total = len(products)
    print(f"    총 {total:,}개 상품 로드 완료")

    db = Database(str(DB_PATH))
    await db.connect()
    print(f"[2] DB 연결: {DB_PATH}")

    brand_counter: Counter = Counter()
    category_counter: Counter = Counter()
    start = time.time()

    for i, p in enumerate(products, 1):
        await db.upsert_kream_product(
            product_id=str(p["product_id"]),
            name=p.get("name", ""),
            model_number=p.get("model_number", ""),
            brand=p.get("brand", ""),
            category=p.get("category", "sneakers"),
            image_url=p.get("image_url", ""),
            url=p.get("url", ""),
        )
        brand_counter[p.get("brand", "(없음)")] += 1
        category_counter[p.get("category", "(없음)")] += 1

        if i % 1000 == 0:
            elapsed = time.time() - start
            print(f"    ... {i:,}/{total:,} ({i*100//total}%) - {elapsed:.1f}s")

    elapsed = time.time() - start
    await db.close()

    print(f"\n[3] 임포트 완료! ({elapsed:.1f}초)")
    print(f"    총 건수: {total:,}")
    print(f"\n[4] 브랜드 TOP 10:")
    for rank, (brand, cnt) in enumerate(brand_counter.most_common(10), 1):
        print(f"    {rank:>2}. {brand:<25} {cnt:>6,}개")
    print(f"\n[5] 카테고리별 건수:")
    for cat, cnt in category_counter.most_common():
        print(f"    - {cat:<20} {cnt:>6,}개")


if __name__ == "__main__":
    asyncio.run(main())
