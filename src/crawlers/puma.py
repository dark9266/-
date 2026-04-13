"""푸마 한국 공식몰(kr.puma.com) 크롤러.

Salesforce Commerce Cloud (SFRA) 기반. `Search-UpdateGrid` 그리드
엔드포인트를 GET 페이지네이션으로 호출해 PLP(product listing page)를
받고, 응답 HTML 안의 ``data-puma-analytics-g4`` 속성에 embed 된 GA4
ecommerce JSON 을 파싱하면 상품 메타(item_id = style_number = 모델번호,
item_name, price, item_brand, orderable, color 등) 를 한 번에 얻을 수 있다.

Puma 모델번호 패턴
------------------
* 6자리 숫자 + ``_`` + 2자리 숫자 (예: ``403767_03``)
* 크림 DB 는 같은 번호를 ``403767-03`` 하이픈 포맷으로 보관
* ``normalize_model_number`` 가 공백·하이픈만 정규화하므로,
  어댑터 매칭 단계에서 ``_`` 를 ``-`` 로 치환해 동일 키로 맞춘다.

설계 원칙
---------
* 읽기 전용 GET 만 사용 — SFCC 그리드 엔드포인트는 HTML 응답이지만
  GET 파라미터만 바꿔 카테고리/검색/페이지 조회 가능.
* `search_products` / `get_product_detail` / `fetch_category_grid` 3 메서드.
  어댑터(`PumaAdapter`) 는 카탈로그 덤프에 `fetch_category_grid` 만 사용.
* Rate limit: 2 concurrent, min_interval 2.0s.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("puma_crawler")

BASE_URL = "https://kr.puma.com"
GRID_PATH = "/on/demandware.store/Sites-KR-Site/ko_KR/Search-UpdateGrid"
SEARCH_SHOW_PATH = "/on/demandware.store/Sites-KR-Site/ko_KR/Search-Show"
GRID_URL = f"{BASE_URL}{GRID_PATH}"
SEARCH_SHOW_URL = f"{BASE_URL}{SEARCH_SHOW_PATH}"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

# Puma 모델번호: 6자리_2자리 (소싱 응답) 또는 6자리-2자리 (크림 DB)
PUMA_STYLE_RE = re.compile(r"\b(\d{6})[_-](\d{2})\b")

# PLP 페이지당 상품 수 (SFCC 기본 48)
DEFAULT_PAGE_SIZE = 48


# --------------- 순수 파싱 함수 ---------------


def _extract_ga4_items(html_text: str) -> list[dict]:
    """`data-puma-analytics-g4` 속성에서 GA4 ecommerce items 리스트를 전부 추출.

    응답 HTML 안에는 1개 이상의 `data-puma-analytics-g4` 속성이 존재할 수
    있고, 각 속성 값은 HTML-entity 인코딩된 JSON 문자열이다. 페이지 단위
    `view_item_list` 이벤트 하나에 전체 아이템이 담기는 게 정상이지만
    개별 product-tile 에도 단건 payload 가 붙을 수 있으므로 모두 모은다.
    """
    if not html_text:
        return []
    out: list[dict] = []
    for raw in re.findall(r'data-puma-analytics-g4="([^"]+)"', html_text):
        try:
            payload = json.loads(html_lib.unescape(raw))
        except (ValueError, TypeError):
            continue
        ecommerce = payload.get("ecommerce") if isinstance(payload, dict) else None
        if not isinstance(ecommerce, dict):
            continue
        items = ecommerce.get("items") or []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    out.append(it)
    return out


def _normalize_style(raw: str) -> str:
    """``403767_03`` 또는 ``403767-03`` → ``403767-03`` (크림 DB 포맷)."""
    if not raw:
        return ""
    raw = raw.strip()
    m = PUMA_STYLE_RE.search(raw)
    if not m:
        return ""
    return f"{m.group(1)}-{m.group(2)}"


def _ga4_item_to_product(item: dict) -> dict | None:
    """GA4 ecommerce item → 표준 search-result dict.

    필수 키 누락 시 None. 품절(orderable=false, availability=false) 도
    일단 파싱해서 돌려주고, 필터는 어댑터에서 수행.
    """
    if not isinstance(item, dict):
        return None

    raw_style = str(item.get("style_number") or item.get("item_id") or "")
    model_number = _normalize_style(raw_style)
    if not model_number:
        return None

    name_raw = str(item.get("item_name") or "")
    # item_name 에는 `<br>` 등 HTML 태그가 섞여 있을 수 있다.
    name = re.sub(r"<[^>]+>", " ", name_raw).strip()

    # 가격: int 또는 "159000" 문자열
    price_raw = item.get("price")
    try:
        price = int(float(price_raw)) if price_raw not in (None, "", "undefined") else 0
    except (TypeError, ValueError):
        price = 0

    full_price_raw = item.get("full_price")
    try:
        original_price = (
            int(float(full_price_raw))
            if full_price_raw not in (None, "", "undefined", "0.00")
            else price
        )
    except (TypeError, ValueError):
        original_price = price
    if not original_price:
        original_price = price

    # 재고/품절 판정 — 아래 4개 중 하나라도 "false" 면 품절 간주.
    def _flag(v: object) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() not in ("false", "0", "no", "n")
        return bool(v)

    availability = _flag(item.get("availability", True))
    orderable = _flag(item.get("orderable", True))
    assortment = str(item.get("assortment_availability") or "available").lower()
    in_stock = availability and orderable and assortment == "available"

    brand = str(item.get("item_brand") or item.get("affiliation") or "Puma")
    color = str(item.get("color") or "")

    # PDP URL 은 SFCC 규약상 style_number 만으로는 재구성 불가 →
    # 검색 경로로 이동하는 안전 URL 사용 (모델번호 검색 → 1건 매칭).
    url = f"{BASE_URL}{SEARCH_SHOW_PATH}?q={model_number}"

    return {
        "product_id": model_number,
        "name": name,
        "brand": brand,
        "model_number": model_number,
        "price": price,
        "original_price": original_price,
        "url": url,
        "image_url": "",
        "color": color,
        "is_sold_out": not in_stock,
        "available": in_stock,
        "sizes": [],  # 그리드 응답엔 사이즈별 재고 정보 없음
    }


# --------------- 크롤러 클래스 ---------------


class PumaCrawler:
    """푸마 한국 공식몰 크롤러 (SFCC Search-UpdateGrid GET).

    본 크롤러는 푸시 파이프라인의 `PumaAdapter` 가 사용하는 HTTP 레이어.
    기존 스캐너/역방향 파이프라인과의 호환을 위해 `search_products` /
    `get_product_detail` 도 구현하지만 주력 메서드는 `fetch_category_grid`.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=2.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=20.0,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    # ------------------------------------------------------------------
    # 카테고리 PLP 페이지네이션 — 어댑터 주력 경로
    # ------------------------------------------------------------------
    async def fetch_category_grid(
        self,
        cgid: str,
        *,
        start: int = 0,
        sz: int = DEFAULT_PAGE_SIZE,
    ) -> list[dict]:
        """SFCC `Search-UpdateGrid?cgid=...&start=N&sz=M` 호출.

        Parameters
        ----------
        cgid:
            카테고리 ID (예: ``mens-shoes``, ``womens-shoes``).
        start:
            페이지 오프셋. 기본 0, 다음 페이지는 +sz.
        sz:
            페이지당 상품 수. 기본 48.

        Returns
        -------
        list[dict]
            `_ga4_item_to_product` 로 변환된 표준 상품 dict 리스트.
        """
        params = {"cgid": cgid, "start": str(start), "sz": str(sz)}
        client = await self._get_client()
        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(GRID_URL, params=params)
            except httpx.HTTPError as e:
                logger.warning(
                    "푸마 그리드 HTTP 오류: %s (cgid=%s start=%d)", e, cgid, start
                )
                return []

        if resp.status_code != 200:
            logger.warning(
                "푸마 그리드 HTTP %d (cgid=%s start=%d)",
                resp.status_code,
                cgid,
                start,
            )
            return []

        items = _extract_ga4_items(resp.text)
        out: list[dict] = []
        for it in items:
            parsed = _ga4_item_to_product(it)
            if parsed is not None:
                out.append(parsed)
        logger.info(
            "푸마 그리드 cgid=%s start=%d: %d건", cgid, start, len(out)
        )
        return out

    # ------------------------------------------------------------------
    # 키워드 검색 — 기존 스캐너 호환용 (SFCC Search-UpdateGrid?q=...)
    # ------------------------------------------------------------------
    async def search_products(
        self, keyword: str, limit: int = 48
    ) -> list[dict]:
        """키워드(모델번호/상품명)로 검색.

        SFCC Search-UpdateGrid 에 q 파라미터를 넘긴다.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        params = {"q": keyword, "start": "0", "sz": str(limit)}
        client = await self._get_client()
        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(GRID_URL, params=params)
            except httpx.HTTPError as e:
                logger.warning("푸마 검색 HTTP 오류: %s (q=%s)", e, keyword)
                return []

        if resp.status_code != 200:
            logger.warning("푸마 검색 HTTP %d (q=%s)", resp.status_code, keyword)
            return []

        items = _extract_ga4_items(resp.text)
        out: list[dict] = []
        for it in items:
            parsed = _ga4_item_to_product(it)
            if parsed is not None:
                out.append(parsed)
        logger.info("푸마 검색 '%s': %d건", keyword, len(out))
        return out

    # ------------------------------------------------------------------
    # 상세 — 그리드만으로 사이즈별 재고를 모르므로 경량 구현
    # ------------------------------------------------------------------
    async def get_product_detail(
        self, product_id: str
    ) -> RetailProduct | None:
        """상품 상세 조회.

        product_id = 정규화 모델번호(``403767-03``). 현재는 검색 결과로
        대체해 최소 정보만 반환 (사이즈별 재고 없음).
        """
        if not product_id:
            return None
        results = await self.search_products(product_id, limit=5)
        if not results:
            return None
        target = None
        for r in results:
            if r.get("model_number") == product_id:
                target = r
                break
        if target is None:
            target = results[0]

        sizes: list[RetailSizeInfo] = []  # 그리드 경로에선 제공되지 않음
        return RetailProduct(
            source="puma",
            product_id=target["product_id"],
            name=target["name"],
            model_number=target["model_number"],
            brand=target["brand"],
            url=target["url"],
            image_url=target.get("image_url", ""),
            sizes=sizes,
            fetched_at=datetime.now(),
        )

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("푸마 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
puma_crawler = PumaCrawler()
register("puma", puma_crawler, "푸마")
