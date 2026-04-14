"""아디다스 공식몰 (adidas.co.kr) 크롤러.

아디다스 KR 검색 taxonomy API로 상품 정보를 수집한다.
검색: /api/search/taxonomy?query={keyword} (JSON API, 사이즈/가격 포함)
상세: 검색 결과에 사이즈/가격이 포함되어 별도 상세 조회 불필요.
"""

import random
from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("adidas_crawler")

BASE_URL = "https://www.adidas.co.kr"
SEARCH_API = BASE_URL + "/api/search/taxonomy"
PRODUCT_PAGE_URL = BASE_URL + "/{product_id}.html"

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


def _build_headers() -> dict:
    """Akamai WAF 우회를 위한 브라우저 유사 헤더."""
    return {
        "User-Agent": _random_ua(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.adidas.co.kr/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def _parse_items(data: dict) -> list[dict]:
    """taxonomy API 응답에서 상품 목록 파싱.

    응답 구조: {"itemList": {"items": [...], "count": N, ...}}
    """
    item_list = data.get("itemList", {})
    items = item_list.get("items", [])
    results = []

    for item in items:
        product_id = item.get("productId", "")
        if not product_id:
            continue

        display_name = item.get("displayName", "")
        price = int(item.get("price", 0) or 0)
        sale_price = int(item.get("salePrice", 0) or 0) or price
        link = item.get("link", "")
        url = BASE_URL + link if link else PRODUCT_PAGE_URL.format(product_id=product_id)

        # 이미지 URL
        image_info = item.get("image", {})
        image_url = image_info.get("src", "") if isinstance(image_info, dict) else ""

        # 사이즈 ("hidden" 엔트리 제외)
        raw_sizes = item.get("availableSizes", [])
        sizes = [s for s in raw_sizes if s and s != "hidden"]

        results.append({
            "product_id": product_id,
            "name": display_name,
            "brand": "adidas",
            "model_number": product_id,
            "price": sale_price,
            "original_price": price,
            "url": url,
            "image_url": image_url,
            "is_sold_out": not item.get("orderable", True),
            "sizes": sizes,
        })

    return results


class AdidasCrawler:
    """아디다스 공식몰 크롤러."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=5.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=_build_headers(),
                timeout=15,
                follow_redirects=True,
            )
        return self._client

    async def search_products(self, keyword: str, limit: int = 48) -> list[dict]:
        """아디다스 KR taxonomy API 검색.

        Returns:
            [{"product_id": str, "name": str, "brand": str, "model_number": str,
              "price": int, "original_price": int, "url": str, ...}, ...]
        """
        client = await self._get_client()

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(
                    SEARCH_API,
                    params={"query": keyword, "start": "0"},
                    headers=_build_headers(),
                )

            if resp.status_code == 403:
                logger.warning("아디다스 검색 차단 (403): %s", keyword)
                return []
            if resp.status_code != 200:
                logger.warning("아디다스 검색 실패 (HTTP %d): %s", resp.status_code, keyword)
                return []

            data = resp.json()
            results = _parse_items(data)
            logger.info("아디다스 검색 '%s': %d건", keyword, len(results))
            return results[:limit]

        except Exception as e:
            logger.error("아디다스 검색 에러 (%s): %s", keyword, e)
            return []

    async def fetch_taxonomy_page(
        self,
        *,
        category: str,
        page_size: int = 48,
        page_number: int = 1,
    ) -> dict:
        """카테고리 페이지네이션 덤프 — `AdidasAdapter.dump_catalog` 전용.

        adidas KR taxonomy API 는 `query` 에 카테고리 slug(예: ``men-shoes``)
        를 그대로 넘기고 `start` 로 offset 페이지네이션한다. 응답의
        `itemList.items` 를 어댑터가 기대하는 정규화 dict 리스트로 변환한 뒤
        `{"items": [...], "totalCount": N}` 형태로 감싸 반환한다.

        Parameters
        ----------
        category:
            adidas 카테고리 slug (예: ``men-shoes`` / ``women-shoes`` /
            ``kids-shoes``).
        page_size:
            페이지당 아이템 수. adidas 기본 48.
        page_number:
            1-base 페이지 번호.

        Returns
        -------
        dict
            ``{"items": list[dict], "totalCount": int}``. items 는
            `_parse_items` 결과(`product_id`/`name`/`price`/`sizes`/...) 와
            동일하며 어댑터가 카테고리 메타를 추가한다. 실패/차단 시
            ``{"items": [], "totalCount": 0}`` 반환.
        """
        client = await self._get_client()
        start = max(0, (int(page_number) - 1) * int(page_size))

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(
                    SEARCH_API,
                    params={"query": category, "start": str(start)},
                    headers=_build_headers(),
                )

            if resp.status_code == 403:
                logger.warning(
                    "아디다스 카테고리 덤프 차단(403): %s start=%d", category, start,
                )
                return {"items": [], "totalCount": 0}
            if resp.status_code != 200:
                logger.warning(
                    "아디다스 카테고리 덤프 실패(HTTP %d): %s start=%d",
                    resp.status_code, category, start,
                )
                return {"items": [], "totalCount": 0}

            data = resp.json()
            items = _parse_items(data)
            item_list = data.get("itemList") or {}
            total_count = int(item_list.get("count") or 0)
            logger.info(
                "아디다스 카탈로그 '%s' page=%d: %d건 (total=%d)",
                category, page_number, len(items), total_count,
            )
            return {"items": items, "totalCount": total_count}

        except Exception as e:
            logger.error(
                "아디다스 카탈로그 덤프 에러 (%s page=%d): %s",
                category, page_number, e,
            )
            return {"items": [], "totalCount": 0}

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """taxonomy API에서 상품 상세 조회.

        productId로 검색하여 사이즈/가격 정보를 수집한다.
        별도 상세 페이지 접근 불필요 (taxonomy API에 사이즈 포함).
        """
        client = await self._get_client()

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(
                    SEARCH_API,
                    params={"query": product_id, "start": "0"},
                    headers=_build_headers(),
                )

            if resp.status_code != 200:
                logger.warning(
                    "아디다스 상품 조회 실패 (HTTP %d): %s", resp.status_code, product_id,
                )
                return None

            data = resp.json()
            items = _parse_items(data)

            # productId가 정확히 일치하는 상품 찾기
            target = None
            for item in items:
                if item["product_id"] == product_id:
                    target = item
                    break

            if not target:
                logger.warning("아디다스 상품 미발견: %s", product_id)
                return None

            # 사이즈 정보 → RetailSizeInfo 변환
            price = target["price"]
            original_price = target["original_price"]
            sizes = []

            for size_val in target.get("sizes", []):
                discount_rate = 0.0
                if original_price and price and original_price > price:
                    discount_rate = round(1 - price / original_price, 3)

                sizes.append(RetailSizeInfo(
                    size=size_val,
                    price=price,
                    original_price=original_price,
                    in_stock=True,
                    discount_type="할인" if discount_rate > 0 else "",
                    discount_rate=discount_rate,
                ))

            product = RetailProduct(
                source="adidas",
                product_id=product_id,
                name=target["name"],
                model_number=product_id,
                brand="adidas",
                url=target["url"],
                image_url=target.get("image_url", ""),
                sizes=sizes,
                fetched_at=datetime.now(),
            )

            logger.info(
                "아디다스 상품: %s | 모델: %s | 가격: %s원 | 사이즈: %d개",
                target["name"], product_id,
                f"{price:,}" if price else "?",
                len(sizes),
            )
            return product

        except Exception as e:
            logger.error("아디다스 상품 조회 에러 (%s): %s", product_id, e)
            return None

    async def disconnect(self) -> None:
        """클라이언트 종료."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("아디다스 크롤러 연결 해제")


# 싱글톤
adidas_crawler = AdidasCrawler()
register("adidas", adidas_crawler, "아디다스")
