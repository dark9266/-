"""ABC마트 (a-rt.com) 크롤러.

검색: /display/search-word/result-total/list (JSON API, channel=10002 for GrandStage)
상세: /product/info?prdtNo={id} (JSON API, 사이즈/재고/가격 포함)
"""

import random
import re
from datetime import datetime

import httpx

from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("abcmart_crawler")

BASE_URL = "https://abcmart.a-rt.com"
SEARCH_URL = BASE_URL + "/display/search-word/result-total/list"
DETAIL_URL = BASE_URL + "/product/info"
PRODUCT_PAGE_URL = BASE_URL + "/product/new?prdtNo={prdt_no}"

# GrandStage 채널 (신발/스니커즈 위주)
GS_CHANNEL = "10002"

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


def _build_model_number(style_info: str, color_id: str) -> str:
    """STYLE_INFO + COLOR_ID에서 모델번호 조합.

    style_info: "IB7746", color_id: "001" → "IB7746-001"
    color_id가 없거나 RGB 값이면 style_info만 반환.
    """
    if not style_info:
        return ""
    # COLOR_ID가 3자리 숫자면 모델번호 조합
    if color_id and re.match(r"^\d{3}$", color_id):
        return f"{style_info}-{color_id}"
    return style_info


def _parse_option_inline(option_inline: str) -> list[dict]:
    """PRDT_OPTION_INLINE 파싱.

    형식: "240,168,10001/245,59,10001/250,0,10001/"
    → [{"size": "240", "stock": 168, "channel": "10001"}, ...]
    """
    sizes = []
    if not option_inline:
        return sizes
    for part in option_inline.strip().split("/"):
        if not part:
            continue
        fields = part.split(",")
        if len(fields) >= 2:
            stock = int(fields[1]) if fields[1].isdigit() else 0
            sizes.append({
                "size": fields[0],
                "stock": stock,
                "in_stock": stock > 0,
            })
    return sizes


class AbcMartCrawler:
    """ABC마트 크롤러."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=3, min_interval=2.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": _random_ua(),
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=15,
                follow_redirects=True,
                verify=False,
            )
        return self._client

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """ABC마트 GrandStage 채널 검색.

        Returns:
            [{"product_id": str, "name": str, "brand": str, "model_number": str,
              "price": int, "original_price": int, "url": str, ...}, ...]
        """
        client = await self._get_client()

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(SEARCH_URL, params={
                    "searchWord": keyword,
                    "channel": GS_CHANNEL,
                    "page": "1",
                    "perPage": str(limit),
                    "tabGubun": "total",
                }, headers={"User-Agent": _random_ua()})

            if resp.status_code != 200:
                logger.warning("ABC마트 검색 실패 (HTTP %d): %s", resp.status_code, keyword)
                return []

            body = resp.json()
            products = body.get("SEARCH", [])

        except Exception as e:
            logger.error("ABC마트 검색 에러 (%s): %s", keyword, e)
            return []

        results = []
        for p in products:
            prdt_no = str(p.get("PRDT_NO", ""))
            name = p.get("PRDT_NAME", "")
            style = p.get("STYLE_INFO", "")
            color = p.get("COLOR_ID", "")
            model = _build_model_number(style, color)
            sell_price = int(p.get("PRDT_DC_PRICE", 0) or 0)
            normal_price = int(p.get("NRMAL_AMT", 0) or 0)
            is_sold_out = str(p.get("SOLD_OUT", "")).lower() == "y"

            results.append({
                "product_id": prdt_no,
                "name": name,
                "brand": p.get("BRAND_NAME", ""),
                "model_number": model,
                "price": sell_price or normal_price,
                "original_price": normal_price,
                "url": PRODUCT_PAGE_URL.format(prdt_no=prdt_no),
                "image_url": p.get("PRDT_IMAGE_URL", ""),
                "is_sold_out": is_sold_out,
            })

        logger.info("ABC마트 검색 '%s': %d건", keyword, len(results))
        return results[:limit]

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 API에서 사이즈별 가격/재고 수집."""
        client = await self._get_client()

        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(
                    DETAIL_URL, params={"prdtNo": product_id},
                    headers={"User-Agent": _random_ua()},
                )

            if resp.status_code != 200:
                logger.warning(
                    "ABC마트 상품 조회 실패 (HTTP %d): %s", resp.status_code, product_id,
                )
                return None

            data = resp.json()

        except Exception as e:
            logger.error("ABC마트 상품 조회 에러 (%s): %s", product_id, e)
            return None

        # 기본 정보
        name = data.get("prdtName", "")
        style = data.get("styleInfo", "")
        color = data.get("prdtColorInfo", "")
        model_number = _build_model_number(style, color)
        brand_info = data.get("brand", {}) or {}
        brand = brand_info.get("brandName", "")

        # 가격
        price_info = data.get("productPrice", {}) or {}
        normal_price = int(price_info.get("normalAmt", 0) or 0)
        sell_price = int(price_info.get("sellAmt", 0) or 0) or normal_price

        # 사이즈/재고
        options = data.get("productOption", []) or []
        sizes = []
        for opt in options:
            size_val = str(opt.get("optnName", ""))
            orderable = int(opt.get("orderPsbltQty", 0) or 0)
            if not size_val or orderable <= 0:
                continue

            discount_rate = 0.0
            if normal_price and sell_price and normal_price > sell_price:
                discount_rate = round(1 - sell_price / normal_price, 3)

            sizes.append(RetailSizeInfo(
                size=size_val,
                price=sell_price,
                original_price=normal_price,
                in_stock=True,
                discount_type="할인" if discount_rate > 0 else "",
                discount_rate=discount_rate,
            ))

        product = RetailProduct(
            source="abcmart",
            product_id=product_id,
            name=name,
            model_number=model_number,
            brand=brand,
            url=PRODUCT_PAGE_URL.format(prdt_no=product_id),
            image_url="",
            sizes=sizes,
            fetched_at=datetime.now(),
        )

        logger.info(
            "ABC마트 상품: %s | 모델: %s | 가격: %s원 | 사이즈: %d개",
            name, model_number,
            f"{sell_price:,}" if sell_price else "?",
            len(sizes),
        )
        return product

    async def disconnect(self) -> None:
        """클라이언트 종료."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("ABC마트 크롤러 연결 해제")


# 싱글톤 — 레지스트리 미등록 (모델번호 검색 불가로 역방향 매칭 부적합)
abcmart_crawler = AbcMartCrawler()
