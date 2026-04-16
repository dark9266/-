"""아크테릭스 코리아(arcteryx.co.kr) 크롤러.

Laravel 백엔드 REST API (GET 전용).
검색 API + 옵션(사이즈/재고) API 조합.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("arcteryx_crawler")

API_BASE = "https://api.arcteryx.co.kr"
WEB_BASE = "https://arcteryx.co.kr"

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": f"{WEB_BASE}/",
    "Origin": WEB_BASE,
}

# 아크테릭스 사이즈 → 크림 사이즈 매핑 (의류)
ARCTERYX_SIZE_MAP: dict[str, str] = {
    "2XS": "2XS",
    "XXS": "2XS",
    "XS": "XS",
    "SM": "S",
    "S": "S",
    "MD": "M",
    "M": "M",
    "LG": "L",
    "L": "L",
    "XL": "XL",
    "XXL": "2XL",
    "2XL": "2XL",
    "3XL": "3XL",
}


# --------------- 순수 파싱 함수 ---------------


def _parse_search_item(item: dict) -> dict | None:
    """검색 API 응답의 단일 상품 → 표준 search result dict.

    sale_state != "ON" 이면 None 반환.
    """
    sale_state = item.get("sale_state") or ""
    if sale_state != "ON":
        return None

    product_id = item.get("product_id")
    if not product_id:
        return None

    name = item.get("product_name") or ""
    sell_price = int(item.get("sell_price") or 0)
    retail_price = int(item.get("retail_price") or 0)

    # 이미지 URL 추출
    image_url = ""
    option_images = item.get("option_image") or []
    if option_images and isinstance(option_images, list):
        first_img = option_images[0]
        if isinstance(first_img, dict):
            image_url = first_img.get("image_chip") or ""

    return {
        "product_id": str(product_id),
        "name": name,
        "brand": "Arc'teryx",
        "model_number": "",
        "price": sell_price,
        "original_price": retail_price or sell_price,
        "url": f"{WEB_BASE}/products/{product_id}",
        "image_url": image_url,
        "is_sold_out": False,
        "sizes": [],
    }


def _parse_options(data: dict) -> tuple[str, list[dict]]:
    """옵션 API 응답 → (model_number, sizes_list).

    level 1 = Colour (code 필드 → model_number)
    level 2 = Size (사이즈별 가격/재고)

    Returns:
        (model_number, [{"size", "price", "original_price", "in_stock", "stock"}])
    """
    options = data.get("options") or []
    model_number = ""
    sizes: list[dict] = []

    for option in options:
        level = option.get("level")

        if level == 1:
            # Colour 레벨 — code 필드에서 모델번호 추출
            code = option.get("code") or ""
            if code:
                model_number = code
            # values에서도 code 시도 (fallback)
            if not model_number:
                values = option.get("values") or []
                if values and isinstance(values[0], dict):
                    model_number = values[0].get("value") or ""

        elif level == 2:
            # Size 레벨 — 멀티컬러 상품은 같은 사이즈가 여러 번 나옴
            values = option.get("values") or []
            seen_sizes: set[str] = set()
            for val in values:
                if not isinstance(val, dict):
                    continue

                size_val = str(val.get("value") or "").strip()
                if not size_val or size_val in seen_sizes:
                    continue

                val_sale_state = val.get("sale_state") or ""
                is_orderable = bool(val.get("is_orderable", False))
                stock = int(val.get("stock") or 0)
                sell_price = int(val.get("sell_price") or 0)

                in_stock = (
                    val_sale_state == "ON" and is_orderable and stock > 0
                )

                seen_sizes.add(size_val)
                sizes.append({
                    "size": size_val,
                    "price": sell_price,
                    "original_price": sell_price,
                    "in_stock": in_stock,
                    "stock": stock,
                })

    return model_number, sizes


def normalize_arcteryx_size(size: str) -> str:
    """아크테릭스 사이즈를 크림 사이즈로 변환."""
    upper = size.strip().upper()
    return ARCTERYX_SIZE_MAP.get(upper, upper)


# --------------- 크롤러 클래스 ---------------


class ArcteryxCrawler:
    """아크테릭스 코리아(arcteryx.co.kr) 크롤러."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=2.0)

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
        """키워드로 상품 검색."""
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        client = await self._get_client()
        params = {
            "search_keyword": keyword,
            "search_type": "keyword",
        }

        async with self._rate_limiter.acquire():
            resp = await client.get(
                f"{API_BASE}/api/products/search", params=params
            )

        if resp.status_code != 200:
            logger.warning(
                "아크테릭스 검색 HTTP %d (keyword=%s)",
                resp.status_code,
                keyword,
            )
            return []

        try:
            body = resp.json()
        except ValueError:
            logger.warning("아크테릭스 검색 JSON 파싱 실패 (keyword=%s)", keyword)
            return []

        if not body.get("success"):
            logger.warning("아크테릭스 검색 실패 응답 (keyword=%s)", keyword)
            return []

        data = body.get("data") or {}
        rows = data.get("rows") or []

        results: list[dict] = []
        for item in rows:
            parsed = _parse_search_item(item)
            if parsed is not None:
                results.append(parsed)

        logger.info("아크테릭스 검색 '%s': %d건", keyword, len(results))
        return results[:limit]

    async def get_product_detail(
        self, product_id: str
    ) -> RetailProduct | None:
        """상품 상세 조회 — 옵션 API에서 사이즈/재고 추출."""
        if not product_id:
            return None

        client = await self._get_client()

        async with self._rate_limiter.acquire():
            resp = await client.get(
                f"{API_BASE}/api/products/{product_id}/options"
            )

        if resp.status_code != 200:
            logger.warning(
                "아크테릭스 옵션 HTTP %d (id=%s)",
                resp.status_code,
                product_id,
            )
            return None

        try:
            body = resp.json()
        except ValueError:
            logger.warning(
                "아크테릭스 옵션 JSON 파싱 실패 (id=%s)", product_id
            )
            return None

        if not body.get("success"):
            return None

        data = body.get("data") or {}
        if not data.get("has_options"):
            return None

        model_number, raw_sizes = _parse_options(data)

        # RetailSizeInfo 변환 (재고 있는 사이즈만)
        sizes: list[RetailSizeInfo] = []
        for s in raw_sizes:
            if not s["in_stock"]:
                continue
            kream_size = normalize_arcteryx_size(s["size"])
            opt_price = s["price"]
            orig = s["original_price"] or opt_price
            discount_rate = 0.0
            if orig and opt_price and orig > opt_price:
                discount_rate = round(1 - opt_price / orig, 3)
            sizes.append(
                RetailSizeInfo(
                    size=kream_size,
                    price=opt_price,
                    original_price=orig,
                    in_stock=True,
                    discount_type="할인" if discount_rate > 0 else "",
                    discount_rate=discount_rate,
                )
            )

        product = RetailProduct(
            source="arcteryx",
            product_id=str(product_id),
            name="",
            model_number=model_number,
            brand="Arc'teryx",
            url=f"{WEB_BASE}/products/{product_id}",
            image_url="",
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "아크테릭스 상품: id=%s | 모델: %s | 사이즈: %d개",
            product_id,
            model_number,
            len(sizes),
        )
        return product

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("아크테릭스 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
arcteryx_crawler = ArcteryxCrawler()
register("arcteryx", arcteryx_crawler, "아크테릭스")
