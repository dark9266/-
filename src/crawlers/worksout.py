"""웍스아웃(worksout.co.kr) 크롤러.

REST API (GET 전용).
검색 API에서 사이즈/재고 포함, 상세 API는 보조.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("worksout_crawler")

API_BASE = "https://api.worksout.co.kr/v1"
SEARCH_URL = f"{API_BASE}/search"
DETAIL_URL = f"{API_BASE}/products"
WEB_BASE = "https://www.worksout.co.kr"

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.worksout.co.kr/",
    "Origin": "https://www.worksout.co.kr",
}


# --------------- 순수 파싱 함수 ---------------


def _parse_sizes(sizes_data: list[dict] | None) -> list[dict]:
    """API sizes 배열 → 표준 사이즈 리스트."""
    if not sizes_data or not isinstance(sizes_data, list):
        return []
    result: list[dict] = []
    for s in sizes_data:
        if not isinstance(s, dict):
            continue
        size_name = str(s.get("sizeName") or "").strip()
        if not size_name:
            continue
        is_sold_out = bool(s.get("isSoldOut", False))
        result.append({
            "size": size_name,
            "in_stock": not is_sold_out,
            "is_last": bool(s.get("isLast", False)),
        })
    return result


def _should_skip(product: dict) -> bool:
    """오프라인 전용, 프리오더, 전체 품절 필터."""
    if product.get("onlyOffline", False):
        return True
    if product.get("isPreOrder", False):
        return True
    return False


def _all_sold_out(sizes: list[dict]) -> bool:
    """모든 사이즈가 품절인지 확인."""
    if not sizes:
        return True
    return all(not s.get("in_stock", False) for s in sizes)


def _parse_search_product(item: dict) -> dict | None:
    """검색 API 상품 항목 → 표준 dict.

    필터링 대상이면 None 반환.
    """
    if _should_skip(item):
        return None

    product_id = str(item.get("productId") or "")
    if not product_id:
        return None

    brand = (item.get("brandName") or "").strip()
    name = (item.get("productName") or "").strip()
    korean_name = (item.get("productKoreanName") or "").strip()

    current_price = int(item.get("currentPrice") or 0)
    initial_price = int(item.get("initialPrice") or 0)

    # 이미지 URL
    image_url = ""
    image_list = item.get("imageUrls") or item.get("productImages") or []
    if isinstance(image_list, list) and image_list:
        first_img = image_list[0]
        if isinstance(first_img, str):
            image_url = first_img
        elif isinstance(first_img, dict):
            image_url = first_img.get("imageUrl") or first_img.get("url") or ""

    # 사이즈 파싱
    sizes_raw = item.get("sizes") or []
    sizes = _parse_sizes(sizes_raw)

    if _all_sold_out(sizes):
        return None

    # 사이즈에 가격 추가
    sized_list = []
    for s in sizes:
        sized_list.append({
            "size": s["size"],
            "price": current_price,
            "original_price": initial_price or current_price,
            "in_stock": s["in_stock"],
        })

    display_name = name or korean_name

    return {
        "product_id": product_id,
        "name": display_name,
        "brand": brand,
        "model_number": "",
        "price": current_price,
        "original_price": initial_price or current_price,
        "url": f"{WEB_BASE}/product/{product_id}",
        "image_url": image_url,
        "is_sold_out": False,
        "sizes": sized_list,
    }


# --------------- 크롤러 클래스 ---------------


class WorksoutCrawler:
    """웍스아웃(worksout.co.kr) 크롤러 — REST API."""

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

    async def search_products(self, keyword: str, limit: int = 20) -> list[dict]:
        """키워드로 상품 검색."""
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        params = {
            "searchKeyword": keyword,
            "size": min(limit, 20),
            "page": 0,
            "mainCategoryId": 37,
        }

        client = await self._get_client()
        async with self._rate_limiter.acquire():
            resp = await client.get(SEARCH_URL, params=params)

        if resp.status_code != 200:
            logger.warning(
                "Worksout 검색 HTTP %d (keyword=%s)", resp.status_code, keyword
            )
            return []

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Worksout 검색 JSON 파싱 실패 (keyword=%s)", keyword)
            return []

        payload = data.get("payload") or {}
        products_page = payload.get("products") or {}
        content = products_page.get("content") or []

        results: list[dict] = []
        for item in content:
            parsed = _parse_search_product(item)
            if parsed is not None:
                results.append(parsed)

        logger.info("Worksout 검색 '%s': %d건", keyword, len(results))
        return results

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 조회."""
        if not product_id:
            return None

        url = f"{DETAIL_URL}/{product_id}/detail"
        params = {"sourceSite": "WORKSOUT"}

        client = await self._get_client()
        async with self._rate_limiter.acquire():
            resp = await client.get(url, params=params)

        if resp.status_code != 200:
            logger.warning(
                "Worksout 상세 HTTP %d (id=%s)", resp.status_code, product_id
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        # 상세 API 응답은 payload 안에 있을 수도 있고, 직접 최상위일 수도 있음
        detail = data.get("payload") or data
        if isinstance(detail, dict) and "productCode" not in detail:
            # 중첩 구조가 아닌 경우
            detail = data

        brand = (detail.get("brandName") or "").strip()
        name = (detail.get("productName") or "").strip()
        korean_name = (detail.get("productKoreanName") or "").strip()
        display_name = name or korean_name

        current_price = int(detail.get("currentPrice") or 0)
        initial_price = int(detail.get("initialPrice") or 0)

        # 이미지
        image_url = ""
        image_list = detail.get("imageUrls") or detail.get("productImages") or []
        if isinstance(image_list, list) and image_list:
            first_img = image_list[0]
            if isinstance(first_img, str):
                image_url = first_img
            elif isinstance(first_img, dict):
                image_url = first_img.get("imageUrl") or first_img.get("url") or ""

        # 사이즈 파싱
        sizes_raw = detail.get("productSizes") or detail.get("sizes") or []
        parsed_sizes = _parse_sizes(sizes_raw)

        # RetailSizeInfo 변환 (재고 있는 사이즈만)
        sizes: list[RetailSizeInfo] = []
        for s in parsed_sizes:
            if not s["in_stock"]:
                continue
            opt_price = current_price
            orig = initial_price or opt_price
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

        product = RetailProduct(
            source="worksout",
            product_id=str(product_id),
            name=display_name,
            model_number="",
            brand=brand,
            url=f"{WEB_BASE}/product/{product_id}",
            image_url=image_url,
            sizes=sizes,
            fetched_at=datetime.now(),
        )

        logger.info(
            "Worksout 상품: %s | 브랜드: %s | %s원 | 사이즈: %d개",
            display_name,
            brand,
            f"{current_price:,}" if current_price else "?",
            len(sizes),
        )
        return product

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("웍스아웃 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
worksout_crawler = WorksoutCrawler()
register("worksout", worksout_crawler, "웍스아웃")
