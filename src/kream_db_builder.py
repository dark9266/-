"""크림 전체 상품 DB 구축 모듈.

카테고리별로 크림 검색 API를 페이지 단위로 호출하여
전체 상품을 수집하고 data/kream_db.json에 저장한다.

카테고리: 신발, 상의, 하의, 아우터, 가방, 모자, 액세서리
"""

import asyncio
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable

from src.config import DATA_DIR
from src.crawlers.kream import kream_crawler, KREAM_BASE, _API_HEADERS, _random_delay
from src.utils.logging import setup_logger

logger = setup_logger("kream_db_builder")

DB_PATH = DATA_DIR / "kream_db.json"

# 크림 카테고리 매핑 (한글명 → API 파라미터)
CATEGORIES = {
    "신발": {"category_id": "34", "keywords": ["nike", "jordan", "adidas", "new balance", "asics", "puma", "converse", "vans", "reebok", "hoka", "salomon", "arc'teryx", "on running"]},
    "상의": {"category_id": "35", "keywords": ["nike", "adidas", "stussy", "supreme", "the north face", "arc'teryx"]},
    "하의": {"category_id": "36", "keywords": ["nike", "adidas", "stussy", "supreme", "carhartt"]},
    "아우터": {"category_id": "37", "keywords": ["nike", "the north face", "arc'teryx", "supreme", "adidas", "patagonia"]},
    "가방": {"category_id": "39", "keywords": ["nike", "supreme", "the north face", "louis vuitton", "chanel"]},
    "모자": {"category_id": "41", "keywords": ["nike", "new era", "supreme", "adidas", "stussy"]},
    "액세서리": {"category_id": "167", "keywords": ["nike", "supreme", "chrome hearts", "rolex", "apple"]},
}

# 진행 콜백 타입
ProgressCallback = Callable[[str], Awaitable[None]] | None


async def _fetch_category_page(
    category_name: str,
    keyword: str,
    page: int,
    per_page: int = 40,
) -> list[dict]:
    """카테고리별 검색 API로 한 페이지 수집."""
    session = await kream_crawler._get_session()

    # 검색 페이지 HTML을 통한 수집 (Nuxt 데이터 파싱)
    sort = "popular"
    url = f"{KREAM_BASE}/search?keyword={keyword}&tab=products&sort={sort}&page={page}"
    html = await kream_crawler._request("GET", url, parse_json=False, max_retries=2)
    if not html:
        return []

    data = kream_crawler._extract_page_data(html)
    if not data:
        return []

    products = kream_crawler._extract_listing_products(data)
    results = []
    for p in products:
        if not p.get("product_id"):
            continue
        # model_number 추출 시도
        model_number = p.get("model_number", "")
        results.append({
            "product_id": p["product_id"],
            "name": p.get("name", ""),
            "brand": p.get("brand", ""),
            "model_number": model_number,
            "category": category_name,
            "url": p.get("url", f"{KREAM_BASE}/products/{p['product_id']}"),
            "image_url": p.get("image_url", ""),
        })

    return results


async def _fetch_category_page_api(
    category_name: str,
    keyword: str,
    page: int,
    per_page: int = 40,
) -> list[dict]:
    """search_products로 한 페이지 수집 (폴백)."""
    if page > 1:
        return []  # nuxt 검색은 페이지네이션 미지원 — 1페이지만
    search_results = await kream_crawler.search_products(keyword)
    results = []
    for item in search_results:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "")
        if not product_id:
            continue
        results.append({
            "product_id": product_id,
            "name": str(item.get("name", "")).strip(),
            "brand": str(item.get("brand", "")).strip(),
            "model_number": str(item.get("model_number", "")).strip(),
            "category": category_name,
            "buy_now_price": 0,
            "sell_now_price": 0,
            "trading_volume": 0,
            "image_url": item.get("image_url", ""),
            "url": item.get("url", f"{KREAM_BASE}/products/{product_id}"),
        })
    return results


