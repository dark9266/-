"""W컨셉(wconcept.co.kr) 크롤러.

검색: POST /display/api/v3/search/result/product (읽기 전용 POST — 상품 검색)
상세: GET /Product/{itemCd} (HTML — 모델번호, 가격, 사이즈별 재고)
DISPLAY-API-KEY 헤더 필수.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("wconcept_crawler")

BASE_URL = "https://www.wconcept.co.kr"
API_BASE = "https://api-display.wconcept.co.kr"

# W컨셉 프론트 게이트웨이 API 키 (공개, 프론트엔드 JS에 하드코딩)
DISPLAY_API_KEY = "VWmkUPgs6g2fviPZ5JQFQ3pERP4tIXv/J2jppLqSRBk="

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "DISPLAY-API-KEY": DISPLAY_API_KEY,
    "Content-Type": "application/json",
}

HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/",
}

# 모델번호 추출: 상품명에서 [XXX-XXX] 패턴 또는 끝부분 품번
_BRACKET_MODEL_RE = re.compile(r"\[([A-Z0-9]+-[A-Z0-9]+)\]")
# 하이픈 포함: HQ2409-001, 1203A537-110 등
_MODEL_HYPHEN_RE = re.compile(r"(?=[A-Z0-9]*\d)[A-Z0-9]{2,10}-\d{2,4}")
# 하이픈 없음: IE4195, VN000BW5BKA
_MODEL_NOHYPHEN_RE = re.compile(r"[A-Z]{1,3}\d[A-Z0-9]{3,9}")

# HTML 파싱: saleprice
_SALE_PRICE_RE = re.compile(r'name="saleprice"\s+value="(\d+)"')
_ORIGINAL_PRICE_RE = re.compile(r'name="originalPrice"\s+value="(\d+)"')

# statusCd: 01=판매중, 04=품절
_STATUS_RE = re.compile(r"var\s+statusCd\s*=\s*'(\d+)'")

# 사이즈 옵션: <option value="..." optionvalue="220">220</option>
_SIZE_OPTION_RE = re.compile(
    r'<option\s[^>]*value="([^"]*)"[^>]*optionvalue="([^"]*)"[^>]*>',
)

# skuqty 배열
_SKUQTY_RE = re.compile(r"var\s+skuqty\s*=\s*\[([^\]]*)\]")

# brazeJson에서 itemName/brandName 추출
_BRAZE_ITEM_RE = re.compile(r'"itemName"\s*:\s*"([^"]*)"')
_BRAZE_BRAND_RE = re.compile(r'"brandName"\s*:\s*"([^"]*)"')


def _extract_model_number(name: str) -> str:
    """상품명에서 모델번호 추출."""
    if not name:
        return ""

    # 1) [XXX-XXX] 대괄호 패턴
    m = _BRACKET_MODEL_RE.search(name)
    if m:
        return m.group(1)

    # 2) 하이픈 포함 패턴
    matches = _MODEL_HYPHEN_RE.findall(name)
    if matches:
        return matches[-1]

    # 3) 하이픈 없음 패턴
    matches = _MODEL_NOHYPHEN_RE.findall(name)
    if matches:
        return matches[-1]

    return ""


def _parse_search_response(data: dict) -> list[dict]:
    """검색 API JSON 응답 파싱."""
    results: list[dict] = []

    # 신규 응답: data.productList.content
    product_list = data.get("data", {}).get("productList", {})
    items = product_list.get("content", []) if isinstance(product_list, dict) else []
    if not items:
        # 구버전 호환
        items = data.get("data", {}).get("product", {}).get("items", [])
    if not items:
        items = data.get("data", {}).get("items", [])

    for item in items:
        if not isinstance(item, dict):
            continue

        item_cd = str(item.get("itemCd", item.get("itemNo", "")))
        if not item_cd:
            continue

        name = item.get("itemName", item.get("itemNm", ""))
        brand = (
            item.get("brandNameKr")
            or item.get("brandNameEn")
            or item.get("brandName", item.get("brandNm", ""))
        )
        sale_price = item.get("salePrice", item.get("lastSalePrice", 0))
        original_price = item.get("customerPrice", item.get("originalPrice", 0))
        image_url = item.get("imageUrlMobile", item.get("imageUrl", ""))
        # statusCd 04 = 품절
        status_cd = str(item.get("statusCd", "01"))
        sold_out = status_cd == "04" or item.get("soldOutYn", "N") == "Y"

        model_number = _extract_model_number(name)

        results.append({
            "product_id": item_cd,
            "name": name,
            "brand": brand,
            "model_number": model_number,
            "price": int(sale_price) if sale_price else 0,
            "original_price": int(original_price) if original_price else 0,
            "url": f"{BASE_URL}/Product/{item_cd}",
            "image_url": image_url or "",
            "is_sold_out": sold_out,
        })

    return results


def _parse_detail_html(html_text: str) -> dict:
    """상세 페이지 HTML 파싱 → 가격, 모델번호, 브랜드, 사이즈별 재고."""
    result: dict = {
        "name": "",
        "brand": "",
        "model_number": "",
        "price": 0,
        "original_price": 0,
        "is_sold_out": False,
        "sizes": [],
    }

    # brazeJson에서 이름/브랜드
    name_m = _BRAZE_ITEM_RE.search(html_text)
    if name_m:
        result["name"] = name_m.group(1)
    brand_m = _BRAZE_BRAND_RE.search(html_text)
    if brand_m:
        result["brand"] = brand_m.group(1)

    result["model_number"] = _extract_model_number(result["name"])

    # 가격
    price_m = _SALE_PRICE_RE.search(html_text)
    if price_m:
        result["price"] = int(price_m.group(1))
    orig_m = _ORIGINAL_PRICE_RE.search(html_text)
    if orig_m:
        result["original_price"] = int(orig_m.group(1))

    # 판매 상태
    status_m = _STATUS_RE.search(html_text)
    if status_m and status_m.group(1) != "01":
        result["is_sold_out"] = True

    # 사이즈 옵션 추출
    size_options = _SIZE_OPTION_RE.findall(html_text)

    # skuqty 배열
    qty_m = _SKUQTY_RE.search(html_text)
    quantities: list[int] = []
    if qty_m:
        qty_str = qty_m.group(1).strip()
        if qty_str:
            quantities = [int(q.strip()) for q in qty_str.split(",") if q.strip()]

    # 사이즈-재고 매핑
    sizes: list[dict] = []
    for i, (value, option_value) in enumerate(size_options):
        raw_label = html.unescape(option_value.strip() or value.strip())
        if not raw_label:
            continue
        # "화이트_225" → "225", "260" → "260"
        size_num = re.search(r"(\d{2,4}(?:\.\d)?)", raw_label)
        size_label = size_num.group(1) if size_num else raw_label
        qty = quantities[i] if i < len(quantities) else 0
        sizes.append({
            "size": size_label,
            "in_stock": qty > 0,
            "qty": qty,
        })

    result["sizes"] = sizes
    return result


class WconceptCrawler:
    """W컨셉 (wconcept.co.kr) 크롤러."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=2.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("W컨셉 크롤러 연결 해제")

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """모델번호로 W컨셉 검색.

        POST /display/api/v3/search/result/product (읽기 전용).
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        client = await self._get_client()
        payload = {
            "keyword": keyword,
            "gender": "unisex",
            "pageNo": 1,
            "pageSize": min(limit, 40),
            "sort": "NEW",
            "searchType": "KEYWORD",
            "platform": "PC",
        }

        async with self._rate_limiter.acquire():
            try:
                resp = await client.post(
                    f"{API_BASE}/display/api/v3/search/result/product",
                    headers=HEADERS,
                    content=json.dumps(payload),
                )
            except httpx.HTTPError as exc:
                logger.warning("W컨셉 검색 HTTP 에러: %s", exc)
                return []

        if resp.status_code != 200:
            logger.warning("W컨셉 검색 HTTP %d (%s)", resp.status_code, keyword)
            return []

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.warning("W컨셉 검색 JSON 파싱 실패 (%s)", keyword)
            return []

        results = _parse_search_response(data)
        logger.info("W컨셉 검색 '%s': %d건", keyword, len(results))
        return results[:limit]

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 + 사이즈별 재고.

        GET /Product/{itemCd} → HTML 파싱.
        """
        if not product_id:
            return None

        client = await self._get_client()

        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(
                    f"{BASE_URL}/Product/{product_id}",
                    headers=HTML_HEADERS,
                )
            except httpx.HTTPError as exc:
                logger.error("W컨셉 상세 HTTP 에러 (%s): %s", product_id, exc)
                return None

        if resp.status_code != 200:
            logger.warning("W컨셉 상세 HTTP %d (%s)", resp.status_code, product_id)
            return None

        detail = _parse_detail_html(resp.text)

        if detail["is_sold_out"]:
            logger.info("W컨셉 품절: %s (%s)", detail["name"], product_id)
            return None

        sizes: list[RetailSizeInfo] = []
        for s in detail["sizes"]:
            if not s["in_stock"]:
                continue
            sizes.append(
                RetailSizeInfo(
                    size=s["size"],
                    price=detail["price"],
                    original_price=detail["original_price"],
                    in_stock=True,
                    discount_type="세일" if detail["price"] < detail["original_price"] else "",
                    discount_rate=(
                        round(1 - detail["price"] / detail["original_price"], 2)
                        if detail["original_price"] > 0
                        and detail["price"] < detail["original_price"]
                        else 0.0
                    ),
                )
            )

        product = RetailProduct(
            source="wconcept",
            product_id=str(product_id),
            name=detail["name"],
            model_number=detail["model_number"],
            brand=detail["brand"],
            url=f"{BASE_URL}/Product/{product_id}",
            image_url="",
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "W컨셉 상품: %s | 모델: %s | %s원 | 사이즈: %d개",
            detail["name"],
            detail["model_number"],
            f"{detail['price']:,}" if detail["price"] else "?",
            len(sizes),
        )
        return product


# 싱글톤 + 레지스트리 등록
wconcept_crawler = WconceptCrawler()
register("wconcept", wconcept_crawler, "W컨셉")
