"""살로몬 한국 공식몰(salomon.co.kr) 크롤러.

Shopify 기반 JSON API (GET 전용).
SKU = 크림 모델번호 (L + 8자리), 사이즈 mm 단위 (변환 불필요).
"""

from __future__ import annotations

from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("salomon_crawler")

BASE_URL = "https://salomon.co.kr"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


# --------------- 순수 파싱 함수 ---------------


def _parse_variant(variant: dict) -> dict:
    """Shopify variant → 표준 사이즈 dict.

    variant 예시:
    {
        "title": "레이니 데이 / 팔로마 / 실버 / 230",
        "sku": "L47988000",
        "option1": "레이니 데이 / 팔로마 / 실버",
        "option2": "230",
        "price": "280000",
        "compare_at_price": "",
        "available": true
    }
    """
    size = (variant.get("option2") or "").strip()
    sku = (variant.get("sku") or "").strip().upper()
    price_str = variant.get("price") or "0"
    price = int(float(price_str))

    compare_str = variant.get("compare_at_price") or ""
    if compare_str and compare_str.strip():
        original_price = int(float(compare_str))
    else:
        original_price = price

    available = bool(variant.get("available", False))

    return {
        "size": size,
        "sku": sku,
        "price": price,
        "original_price": original_price,
        "in_stock": available,
    }


def _parse_product_json(product: dict) -> dict | None:
    """Shopify product JSON → 표준 search result dict.

    product는 /products/{handle}.json 응답의 "product" 키 값.
    """
    if product is None:
        return None

    handle = product.get("handle") or ""
    title = product.get("title") or ""

    # 이미지
    images = product.get("images") or []
    image_url = ""
    if images and isinstance(images, list) and len(images) > 0:
        image_url = images[0].get("src") or ""

    # variants 파싱
    variants = product.get("variants") or []
    sizes: list[dict] = []
    model_number = ""
    first_price = 0
    first_original = 0
    all_sold_out = True

    for v in variants:
        parsed = _parse_variant(v)
        if not model_number and parsed["sku"]:
            model_number = parsed["sku"]
        if not first_price and parsed["price"]:
            first_price = parsed["price"]
            first_original = parsed["original_price"]
        if parsed["in_stock"]:
            all_sold_out = False
        sizes.append({
            "size": parsed["size"],
            "price": parsed["price"],
            "original_price": parsed["original_price"],
            "in_stock": parsed["in_stock"],
        })

    return {
        "product_id": handle,
        "name": title,
        "brand": "Salomon",
        "model_number": model_number,
        "price": first_price,
        "original_price": first_original,
        "url": f"{BASE_URL}/products/{handle}" if handle else "",
        "image_url": image_url,
        "is_sold_out": all_sold_out if variants else True,
        "sizes": sizes,
    }


# --------------- 크롤러 클래스 ---------------


class SalomonCrawler:
    """살로몬 한국 공식몰 크롤러 — Shopify JSON API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=1.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=15.0,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    async def search_products(
        self, keyword: str, limit: int = 30
    ) -> list[dict]:
        """키워드(모델번호)로 상품 검색.

        1. keyword를 handle(소문자)로 변환하여 /products/{handle}.json 직접 조회
        2. 성공하면 SKU가 keyword와 일치하는 상품 반환
        3. 404이면 빈 리스트
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        handle = keyword.lower()
        url = f"{BASE_URL}/products/{handle}.json"

        client = await self._get_client()
        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(url)
            except httpx.HTTPError as e:
                logger.warning("살로몬 검색 HTTP 오류: %s (keyword=%s)", e, keyword)
                return []

        if resp.status_code == 404:
            logger.debug("살로몬 검색 404 (keyword=%s)", keyword)
            return []

        if resp.status_code != 200:
            logger.warning(
                "살로몬 검색 HTTP %d (keyword=%s)", resp.status_code, keyword
            )
            return []

        try:
            data = resp.json()
        except ValueError:
            logger.warning("살로몬 검색 JSON 파싱 실패 (keyword=%s)", keyword)
            return []

        product = data.get("product")
        if not product:
            return []

        parsed = _parse_product_json(product)
        if parsed is None:
            return []

        # SKU 일치 검증 (대소문자 무시)
        keyword_upper = keyword.upper()
        if parsed["model_number"] and parsed["model_number"] != keyword_upper:
            # variant 중 일치하는 SKU가 없으면 빈 리스트
            has_match = any(
                (v.get("sku") or "").strip().upper() == keyword_upper
                for v in (product.get("variants") or [])
            )
            if not has_match:
                logger.debug(
                    "살로몬 SKU 불일치: %s != %s", parsed["model_number"], keyword_upper
                )
                return []

        logger.info("살로몬 검색 '%s': 1건", keyword)
        return [parsed]

    async def get_product_detail(
        self, product_id: str
    ) -> RetailProduct | None:
        """상품 상세 조회.

        product_id = handle (SKU 소문자).
        /products/{handle}.json 호출.
        """
        if not product_id:
            return None

        handle = product_id.lower()
        url = f"{BASE_URL}/products/{handle}.json"

        client = await self._get_client()
        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(url)
            except httpx.HTTPError as e:
                logger.warning("살로몬 상세 HTTP 오류: %s (id=%s)", e, product_id)
                return None

        if resp.status_code != 200:
            logger.warning(
                "살로몬 상세 HTTP %d (id=%s)", resp.status_code, product_id
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        product = data.get("product")
        if not product:
            return None

        parsed = _parse_product_json(product)
        if parsed is None:
            return None

        # RetailSizeInfo 변환 (재고 있는 사이즈만)
        sizes: list[RetailSizeInfo] = []
        for s in parsed.get("sizes") or []:
            if not s["in_stock"]:
                continue
            opt_price = s["price"]
            orig = s["original_price"] or opt_price
            discount_rate = 0.0
            if orig and opt_price and orig > opt_price:
                discount_rate = round(1 - opt_price / orig, 3)
            sizes.append(
                RetailSizeInfo(
                    size=s["size"],
                    price=opt_price,
                    original_price=orig,
                    in_stock=True,
                    discount_type="할인" if discount_rate > 0 else "",
                    discount_rate=discount_rate,
                )
            )

        ret = RetailProduct(
            source="salomon",
            product_id=parsed["product_id"],
            name=parsed["name"],
            model_number=parsed["model_number"],
            brand=parsed["brand"],
            url=parsed["url"],
            image_url=parsed["image_url"],
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "살로몬 상품: %s | 모델: %s | %s원 | 사이즈: %d개",
            parsed["name"],
            parsed["model_number"],
            f"{parsed['price']:,}" if parsed["price"] else "?",
            len(sizes),
        )
        return ret

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("살로몬 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
salomon_crawler = SalomonCrawler()
register("salomon", salomon_crawler, "살로몬")
