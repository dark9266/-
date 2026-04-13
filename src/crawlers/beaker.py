"""BEAKER (비이커) 크롤러.

BEAKER 는 **삼성물산 SSF Shop** 산하 편집숍 (beaker 브랜드 샵).
주소: https://www.ssfshop.com/beaker/ — beakerBrndShopId=MCBR, brandShopNo=BDMA09.
(도메인 `beaker.co.kr` 는 빈 frameset 셸 — 실 서비스는 ssfshop.com.)

플랫폼 주의
------------
- SSF Shop 은 **한섬(EQL) 백엔드가 아니다.** 이름 일부 컬럼명
  (`godNo`·`godNm`·`brndNm`·`lastSalePrc`·`sizeItmNm`·`onlineUsefulInvQty`)
  이 한섬 계열과 유사하지만, 페이지 구조·검색 엔드포인트는 별도 구현이다.
- 검색은 SPA 프론트가 Ajax 로 `/selectProductList` 를 호출해 SSR HTML 조각을
  돌려받는 구조. `currentPage` 파라미터로 페이지네이션한다.
- 카탈로그 덤프 = **BEAKER 브랜드샵 카테고리 리스팅** 을 `/selectProductList`
  로 페이지네이션.
- 상품 상세는 `/public/goods/detail/{godNo}/view` 에서 사이즈별 `sizeItmNo`
  hidden input 의 `onlineUsefulInvQty` 속성으로 실재고를 준다.

Kream 매칭 기대치
------------------
BEAKER 는 여성복/잡화/뷰티 위주라 스니커 모델번호가 거의 없다. EQL 과
동일한 `_extract_model_from_name` 전략으로 **상품명 끝 모델번호** 를
뽑아내지만 실매칭은 살로몬/아식스 등 드물게 취급하는 런닝화 라인에만
걸릴 가능성이 크다. 덤프는 그대로 유지하고 모델번호 없는 건
`no_model_number` 로 집계된다.

안전
-----
- GET only. POST·상태변경 금지.
- Rate limit: max_concurrent=2, min_interval=1.5.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

import httpx

from src.crawlers.eql import (
    _extract_model_from_name as _eql_extract_model,
)
from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("beaker_crawler")

BASE_URL = "https://www.ssfshop.com"

# BEAKER 브랜드 샵 공통 파라미터.
BRAND_SHOP_NO = "BDMA09"
BRND_SHOP_ID = "MCBR"

# BEAKER 주요 카테고리 — 여성/남성/백앤슈즈/뷰티/라이프. 키워드별
# 매핑은 어댑터가 _brand_keyword → dspCtgryNo 로 해석한다.
CATEGORY_MAP: dict[str, str] = {
    "BEAKER": "SFMA44A02",         # WOMEN (기본)
    "BEAKER_WOMEN": "SFMA44A02",
    "BEAKER_MEN": "SFMA41",
    "BEAKER_BAG_SHOES": "SFMA46",
    "BEAKER_BEAUTY": "SFMA45",
}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/beaker/",
}

# ─── 검색/리스팅 HTML 정규식 ──────────────────────────────────

# <li data-prdno="GM00..." ... class="god-item">
_ITEM_RE = re.compile(
    r'<li[^>]*data-prdno="(GM\d+)"[^>]*class="[^"]*god-item[^"]*"'
    r'(?P<body>.*?)</li>',
    re.DOTALL,
)

# <a href="/{slug}/{godNo}/good?..."> — slug 는 영숫자/하이픈/괄호 혼합
_ITEM_URL_RE = re.compile(r'href="(/[^"]+/GM\d+/good[^"]*)"')

# <span class="brand">...</span>
_BRAND_RE = re.compile(r'<span\s+class="brand"[^>]*>\s*([^<]+)', re.DOTALL)

# <span class="name">...</span>
_NAME_RE = re.compile(r'<span\s+class="name"[^>]*>\s*([^<]+)', re.DOTALL)

# 가격: 최종가는 할인가가 있으면 <del>...</del> 뒤의 숫자, 없으면 <span class="price">
# 뒤쪽 `판매가` 바로 앞 숫자가 확정 판매가.
_FINAL_PRICE_RE = re.compile(
    r'</del>.*?([\d,]+)\s*<span[^>]*>원',
    re.DOTALL,
)
_PLAIN_PRICE_RE = re.compile(
    r'<span\s+class="price"[^>]*>\s*([\d,]+)\s*<span[^>]*>원',
    re.DOTALL,
)
_DEL_PRICE_RE = re.compile(r'<del>.*?([\d,]+)', re.DOTALL)

# 품절 배지
_SOLDOUT_RE = re.compile(r'(sold[-_ ]?out|품절)', re.IGNORECASE)

# ─── 상세 HTML 정규식 ───────────────────────────────────────

# <input ... id="lastSalePrc" value="147000"/>
_DETAIL_PRICE_RE = re.compile(r'id="lastSalePrc"[^>]*value="(\d+)"')
_DETAIL_NAME_RE = re.compile(r'id="godNm"[^>]*value="([^"]*)"')
_DETAIL_BRAND_RE = re.compile(r'id="brndNm"[^>]*value="([^"]*)"')

# 사이즈: 동일 <input name="sizeItmNo" ...> 태그에 속성들이 몰려있다.
#   sizeItmStdSizeCd="M" sizeItmNm="002" itmStatCd="SALE_PROGRS"
#   dlvMsgTxt="" onlineUsefulInvQty="40"
_SSF_SIZE_INPUT_RE = re.compile(
    r'<input[^>]*name="sizeItmNo"[^>]*>',
    re.DOTALL,
)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_listing_html(html_text: str) -> list[dict]:
    """`/selectProductList` 응답 HTML → dict 리스트.

    각 dict 필드: product_id, name, brand, model_number, price,
    original_price, url, image_url, is_sold_out.
    """
    results: list[dict] = []
    for m in _ITEM_RE.finditer(html_text):
        god_no = m.group(1)
        body = m.group("body")

        url_m = _ITEM_URL_RE.search(body)
        rel = url_m.group(1) if url_m else f"/beaker/{god_no}/good"
        url = BASE_URL + html.unescape(rel)

        brand_m = _BRAND_RE.search(body)
        brand = (brand_m.group(1).strip() if brand_m else "").strip()

        name_m = _NAME_RE.search(body)
        name = (name_m.group(1).strip() if name_m else "").strip()

        # 최종가
        price = 0
        fp = _FINAL_PRICE_RE.search(body)
        if fp:
            price = int(fp.group(1).replace(",", ""))
        else:
            pp = _PLAIN_PRICE_RE.search(body)
            if pp:
                price = int(pp.group(1).replace(",", ""))

        # 정가
        original = price
        dp = _DEL_PRICE_RE.search(body)
        if dp:
            try:
                original = int(dp.group(1).replace(",", ""))
            except ValueError:
                original = price

        is_sold_out = bool(_SOLDOUT_RE.search(body))
        # 상품명에서 SKU regex 추출 시도 (나이키/아디다스/NB 콜라보 등 일부 성공).
        # 실패 시 god_no 를 fallback — 크림 DB 매칭은 거의 미스지만 미등재 신상
        # 발견 경로(`kream_collect_queue`) 가 살아나고, 향후 god_no 로 크림에
        # 등재된 경우 pickup 가능. 한섬(`thehandsome.py`) 과 동일한 설계.
        model_number = _eql_extract_model(name) or god_no

        results.append({
            "product_id": god_no,
            "name": name,
            "brand": brand,
            "model_number": model_number,
            "price": price,
            "original_price": original or price,
            "url": url,
            "image_url": "",
            "is_sold_out": is_sold_out,
        })
    return results


def _parse_detail_sizes(html_text: str) -> list[dict]:
    """상세 HTML 의 sizeItmNo input 파싱 → [{size, in_stock, qty}]."""
    rows: list[dict] = []
    for tag in _SSF_SIZE_INPUT_RE.findall(html_text):
        attrs = dict(_ATTR_RE.findall(tag))
        # 판매중이 아닌 옵션은 스킵
        stat = attrs.get("itmStatCd", "")
        if stat and stat != "SALE_PROGRS":
            continue
        size = (
            attrs.get("sizeItmStdSizeCd")
            or attrs.get("sizeItmNm")
            or ""
        ).strip()
        try:
            qty = int(attrs.get("onlineUsefulInvQty") or 0)
        except ValueError:
            qty = 0
        if not size:
            continue
        rows.append({
            "size": size,
            "in_stock": qty > 0,
            "qty": qty,
        })
    return rows


class BeakerCrawler:
    """BEAKER (SSF Shop 산하) 크롤러."""

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
        logger.info("BEAKER 크롤러 연결 해제")

    async def search_products(
        self,
        keyword: str,
        limit: int = 60,
        page_no: int = 1,
    ) -> list[dict]:
        """BEAKER 카테고리 리스팅 페이지네이션.

        keyword 는 CATEGORY_MAP 키 (예: "BEAKER_WOMEN"). 매핑 실패 시 기본
        WOMEN (SFMA44A02) 을 사용한다. 어댑터에서 `_brand_keyword` 로
        카테고리 키를 넘겨 덤프를 돌린다.
        """
        keyword = (keyword or "BEAKER").strip().upper()
        dsp_ctgry_no = CATEGORY_MAP.get(keyword) or CATEGORY_MAP["BEAKER"]

        client = await self._get_client()
        params = {
            "dspCtgryNo": dsp_ctgry_no,
            "brandShopNo": BRAND_SHOP_NO,
            "brndShopId": BRND_SHOP_ID,
            "currentPage": str(max(1, page_no)),
        }

        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(
                    f"{BASE_URL}/selectProductList",
                    params=params,
                )
            except httpx.HTTPError as exc:
                logger.warning("BEAKER 리스팅 HTTP 에러: %s", exc)
                return []

        if resp.status_code != 200:
            logger.warning(
                "BEAKER 리스팅 HTTP %d (%s page=%d)",
                resp.status_code, keyword, page_no,
            )
            return []

        items = _parse_listing_html(resp.text)
        logger.info(
            "BEAKER 리스팅 %s page=%d: %d건",
            keyword, page_no, len(items),
        )
        return items[:limit]

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 + 사이즈별 재고.

        GET /public/goods/detail/{godNo}/view → HTML 파싱.
        """
        if not product_id:
            return None
        client = await self._get_client()

        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(
                    f"{BASE_URL}/public/goods/detail/{product_id}/view"
                )
            except httpx.HTTPError as exc:
                logger.error("BEAKER 상세 HTTP 에러 (%s): %s", product_id, exc)
                return None

        if resp.status_code != 200:
            logger.warning("BEAKER 상세 HTTP %d (%s)", resp.status_code, product_id)
            return None

        html_text = resp.text
        price_m = _DETAIL_PRICE_RE.search(html_text)
        price = int(price_m.group(1)) if price_m else 0

        name_m = _DETAIL_NAME_RE.search(html_text)
        name = name_m.group(1) if name_m else ""

        brand_m = _DETAIL_BRAND_RE.search(html_text)
        brand = brand_m.group(1) if brand_m else ""

        model_number = _eql_extract_model(name)

        sizes: list[RetailSizeInfo] = []
        for r in _parse_detail_sizes(html_text):
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
            source="beaker",
            product_id=str(product_id),
            name=name,
            model_number=model_number,
            brand=brand,
            url=f"{BASE_URL}/public/goods/detail/{product_id}/view",
            image_url="",
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "BEAKER 상품: %s | 모델: %s | %s원 | 사이즈: %d개",
            name, model_number,
            f"{price:,}" if price else "?",
            len(sizes),
        )
        return product


# 싱글톤 + 레지스트리 등록
beaker_crawler = BeakerCrawler()
register("beaker", beaker_crawler, "BEAKER")
