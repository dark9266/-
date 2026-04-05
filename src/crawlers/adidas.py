"""아디다스 공식몰 (adidas.co.kr) 크롤러.

아디다스 한국 공식몰에서 상품 정보를 수집한다.
검색: adidas.co.kr/search?q={query} HTML 파싱
상세: adidas.co.kr/{product_id}.html HTML 파싱 (schema.org + 사이즈 데이터)
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

logger = setup_logger("adidas_crawler")

BASE_URL = "https://www.adidas.co.kr"
SEARCH_URL = BASE_URL + "/search?q={query}"
PRODUCT_URL = BASE_URL + "/{product_id}.html"

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


def _extract_model_id(name: str) -> str:
    """상품명이나 URL에서 아디다스 모델 ID 추출.

    아디다스 모델 ID 패턴: IG1025, GW2871, HP5586, IF8065 등
    """
    m = re.search(r'\b([A-Z]{2}\d{4})\b', name.upper())
    if m:
        return m.group(1)

    # 더 긴 패턴: IE1775 등
    m = re.search(r'\b([A-Z]{1,3}\d{3,5})\b', name.upper())
    if m:
        return m.group(1)

    return ""


def _parse_schema_org(html: str) -> dict:
    """schema.org JSON-LD에서 Product 정보 추출."""
    try:
        scripts = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        for script in scripts:
            data = json.loads(script)
            if data.get("@type") == "Product":
                return data
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return {}


def _parse_search_results(html: str) -> list[dict]:
    """검색 결과 HTML에서 상품 목록 파싱."""
    results = []

    # 방법 1: __NEXT_DATA__ 또는 window.__STATE__ JSON
    state_match = re.search(
        r'window\.__(?:NEXT_DATA__|STATE__|INITIAL_STATE__)?\s*=\s*({.*?});?\s*</script>',
        html, re.DOTALL,
    )
    if state_match:
        try:
            state = json.loads(state_match.group(1))
            # 아디다스 검색 결과 구조 탐색
            items = _extract_items_from_state(state)
            for item in items:
                results.append(item)
        except (json.JSONDecodeError, ValueError):
            pass

    # 방법 2: schema.org ItemList
    if not results:
        try:
            scripts = re.findall(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                html, re.DOTALL,
            )
            for script in scripts:
                data = json.loads(script)
                if data.get("@type") == "ItemList":
                    for elem in data.get("itemListElement", []):
                        item = elem.get("item", elem)
                        if not isinstance(item, dict):
                            continue
                        name = item.get("name", "")
                        url = item.get("url", "")
                        model = _extract_model_id(name) or _extract_model_id(url)
                        prod_id = re.search(r'/([A-Z0-9]+)\.html', url)

                        offers = item.get("offers", {})
                        price = int(offers.get("price", 0)) if offers else 0

                        results.append({
                            "product_id": prod_id.group(1) if prod_id else model,
                            "name": name,
                            "brand": "adidas",
                            "model_number": model,
                            "price": price,
                            "original_price": price,
                            "url": url,
                            "image_url": item.get("image", ""),
                            "is_sold_out": False,
                        })
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # 방법 3: product card HTML
    if not results:
        cards = re.findall(
            r'<a[^>]*href="(/([A-Z0-9]+)\.html)"[^>]*class="[^"]*product-card[^"]*"',
            html,
        )
        for url_path, prod_id in cards:
            results.append({
                "product_id": prod_id,
                "name": "",
                "brand": "adidas",
                "model_number": _extract_model_id(prod_id),
                "price": 0,
                "original_price": 0,
                "url": BASE_URL + url_path,
                "image_url": "",
                "is_sold_out": False,
            })

    return results


def _extract_items_from_state(state: dict) -> list[dict]:
    """window.__STATE__ 구조에서 상품 목록 추출."""
    items = []

    # 재귀적으로 itemList 또는 products 키 탐색
    def _search(obj, depth=0):
        if depth > 5 or not isinstance(obj, dict):
            return
        for key in ("itemList", "products", "items", "productList"):
            val = obj.get(key)
            if isinstance(val, list):
                for item in val:
                    if not isinstance(item, dict):
                        continue
                    name = (
                        item.get("displayName", "")
                        or item.get("name", "")
                        or item.get("title", "")
                    )
                    model = (
                        item.get("modelId", "")
                        or item.get("articleNumber", "")
                        or _extract_model_id(name)
                    )
                    price = (
                        item.get("salePrice", 0)
                        or item.get("price", 0)
                        or item.get("currentPrice", 0)
                    )
                    prod_id = item.get("productId", "") or item.get("id", "") or model
                    link = item.get("link", "") or item.get("url", "")

                    if name or model:
                        items.append({
                            "product_id": str(prod_id),
                            "name": name,
                            "brand": "adidas",
                            "model_number": model,
                            "price": int(price) if price else 0,
                            "original_price": int(
                                item.get("originalPrice", 0)
                                or item.get("standardPrice", 0)
                                or price
                            ),
                            "url": link if link.startswith("http") else BASE_URL + link,
                            "image_url": item.get("image", ""),
                            "is_sold_out": item.get("isSoldOut", False),
                        })
        for v in obj.values():
            if isinstance(v, dict):
                _search(v, depth + 1)

    _search(state)
    return items


class AdidasCrawler:
    """아디다스 공식몰 크롤러."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=3, min_interval=2.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": _random_ua(),
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                },
                timeout=15,
                follow_redirects=True,
            )
        return self._client

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """아디다스 KR 검색.

        Returns:
            [{"product_id": str, "name": str, "brand": str, "model_number": str,
              "price": int, "original_price": int, "url": str, ...}, ...]
        """
        client = await self._get_client()
        url = SEARCH_URL.format(query=keyword.replace(" ", "+"))

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(url, headers={"User-Agent": _random_ua()})
            if resp.status_code == 403:
                logger.warning("아디다스 검색 차단 (403): %s", keyword)
                return []
            if resp.status_code != 200:
                logger.warning("아디다스 검색 실패 (HTTP %d): %s", resp.status_code, keyword)
                return []

            results = _parse_search_results(resp.text)
            logger.info("아디다스 검색 '%s': %d건", keyword, len(results))
            return results[:limit]

        except Exception as e:
            logger.error("아디다스 검색 에러 (%s): %s", keyword, e)
            return []

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 페이지에서 사이즈별 가격/재고 수집."""
        client = await self._get_client()
        url = PRODUCT_URL.format(product_id=product_id)

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(url, headers={"User-Agent": _random_ua()})
            if resp.status_code != 200:
                logger.warning(
                    "아디다스 상품 조회 실패 (HTTP %d): %s", resp.status_code, product_id,
                )
                return None

            html = resp.text
            schema = _parse_schema_org(html)

            name = schema.get("name", "")
            brand = "adidas"
            image = schema.get("image", "")

            offers = schema.get("offers", {})
            sale_price = int(offers.get("price", 0))
            original_price = sale_price

            model_number = _extract_model_id(name) or _extract_model_id(product_id)

            # 사이즈 파싱
            sizes = self._parse_sizes_from_html(html, sale_price, original_price)

            product = RetailProduct(
                source="adidas",
                product_id=product_id,
                name=name,
                model_number=model_number,
                brand=brand,
                url=url,
                image_url=image if isinstance(image, str) else "",
                sizes=sizes,
                fetched_at=datetime.now(),
            )

            logger.info(
                "아디다스 상품: %s | 모델: %s | 가격: %s원 | 사이즈: %d개",
                name, model_number,
                f"{sale_price:,}" if sale_price else "?",
                len(sizes),
            )
            return product

        except Exception as e:
            logger.error("아디다스 상품 조회 에러 (%s): %s", product_id, e)
            return None

    def _parse_sizes_from_html(
        self, html: str, sale_price: int, original_price: int,
    ) -> list[RetailSizeInfo]:
        """HTML에서 사이즈 정보 파싱."""
        sizes: list[RetailSizeInfo] = []
        seen: set[str] = set()

        # 방법 1: JSON 내 사이즈 데이터 (window.__STATE__ 또는 inline)
        size_json = re.search(
            r'"variation_list"\s*:\s*(\[.*?\])', html, re.DOTALL,
        )
        if size_json:
            try:
                variations = json.loads(size_json.group(1))
                for v in variations:
                    size_val = v.get("size", "") or v.get("label", "")
                    if not size_val or size_val in seen:
                        continue
                    seen.add(size_val)

                    avail = v.get("availability_status", "")
                    if avail in ("NOT_AVAILABLE", "PREVIEW", ""):
                        continue

                    discount_rate = 0.0
                    if original_price and sale_price and original_price > sale_price:
                        discount_rate = round(1 - sale_price / original_price, 3)

                    sizes.append(RetailSizeInfo(
                        size=size_val,
                        price=sale_price,
                        original_price=original_price,
                        in_stock=True,
                        discount_type="할인" if discount_rate > 0 else "",
                        discount_rate=discount_rate,
                    ))
            except (json.JSONDecodeError, ValueError):
                pass

        # 방법 2: 사이즈 버튼 HTML 파싱 (폴백)
        if not sizes:
            buttons = re.findall(
                r'data-size="([^"]+)"[^>]*(?:class="[^"]*(?:unavailable|disabled)[^"]*")?',
                html,
            )
            for size in buttons:
                if not size or size in seen:
                    continue
                seen.add(size)
                sizes.append(RetailSizeInfo(
                    size=size,
                    price=sale_price,
                    original_price=original_price,
                    in_stock=True,
                    discount_type="",
                    discount_rate=0.0,
                ))

        return sizes

    async def disconnect(self) -> None:
        """클라이언트 종료."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("아디다스 크롤러 연결 해제")


# 싱글톤
adidas_crawler = AdidasCrawler()
register("adidas", adidas_crawler, "아디다스")
