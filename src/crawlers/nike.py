"""나이키 공식몰 (nike.com/kr) 크롤러.

Nike KR 검색 페이지의 __NEXT_DATA__에서 상품 정보를 파싱한다.
상세: 상품 페이지 HTML 파싱 → 사이즈/재고/가격.
"""

import json
import random
import re
from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("nike_crawler")

SEARCH_URL = "https://www.nike.com/kr/w?q={query}"
PRODUCT_URL = "https://www.nike.com/kr/t/{slug}/{style_color}"
PDP_URL = "https://www.nike.com/kr/t/_/{style_color}"

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
]


def _random_ua() -> str:
    return random.choice(USER_AGENTS)


def _extract_next_data(html: str) -> dict:
    """HTML에서 __NEXT_DATA__ JSON 추출."""
    m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return {}


def _parse_products_from_wall(next_data: dict) -> list[dict]:
    """__NEXT_DATA__ Wall에서 상품 목록 파싱."""
    wall = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("initialState", {})
        .get("Wall", {})
    )
    groupings = wall.get("productGroupings", [])
    results = []

    for group in groupings:
        products = group.get("products", [])
        for prod in products:
            code = prod.get("productCode", "")
            if not code:
                continue

            copy = prod.get("copy", {})
            prices = prod.get("prices", {})
            pdp = prod.get("pdpUrl", {})

            results.append({
                "product_id": code,
                "name": copy.get("title", ""),
                "brand": "Nike",
                "model_number": code,
                "price": prices.get("currentPrice", 0),
                "original_price": prices.get("initialPrice", 0),
                "url": pdp.get("url", "") if isinstance(pdp, dict) else "",
                "image_url": "",
                "is_sold_out": False,
            })

    return results


def _parse_sizes_from_pdp(html: str) -> list[dict]:
    """상품 상세 페이지에서 사이즈/재고 파싱.

    2024+ 구조: pageProps.selectedProduct.sizes[] (localizedLabel + status)
    """
    next_data = _extract_next_data(html)
    selected = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("selectedProduct", {})
    )

    sizes_data = selected.get("sizes", [])
    sizes = []
    for s in sizes_data:
        size_val = s.get("localizedLabel", "") or s.get("label", "")
        available = s.get("status", "") == "ACTIVE"
        if size_val:
            sizes.append({"size": size_val, "available": available})

    return sizes


def _parse_stock_from_threads(data: dict) -> dict[str, bool]:
    """threads API 응답에서 사이즈별 재고 매핑 파싱.

    Returns: {"240": False, "250": True, ...}
    """
    objects = data.get("objects", [])
    if not objects:
        return {}

    product_info_list = objects[0].get("productInfo", [])
    if not product_info_list:
        return {}

    pi = product_info_list[0]

    # GTIN → localizedSize 매핑
    gtin_to_size: dict[str, str] = {}
    for sku in pi.get("skus", []):
        gtin = sku.get("gtin", "")
        if not gtin:
            continue
        for spec in sku.get("countrySpecifications", []):
            localized = spec.get("localizedSize", "")
            if localized:
                gtin_to_size[gtin] = localized
                break

    # GTIN별 available 확인
    stock_map: dict[str, bool] = {}
    for ag in pi.get("availableGtins", []):
        gtin = ag.get("gtin", "")
        available = ag.get("available", False)
        size = gtin_to_size.get(gtin)
        if size:
            stock_map[size] = available

    return stock_map