async def build_kream_db(
    categories: dict | None = None,
    max_pages_per_keyword: int = 50,
    on_progress: ProgressCallback = None,
    test_mode: bool = False,
    test_pages: int = 2,
) -> dict:
    """크림 전체 상품 DB 구축.

    Args:
        categories: 수집할 카테고리 (None이면 전체)
        max_pages_per_keyword: 키워드당 최대 페이지 수
        on_progress: 진행상황 콜백 (디스코드 로그 등)
        test_mode: True면 첫 카테고리 test_pages 페이지만 수집
        test_pages: 테스트 모드 시 페이지 수

    Returns:
        {"total": int, "by_category": dict, "path": str}
    """
    if categories is None:
        categories = CATEGORIES

    all_products: dict[str, dict] = {}  # product_id → product data
    stats = {cat: 0 for cat in categories}
    started_at = datetime.now()

    # 기존 DB 로드 (증분 업데이트)
    if DB_PATH.exists():
        try:
            existing = json.loads(DB_PATH.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and "products" in existing:
                for p in existing["products"]:
                    all_products[p["product_id"]] = p
                logger.info("기존 DB 로드: %d개 상품", len(all_products))
                if on_progress:
                    await on_progress(f"기존 DB 로드: {len(all_products):,}개 상품")
        except Exception as e:
            logger.warning("기존 DB 로드 실패 (새로 생성): %s", e)

    total_categories = len(categories)
    for cat_idx, (cat_name, cat_config) in enumerate(categories.items(), 1):
        keywords = cat_config["keywords"]
        category_count = 0

        if on_progress:
            await on_progress(
                f"📦 **{cat_name}** 카테고리 수집 시작 ({cat_idx}/{total_categories})"
            )

        for kw_idx, keyword in enumerate(keywords):
            pages_to_fetch = test_pages if test_mode else max_pages_per_keyword
            empty_count = 0

            for page in range(1, pages_to_fetch + 1):
                # 1~2초 딜레이 (차단 방지)
                delay = random.uniform(1.0, 2.0)
                await asyncio.sleep(delay)

                # HTML 파싱 우선 시도, 실패 시 API 폴백
                products = await _fetch_category_page(cat_name, keyword, page)
                if not products:
                    products = await _fetch_category_page_api(cat_name, keyword, page)

                if not products:
                    empty_count += 1
                    if empty_count >= 2:
                        # 2번 연속 빈 페이지면 해당 키워드 종료
                        break
                    continue

                empty_count = 0
                new_count = 0
                for p in products:
                    pid = p["product_id"]
                    if pid not in all_products:
                        # 카테고리 설정
                        p["category"] = cat_name
                        p["last_updated"] = datetime.now().isoformat()
                        all_products[pid] = p
                        new_count += 1
                        category_count += 1
                    else:
                        # 기존 상품 업데이트 (가격 등)
                        existing = all_products[pid]
                        if p.get("buy_now_price"):
                            existing["buy_now_price"] = p["buy_now_price"]
                        if p.get("sell_now_price"):
                            existing["sell_now_price"] = p["sell_now_price"]
                        if p.get("trading_volume"):
                            existing["trading_volume"] = p["trading_volume"]
                        existing["last_updated"] = datetime.now().isoformat()

                if on_progress and page % 2 == 0:
                    await on_progress(
                        f"📦 {cat_name} [{keyword}] {page}/{pages_to_fetch} 페이지 "
                        f"수집중... {category_count:,}개 (+{new_count} 신규)"
                    )

                logger.info(
                    "%s [%s] page %d: %d건 (신규 %d)",
                    cat_name, keyword, page, len(products), new_count,
                )

            if test_mode:
                break  # 테스트 모드: 첫 키워드만

        stats[cat_name] = category_count
        if on_progress:
            await on_progress(
                f"✅ {cat_name} 카테고리 완료: {category_count:,}개 수집 "
                f"(전체 {len(all_products):,}개)"
            )

        if test_mode:
            break  # 테스트 모드: 첫 카테고리만

    # 거래량 0 제외
    filtered = {
        pid: p for pid, p in all_products.items()
        if p.get("trading_volume", 0) > 0 or not p.get("trading_volume")
        # trading_volume이 0이 아니거나, 아직 조회되지 않은(없는) 경우는 유지
        # 명시적으로 0인 경우만 제외
    }
    # 엄격 필터: trading_volume 키가 있고 값이 0인 것만 제외
    filtered = {
        pid: p for pid, p in all_products.items()
        if not (isinstance(p.get("trading_volume"), (int, float)) and p["trading_volume"] == 0)
    }
    excluded = len(all_products) - len(filtered)

    elapsed = (datetime.now() - started_at).total_seconds()

    # JSON 저장
    db_data = {
        "meta": {
            "total_products": len(filtered),
            "excluded_zero_volume": excluded,
            "categories": stats,
            "last_built": datetime.now().isoformat(),
            "build_time_seconds": round(elapsed, 1),
            "test_mode": test_mode,
        },
        "products": list(filtered.values()),
    }

    DB_PATH.write_text(
        json.dumps(db_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = (
        f"DB 구축 완료: {len(filtered):,}개 상품 "
        f"(거래량 0 제외: {excluded}건) | "
        f"소요시간: {elapsed:.0f}초"
    )
    logger.info(summary)
    if on_progress:
        await on_progress(f"🏁 {summary}")

    return {
        "total": len(filtered),
        "excluded": excluded,
        "by_category": stats,
        "path": str(DB_PATH),
        "elapsed_seconds": round(elapsed, 1),
    }
