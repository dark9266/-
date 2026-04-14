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

# 검색 HTML 파싱: 실제 EQL 응답은 ``<li  >``(class 속성 없음) → <a class="link_wrap ...">
# 앵커 기반으로 godNo/godNm/brndNm 을 직접 추출한다. 앵커 class 에 `link_wrap`
# 필터를 걸어 하단 favorite button 의 동일 속성과 충돌하지 않게 한다.
_SEARCH_ITEM_RE = re.compile(
    r'<a\s[^>]*class="link_wrap[^"]*"[^>]*?'
    r'godNo="([^"]*)"[^>]*?'
    r'godNm="([^"]*)"[^>]*?'
    r'brndNm="([^"]*)"',
    re.DOTALL,
)

# 품절 마커: ``<li ... class="... is_soldout ...">`` 직후 같은 블록 내 godNo 를
# 찾는다. 실제 EQL 은 현재 class 를 비워두지만 (``<li  >``) 기존 테스트 fixture
# 와 하위호환을 위해 별도 regex 로 soldout godNo 집합을 만들어 lookup 한다.
_SOLDOUT_LI_RE = re.compile(
    r'<li[^>]*class="[^"]*is_soldout[^"]*"[^>]*>.*?godNo="([^"]+)"',
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

    # godNo/godNm/brndNm 추출 (앵커 기반)
    items = _SEARCH_ITEM_RE.findall(html)

    # 품절 godNo 집합 (하위호환)
    soldout_set: set[str] = set(_SOLDOUT_LI_RE.findall(html))

    # 가격 매핑: godNo -> price
    price_map: dict[str, int] = {}
    for m in _SEARCH_PRICE_RE.finditer(html):
        god_no = m.group(1)
        if m.group(2):
            price_map[god_no] = int(m.group(2))
        elif m.group(3):
            price_map[god_no] = int(m.group(3).replace(",", ""))

    seen: set[str] = set()
    for god_no, god_nm, brnd_nm in items:
        if not god_no or god_no in seen:
            continue
        seen.add(god_no)
        is_sold_out = god_no in soldout_set
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

    async def search_products(
        self, keyword: str, limit: int = 30, page_no: int = 1
    ) -> list[dict]:
        """키워드로 EQL 검색.

        GET /public/search/getSearchGodPaging -> HTML 파싱.
        ``page_no`` 는 1-base. 어댑터가 페이지네이션 덤프에 사용.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        # EQL API 파라미터 관찰 (2026-04-14):
        # - ``productPage`` = 페이지 번호 (1, 2, ..., totalPage). 페이지당 고정 40건.
        # - ``currentPage`` = 호환용 필드. 실질 효과 미관측, "1" 고정.
        # - ``excludeSoldoutGodYn`` = 서버가 현재 무시. 품절 감지는 list 마커에 의존.
        # 이전 코드는 ``productPage`` 에 ``min(limit, 40)`` (30) 를 넣어 page 30
        # (totalPage 밖) 조회 → 빈 응답 → 0건으로 관측됐다. limit 은 post-parse 적용.
        client = await self._get_client()
        params = {
            "searchWord": keyword,
            "currentPage": "1",
            "productPage": str(max(1, int(page_no))),
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
