"""ABC마트 (a-rt.com) 크롤러.

ABC마트 온라인몰(a-rt.com) 상품 페이지 HTML 파싱으로 상품 정보를 수집한다.
검색: a-rt.com/product?keyword={query} HTML 파싱
상세: a-rt.com/product/{goodsCd} HTML 파싱 (schema.org JSON-LD + 사이즈 옵션)
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

logger = setup_logger("abcmart_crawler")

BASE_URL = "https://abcmart.a-rt.com"
SEARCH_URL = BASE_URL + "/product?keyword={query}"
PRODUCT_URL = BASE_URL + "/product/{goods_cd}"

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


def _extract_model_number(name: str) -> str:
    """상품명에서 모델번호 추출.

    패턴: "나이키 덩크 로우 DQ8423-100" 또는 "아디다스 삼바 IG1025"
    """
    # 하이픈 포함 패턴 (DQ8423-100)
    m = re.search(r'\b([A-Z]{1,5}\d{3,5}[-]\d{2,4})\b', name.upper())
    if m:
        return m.group(1)

    # 알파벳+숫자 혼합 패턴 (IG1025, U7408PL)
    m = re.search(r'\b([A-Z]{1,3}\d{3,5}[A-Z]{0,3})\b', name.upper())
    if m:
        return m.group(1)

    return ""


def _parse_schema_org(html: str) -> dict:
    """schema.org JSON-LD에서 상품 정보 추출."""
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


def _parse_products_from_html(html: str) -> list[dict]:
    """검색 결과 HTML에서 상품 목록 파싱.

    ABC마트는 JS 렌더링이 많아 서버 렌더링된 부분만 추출.
    schema.org JSON-LD 또는 상품 카드 HTML에서 파싱.
    """
    results = []

    # 방법 1: schema.org ItemList
    try:
        scripts = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        for script in scripts:
            data = json.loads(script)
            if data.get("@type") == "ItemList":
                for item in data.get("itemListElement", []):
                    product = item.get("item", {})
                    if product.get("@type") != "Product":
                        continue

                    name = product.get("name", "")
                    offers = product.get("offers", {})
                    price = int(offers.get("price", 0))
                    url = product.get("url", "")
                    goods_cd = re.search(r'/product/(\d+)', url)

                    results.append({
                        "product_id": goods_cd.group(1) if goods_cd else "",
                        "name": name,
                        "brand": product.get("brand", {}).get("name", ""),
                        "model_number": _extract_model_number(name),
                        "price": price,
                        "original_price": price,
                        "url": url,
                        "image_url": product.get("image", ""),
                        "is_sold_out": False,
                    })
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 방법 2: 상품 카드 HTML 파싱 (폴백)
    if not results:
        cards = re.findall(
            r'<a[^>]*href="(/product/(\d+))"[^>]*class="[^"]*item-link[^"]*"[^>]*>'
            r'.*?</a>',
            html, re.DOTALL,
        )
        for url_path, goods_cd in cards:
            results.append({
                "product_id": goods_cd,
                "name": "",
                "brand": "",
                "model_number": "",
                "price": 0,
                "original_price": 0,
                "url": BASE_URL + url_path,
                "image_url": "",
                "is_sold_out": False,
            })

    return results


class AbcMartCrawler:
    """ABC마트 크롤러."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=3, min_interval=2.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": _random_ua()},
                timeout=15,
                follow_redirects=True,
                verify=False,
            )
        return self._client

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """ABC마트 검색.

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
                logger.warning("ABC마트 검색 실패 (HTTP %d): %s", resp.status_code, keyword)
                return []

            results = _parse_products_from_html(resp.text)
            logger.info("ABC마트 검색 '%s': %d건", keyword, len(results))
            return results[:limit]

        except Exception as e:
            logger.error("ABC마트 검색 에러 (%s): %s", keyword, e)
            return []

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 페이지에서 사이즈별 가격/재고 수집."""
        client = await self._get_client()
        url = PRODUCT_URL.format(goods_cd=product_id)

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(url, headers={"User-Agent": _random_ua()})
            if resp.status_code != 200:
                logger.warning(
                    "ABC마트 상품 조회 실패 (HTTP %d): %s", resp.status_code, product_id,
                )
                return None

            html = resp.text
            schema = _parse_schema_org(html)

            name = schema.get("name", "")
            brand_obj = schema.get("brand", {})
            brand = brand_obj.get("name", "") if isinstance(brand_obj, dict) else ""
            image = schema.get("image", "")

            offers = schema.get("offers", {})
            sale_price = int(offers.get("price", 0))
            availability = offers.get("availability", "")
            if "OutOfStock" in availability:
                logger.info("ABC마트 품절 상품: %s", product_id)
                return None

            # 사이즈 파싱: 옵션 select 태그 또는 data 속성
            sizes = self._parse_sizes_from_html(html, sale_price)

            model_number = _extract_model_number(name)

            product = RetailProduct(
                source="abcmart",
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
                "ABC마트 상품: %s | 모델: %s | 가격: %s원 | 사이즈: %d개",
                name, model_number,
                f"{sale_price:,}" if sale_price else "?",
                len(sizes),
            )
            return product

        except Exception as e:
            logger.error("ABC마트 상품 조회 에러 (%s): %s", product_id, e)
            return None

    def _parse_sizes_from_html(self, html: str, sale_price: int) -> list[RetailSizeInfo]:
        """HTML에서 사이즈 옵션 파싱."""
        sizes: list[RetailSizeInfo] = []
        seen: set[str] = set()

        # 패턴 1: select option 태그 (사이즈 선택)
        options = re.findall(
            r'<option[^>]*value="([^"]*)"[^>]*data-size="([^"]*)"[^>]*'
            r'(?:data-stock="([^"]*)")?[^>]*>([^<]*)</option>',
            html,
        )
        for value, size, stock, label in options:
            if not size or size in seen:
                continue
            seen.add(size)

            # 품절 체크
            is_sold_out = stock == "0" or "품절" in label
            if is_sold_out:
                continue

            sizes.append(RetailSizeInfo(
                size=size,
                price=sale_price,
                original_price=sale_price,
                in_stock=True,
                discount_type="",
                discount_rate=0.0,
            ))

        # 패턴 2: 사이즈 버튼 (대체 파싱)
        if not sizes:
            buttons = re.findall(
                r'class="[^"]*size[^"]*(?:sold-out|disabled)?[^"]*"[^>]*'
                r'data-size="([^"]*)"',
                html,
            )
            for size in buttons:
                if not size or size in seen:
                    continue
                seen.add(size)
                sizes.append(RetailSizeInfo(
                    size=size,
                    price=sale_price,
                    original_price=sale_price,
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
        logger.info("ABC마트 크롤러 연결 해제")


# 싱글톤
abcmart_crawler = AbcMartCrawler()
register("abcmart", abcmart_crawler, "ABC마트")
