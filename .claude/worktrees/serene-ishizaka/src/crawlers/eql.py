"""EQL(eqlstore.com) 크롤러.

한섬 운영 편집숍. HTML 파싱 기반 (정규식, beautifulsoup 미사용).
검색: /public/search/getSearchGodPaging (HTML)
상세: /product/{godNo}/detail (HTML -- 사이즈/재고)
인증/CSRF 불필요.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("eql_crawler")

BASE_URL = "https://www.eqlstore.com"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/",
}

# 모델번호 추출 패턴:
# 1) 하이픈 포함: HQ2409-001, 1203A537-110, DQ9131-001
#    영숫자 혼합 (최소 1자리 숫자 포함) + 하이픈 + 숫자 2~4자리
MODEL_HYPHEN_RE = re.compile(r"(?=[A-Z0-9]*\d)[A-Z0-9]{2,8}-\d{2,4}")
# 2) 하이픈 없음: IE4195, VN000BW5BKA
MODEL_NOHYPHEN_RE = re.compile(r"[A-Z]{1,3}\d[A-Z0-9]{3,9}")

# 검색 HTML 파싱: <a> 태그에서 godNo, godNm, brndNm 속성 추출
_SEARCH_ITEM_RE = re.compile(
    r'<li[^>]*class="([^"]*)"[^>]*>.*?'
    r'<a\s[^>]*'
    r'godNo="([^"]*)"[^>]*'
    r'godNm="([^"]*)"[^>]*'
    r'brndNm="([^"]*)"',
    re.DOTALL,
)

# 검색 HTML 가격: lastSalePrc 속성
_SEARCH_PRICE_RE = re.compile(
    r'godNo="([^"]*)".*?'
    r'(?:lastSalePrc="(\d+)"|<span\s+class="current">([0-9,]+)</span>)',
    re.DOTALL,
)

# 상세 페이지: hidden input 파싱
_DETAIL_PRICE_RE = re.compile(r'id="lastSalePrc"\s+value="(\d+)"')
_DETAIL_NAME_RE = re.compile(r'id="godNm"\s+value="([^"]*)"')
_DETAIL_BRAND_RE = re.compile(r'id="brndNm"\s+value="([^"]*)"')

# 사이즈 파싱: sizeItmNo (onlineUsefulInvQty) + sizeItmNm (value) 쌍
_SIZE_PAIR_RE = re.compile(
    r'name="sizeItmNo"[^>]*onlineUsefulInvQty="(\d+)"[^>]*/>\s*'
    r'<input\s[^>]*name="sizeItmNm"[^>]*value="([^"]*)"',
    re.DOTALL,
)


def _extract_model_from_name(name: str) -> str:
    """상품명 끝에서 모델번호 추출.

    예:
      "WMNS NIKE FIRST SIGHT NOIR HQ2409-001" -> "HQ2409-001"
      "ASICS GEL-KAYANO 14 1203A537-110" -> "1203A537-110"
      "ADIDAS SAMBA OG IE4195" -> "IE4195"
      "VANS OLD SKOOL VN000BW5BKA" -> "VN000BW5BKA"
    """
    if not name:
        return ""

    # 1) 하이픈 포함 패턴 (가장 정확)
    matches = MODEL_HYPHEN_RE.findall(name)
    if matches:
        return matches[-1]  # 끝에 있는 것 우선

    # 2) 하이픈 없음 패턴
    matches = MODEL_NOHYPHEN_RE.findall(name)
    if matches:
        return matches[-1]

    return ""


def _parse_search_html(html: str) -> list[dict]:
    """검색 결과 HTML -> list[dict].

    각 dict: product_id, name, brand, model_number, price, url, is_sold_out
    """
    results: list[dict] = []

    # godNo/godNm/brndNm 추출
    items = _SEARCH_ITEM_RE.findall(html)

    # 가격 매핑: godNo -> price
    price_map: dict[str, int] = {}
    for m in _SEARCH_PRICE_RE.finditer(html):
        god_no = m.group(1)
        if m.group(2):
            price_map[god_no] = int(m.group(2))
        elif m.group(3):
            price_map[god_no] = int(m.group(3).replace(",", ""))

    for li_class, god_no, god_nm, brnd_nm in items:
        if not god_no:
            continue
        is_sold_out = "is_soldout" in li_class
        model_number = _extract_model_from_name(god_nm)
        price = price_map.get(god_no, 0)

        results.append({
            "product_id": god_no,
            "name": god_nm,
            "brand": brnd_nm,
            "model_number": model_number,
            "price": price,
            "original_price": price,
            "url": f"{BASE_URL}/product/{god_no}/detail",
            "image_url": "",
            "is_sold_out": is_sold_out,
        })

    return results


def _parse_detail_sizes(html: str) -> list[dict]:
    """상세 HTML에서 사이즈/재고 파싱.

    sizeItmNo (onlineUsefulInvQty) + sizeItmNm (value) 쌍.
    반환: list[dict] (size, in_stock, qty)
    """
    rows: list[dict] = []
    for qty_str, size_val in _SIZE_PAIR_RE.findall(html):
        qty = int(qty_str)
        rows.append({
            "size": size_val.strip(),
            "in_stock": qty > 0,
            "qty": qty,
        })
    return rows


class EqlCrawler:
    """EQL (eqlstore.com) 크롤러 -- HTML 파싱 기반."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=1.5)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=15.0,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("EQL 크롤러 연결 해제")

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """모델번호로 EQL 검색.

        GET /public/search/getSearchGodPaging -> HTML 파싱.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        client = await self._get_client()
        params = {
            "searchWord": keyword,
            "currentPage": "1",
            "productPage": str(min(limit, 40)),
            "gubunFilter": "SEARCH",
            "excludeSoldoutGodYn": "Y",
        }

        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(
                    f"{BASE_URL}/public/search/getSearchGodPaging",
                    params=params,
                )
            except httpx.HTTPError as exc:
                logger.warning("EQL 검색 HTTP 에러: %s", exc)
                return []

        if resp.status_code != 200:
            logger.warning("EQL 검색 HTTP %d (%s)", resp.status_code, keyword)
            return []

        html = resp.text
        results = _parse_search_html(html)
        logger.info("EQL 검색 '%s': %d건", keyword, len(results))
        return results[:limit]

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 + 사이즈별 재고.

        GET /product/{godNo}/detail -> HTML 파싱.
        """
        if not product_id:
            return None

        client = await self._get_client()

        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(f"{BASE_URL}/product/{product_id}/detail")
            except httpx.HTTPError as exc:
                logger.error("EQL 상세 HTTP 에러 (%s): %s", product_id, exc)
                return None

        if resp.status_code != 200:
            logger.warning("EQL 상세 HTTP %d (%s)", resp.status_code, product_id)
            return None

        html = resp.text

        # 가격
        price_match = _DETAIL_PRICE_RE.search(html)
        price = int(price_match.group(1)) if price_match else 0

        # 상품명
        name_match = _DETAIL_NAME_RE.search(html)
        name = name_match.group(1) if name_match else ""

        # 브랜드
        brand_match = _DETAIL_BRAND_RE.search(html)
        brand = brand_match.group(1) if brand_match else ""

        # 모델번호
        model_number = _extract_model_from_name(name)

        # 사이즈
        size_rows = _parse_detail_sizes(html)
        sizes: list[RetailSizeInfo] = []
        for r in size_rows:
            if not r["in_stock"]:
                continue
            sizes.append(
                RetailSizeInfo(
                    size=r["size"],
                    price=price,
                    original_price=price,
                    in_stock=True,
                    discount_type="",
                    discount_rate=0.0,
                )
            )

        product = RetailProduct(
            source="eql",
            product_id=str(product_id),
            name=name,
            model_number=model_number,
            brand=brand,
            url=f"{BASE_URL}/product/{product_id}/detail",
            image_url="",
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "EQL 상품: %s | 모델: %s | %s원 | 사이즈: %d개",
            name,
            model_number,
            f"{price:,}" if price else "?",
            len(sizes),
        )
        return product


# 싱글톤 + 레지스트리 등록
eql_crawler = EqlCrawler()
register("eql", eql_crawler, "EQL")
