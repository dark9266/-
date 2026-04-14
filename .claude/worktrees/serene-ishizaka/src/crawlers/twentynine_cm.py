"""29CM 크롤러.

29CM 검색 API + 상품 페이지 파싱으로 상품 정보를 수집한다.
검색: search-api.29cm.co.kr REST API (JSON)
상세: www.29cm.co.kr/products/{id} HTML 파싱 (schema.org + RSC payload)
"""

import json
import random
import re
from datetime import datetime

import httpx

from src.config import settings
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("29cm_crawler")

SEARCH_API = "https://search-api.29cm.co.kr/api/v4/products"
PRODUCT_URL = "https://www.29cm.co.kr/products/{item_no}"
IMAGE_CDN = "https://img.29cm.co.kr"

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
]


def _random_ua() -> str:
    return random.choice(USER_AGENTS)


def _extract_model_number(item_name: str) -> str:
    """상품명에서 모델번호 추출.

    29CM 상품명 패턴: "에어포스 1 '07 - 화이트 / CW2288-111"
    슬래시(/) 뒤의 알파벳+숫자 조합이 모델번호.
    """
    m = re.search(r"/\s*([A-Za-z0-9][-A-Za-z0-9\s]+)", item_name)
    if m:
        return m.group(1).strip()
    return ""