class NikeCrawler:
    """나이키 공식몰 크롤러."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=3, min_interval=2.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": _random_ua()},
                timeout=15,
                follow_redirects=True,
            )
        return self._client

    async def _fetch_stock_by_threads_api(self, style_color: str) -> dict[str, bool]:
        """product_feed/threads API로 사이즈별 재고 조회.

        Returns: {localizedSize: available} (예: {"240": False, "250": True})
        API 실패 시 빈 딕셔너리 반환 (기존 로직 fallback).
        """
        client = await self._get_client()
        url = "https://api.nike.com/product_feed/threads/v3/"
        params = [
            ("filter", "marketplace(KR)"),
            ("filter", "language(ko)"),
            ("filter", f"productInfo.merchProduct.styleColor({style_color})"),
        ]
        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(
                    url, params=params, headers={"User-Agent": _random_ua()}
                )
            if resp.status_code != 200:
                logger.warning(
                    "Nike threads API 실패 (HTTP %d): %s", resp.status_code, style_color
                )
                return {}
            data = resp.json()
            stock_map = _parse_stock_from_threads(data)
            logger.debug("Nike threads 재고: %s → %s", style_color, stock_map)
            return stock_map
        except Exception as e:
            logger.warning("Nike threads API 에러 (%s): %s", style_color, e)
            return {}

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """Nike KR 검색.

        Returns:
            [{"product_id": str, "name": str, "brand": str, "model_number": str,
              "price": int, "original_price": int, "url": str, ...}, ...]
        """
        client = await self._get_client()
        url = SEARCH_URL.format(query=keyword.replace(" ", "+"))

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(url, headers={"User-Agent": _random_ua()})
            if resp.status_code != 200:
                logger.warning("Nike 검색 실패 (HTTP %d): %s", resp.status_code, keyword)
                return []

            next_data = _extract_next_data(resp.text)
            if not next_data:
                logger.warning("Nike __NEXT_DATA__ 파싱 실패: %s", keyword)
                return []

            results = _parse_products_from_wall(next_data)
            logger.info("Nike 검색 '%s': %d건", keyword, len(results))
            return results[:limit]

        except Exception as e:
            logger.error("Nike 검색 에러 (%s): %s", keyword, e)
            return []

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 페이지에서 사이즈별 가격/재고 수집."""
        client = await self._get_client()
        url = PDP_URL.format(style_color=product_id)

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(url, headers={"User-Agent": _random_ua()})
            if resp.status_code != 200:
                logger.warning("Nike 상품 조회 실패 (HTTP %d): %s", resp.status_code, product_id)
                return None

            html = resp.text
            next_data = _extract_next_data(html)

            # 기본 정보 (2024+ 구조: pageProps.selectedProduct)
            selected = (
                next_data.get("props", {})
                .get("pageProps", {})
                .get("selectedProduct", {})
            )
            product_info = selected.get("productInfo", {})
            title = product_info.get("title", "") or product_info.get("fullTitle", "")
            prices = selected.get("prices", {})
            current_price = prices.get("currentPrice", 0)
            full_price = prices.get("initialPrice", 0)

            # 사이즈 파싱
            size_data = _parse_sizes_from_pdp(html)

            # threads API로 실제 재고 확인
            stock_map = await self._fetch_stock_by_threads_api(product_id)

            sizes = []
            for s in size_data:
                if not s.get("available", False):
                    continue

                # threads API 재고 체크 (결과 있을 때만 적용)
                if stock_map and not stock_map.get(s["size"], False):
                    logger.debug("Nike 품절 사이즈 스킵: %s %s", product_id, s["size"])
                    continue

                discount_rate = 0.0
                if full_price and current_price and full_price > current_price:
                    discount_rate = round(1 - current_price / full_price, 3)

                sizes.append(RetailSizeInfo(
                    size=s["size"],
                    price=current_price,
                    original_price=full_price or current_price,
                    in_stock=True,
                    discount_type="할인" if discount_rate > 0 else "",
                    discount_rate=discount_rate,
                ))

            product = RetailProduct(
                source="nike",
                product_id=product_id,
                name=title,
                model_number=product_id,
                brand="Nike",
                url=url,
                image_url="",
                sizes=sizes,
                fetched_at=datetime.now(),
            )

            logger.info(
                "Nike 상품: %s | 모델: %s | 가격: %s원 | 사이즈: %d개",
                title, product_id,
                f"{current_price:,}" if current_price else "?",
                len(sizes),
            )
            return product

        except Exception as e:
            logger.error("Nike 상품 조회 에러 (%s): %s", product_id, e)
            return None

    async def disconnect(self) -> None:
        """클라이언트 종료."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("Nike 크롤러 연결 해제")


# 싱글톤
nike_crawler = NikeCrawler()
register("nike", nike_crawler, "나이키")