class TwentyNineCmCrawler:
    """29CM 크롤러."""

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

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """29CM 검색 API로 상품 검색.

        Returns:
            [{"product_id": str, "name": str, "brand": str, "model_number": str,
              "price": int, "original_price": int, "url": str, "image_url": str,
              "is_sold_out": bool}, ...]
        """
        client = await self._get_client()
        params = {"keyword": keyword, "limit": limit, "offset": 0}

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(
                    SEARCH_API, params=params, headers={"User-Agent": _random_ua()}
                )
            if resp.status_code != 200:
                logger.warning("29CM 검색 실패 (HTTP %d): %s", resp.status_code, keyword)
                return []
            body = resp.json()
        except Exception as e:
            logger.error("29CM 검색 에러 (%s): %s", keyword, e)
            return []

        if body.get("result") != "SUCCESS":
            logger.warning("29CM 검색 응답 에러: %s", body.get("message"))
            return []

        # v4/products: data가 직접 리스트 (이전: data.products)
        raw_data = body.get("data", [])
        products = raw_data if isinstance(raw_data, list) else raw_data.get("products", [])
        results = []

        for p in products:
            item_name = p.get("itemName", "")
            sale_info = p.get("saleInfoV2", {}) or {}
            image_path = p.get("imageUrl", "")
            image_url = f"{IMAGE_CDN}{image_path}" if image_path else ""

            # 할인가: saleInfoV2 > lastSalePrice > consumerPrice
            price = (
                sale_info.get("totalSellPrice")
                or sale_info.get("sellPrice")
                or p.get("lastSalePrice")
                or p.get("consumerPrice", 0)
            )

            results.append({
                "product_id": str(p["itemNo"]),
                "name": item_name,
                "brand": p.get("frontBrandNameEng", "") or p.get("frontBrandNameKor", ""),
                "model_number": _extract_model_number(item_name),
                "price": price,
                "original_price": p.get("consumerPrice", 0),
                "url": PRODUCT_URL.format(item_no=p["itemNo"]),
                "image_url": image_url,
                "is_sold_out": p.get("isSoldOut", False),
            })

        logger.info("29CM 검색 '%s': %d건", keyword, len(results))
        return results

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 페이지에서 사이즈별 가격/재고 수집.

        HTML의 schema.org JSON-LD + RSC payload를 파싱한다.
        """
        client = await self._get_client()
        url = PRODUCT_URL.format(item_no=product_id)

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(url, headers={"User-Agent": _random_ua()})
            if resp.status_code != 200:
                logger.warning("29CM 상품 조회 실패 (HTTP %d): %s", resp.status_code, product_id)
                return None
            html = resp.text
        except Exception as e:
            logger.error("29CM 상품 조회 에러 (%s): %s", product_id, e)
            return None

        # 1) schema.org JSON-LD에서 기본 정보 추출
        name, brand, image_url, sale_price, original_price = self._parse_schema_org(html)

        # 2) RSC payload에서 사이즈/재고 추출
        sizes = self._parse_sizes_from_rsc(html, sale_price, original_price)

        # 3) 상품명에서 모델번호 추출
        model_number = _extract_model_number(name)

        product = RetailProduct(
            source="29cm",
            product_id=product_id,
            name=name,
            model_number=model_number,
            brand=brand,
            url=url,
            image_url=image_url,
            sizes=sizes,
            fetched_at=datetime.now(),
        )

        logger.info(
            "29CM 상품: %s | 모델: %s | 가격: %s원 | 사이즈: %d개",
            name, model_number, f"{sale_price:,}" if sale_price else "?", len(sizes),
        )
        return product

    def _parse_schema_org(self, html: str) -> tuple[str, str, str, int, int]:
        """schema.org JSON-LD에서 상품 기본 정보 추출."""
        name = brand = image_url = ""
        sale_price = original_price = 0

        try:
            scripts = re.findall(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                html, re.DOTALL,
            )
            for script in scripts:
                data = json.loads(script)
                if data.get("@type") != "Product":
                    continue

                name = data.get("name", "")
                brand_obj = data.get("brand", {})
                brand = brand_obj.get("name", "") if isinstance(brand_obj, dict) else ""

                images = data.get("image", [])
                if images and isinstance(images, list):
                    image_url = images[0].get("contentUrl", "") if isinstance(images[0], dict) else ""

                offers = data.get("offers", {})
                sale_price = int(offers.get("price", 0))
                price_spec = offers.get("priceSpecification", {})
                original_price = int(price_spec.get("price", 0)) if price_spec else sale_price

                break
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug("schema.org 파싱 실패: %s", e)

        return name, brand, image_url, sale_price, original_price

    def _parse_sizes_from_rsc(
        self, html: str, sale_price: int, original_price: int
    ) -> list[RetailSizeInfo]:
        """RSC payload에서 사이즈 옵션 및 재고 추출."""
        sizes: list[RetailSizeInfo] = []
        seen: set[str] = set()

        # RSC payload는 escaped JSON (\\" = ")
        # 패턴: optionId\":38870616,\"optionStatusTypeName\":\"ON_STOCK\",...\"optionItemValue\":\"230\"
        q = r'\\"'  # escaped quote in RSC payload
        option_pattern = re.compile(
            rf'{q}optionId{q}:\d+,'
            rf'{q}optionStatusTypeName{q}:{q}([^\\]+){q},'
            rf'{q}optionStatusTypeDescription{q}:{q}[^\\]*{q},'
            rf'{q}isSoldOut{q}:(true|false),'
            rf'{q}isVisible{q}:(true|false),'
            rf'{q}optionName{q}:{q}\[SIZE\]([^\\]+){q},'
            rf'{q}optionItemName{q}:{q}SIZE{q},'
            rf'{q}optionItemValue{q}:{q}([^\\]+){q}'
        )

        for m in option_pattern.finditer(html):
            status = m.group(1)  # ON_STOCK, SOLD_OUT, etc.
            is_sold_out = m.group(2) == "true"
            size_value = m.group(5)  # "230", "250" 등

            if size_value in seen:
                continue
            seen.add(size_value)

            in_stock = status == "ON_STOCK" and not is_sold_out

            if not in_stock:
                logger.debug("29CM 품절 사이즈 스킵: %s", size_value)
                continue

            discount_rate = 0.0
            if original_price and sale_price and original_price > sale_price:
                discount_rate = round(1 - sale_price / original_price, 3)

            sizes.append(RetailSizeInfo(
                size=size_value,
                price=sale_price,
                original_price=original_price,
                in_stock=True,
                discount_type="할인" if discount_rate > 0 else "",
                discount_rate=discount_rate,
            ))

        return sizes

    async def disconnect(self) -> None:
        """클라이언트 종료."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("29CM 크롤러 연결 해제")


# 싱글톤
twentynine_cm_crawler = TwentyNineCmCrawler()

from src.crawlers.registry import register  # noqa: E402
register("29cm", twentynine_cm_crawler, "29CM")
