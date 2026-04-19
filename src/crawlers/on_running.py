r"""On Running 한국 공식몰(www.on.com/ko-kr) 크롤러.

사이트맵 기반 카탈로그 덤프 + JSON-LD ProductGroup PDP 파서.

전략
----
* **카탈로그 덤프**: `https://www.on.com/ko-kr/products.xml` 1회 호출로
  전수 URL 목록 획득 (약 1,320개). 페이지네이션 불필요.
  사이트맵 `<url>` 블록에는 `<loc>` 의 한국 URL 외에도 40여 개 다국어
  `<xhtml:link>` 가 섞여 있는데 `<loc>` 만 추출해 중복 없이 처리한다.
* **PDP 파싱**: `<script id="json-ld" type="application/ld+json">` 안에
  `@graph[].@type == "ProductGroup"` 이 들어 있고, `hasVariant[]` 에
  색상별 Product variant 리스트가 있다. 각 variant 는
    - `sku`: 11자리 (신형 `[13][WMU][EFG]\d{4}\d{4}`) 혹은 `NN.XXXXX` (구형)
    - `color`: 예 "Apollo | Eclipse"
    - `offers.availability`: schema.org/InStock | OutOfStock
    - `offers.price` + `offers.priceCurrency=KRW`
  단, **사이즈별 재고는 SSR 로 내려오지 않는다** — 이는 별도의
  spree variants API 가 담당하는데 로그인/리다이렉트로 차단돼 있어
  어댑터는 `available_sizes=()` 로 발행(listing-only 하위호환 경로).
* **SKU 이중 포맷**: 구형 `NN.XXXXX` 는 크림 DB 에 `NN-XXXXX` 로 저장되어
  있으므로 매칭 시 `.` → `-` 정규화. 매칭은 어댑터 레이어에서 수행하고
  크롤러는 원본을 그대로 돌려준다.

차단/안전
--------
* 읽기 전용. GET 만. POST/PUT/DELETE 금지.
* User-Agent 는 표준 Chrome 문자열. Referer/Cookie 불필요.
* Rate limit: 2 concurrent, 1.5s 간격 (보수적).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("on_running_crawler")


BASE_URL = "https://www.on.com"
LOCALE_PATH = "/ko-kr"
SITEMAP_URL = f"{BASE_URL}{LOCALE_PATH}/products.xml"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# ── SKU 포맷 ───────────────────────────────────────────────
# 신형 11자리: 앞 1자리 [1/3], 성별 [WMU], 카테고리 [EFG], 스타일 4자리, 컬러 4자리
#   * `3MF10074109`, `1WF10150553`, `3ME10120664`, `3MF10671043`
# 구형 점 구분: `NN.XXXXX` (예: `61.97657`, `61.99025`)
SKU_NEW_RE = re.compile(r"^[13][WMU][EFG]\d{8}$")
SKU_OLD_RE = re.compile(r"^\d{2}\.\d{4,6}$")

# URL 내 SKU 트레일링 매칭 — `...-shoes-3MF10074109` 또는 `...-apparel-61.97657`
_URL_SKU_RE = re.compile(
    r"/[^/]*?-(?:shoes|apparel|accessories|gear|shoe|clothing)-"
    r"(?P<sku>[13][WMU][EFG]\d{8}|\d{2}\.\d{4,6})\b",
    re.IGNORECASE,
)
# fallback: URL 끝 세그먼트에서 SKU 발췌 (카테고리 토큰 변형 대비)
_URL_SKU_TAIL_RE = re.compile(
    r"-(?P<sku>[13][WMU][EFG]\d{8}|\d{2}\.\d{4,6})(?:/|$|\?)"
)

# URL path 컬러-slug 범위: `...{gender}/{color-slug}-{category}-{SKU}`
_URL_COLOR_RE = re.compile(
    r"/(?:mens|womens|unisex|kids|kid|men|women|boys|girls)/"
    r"(?P<color>[a-z0-9\-]+?)-(?:shoes|apparel|accessories|gear|shoe|clothing)-"
    r"(?:[13][WMU][EFG]\d{8}|\d{2}\.\d{4,6})",
    re.IGNORECASE,
)
_URL_GENDER_RE = re.compile(
    r"/(mens|womens|unisex|kids|kid|men|women|boys|girls)/",
    re.IGNORECASE,
)

# id="json-ld" script 블록 (attrs 순서가 바뀌어도 매칭되도록 양방향)
_LDJSON_RE = re.compile(
    r'<script\b[^>]*\bid="json-ld"[^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
_LDJSON_RE_ALT = re.compile(
    r'<script\b[^>]*\btype="application/ld\+json"[^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)


# ============================================================
# 순수 파싱 함수 — fixture 기반 테스트가 직접 호출
# ============================================================


def parse_sitemap(xml_text: str) -> list[str]:
    """사이트맵 XML → ko-kr 상품 URL 리스트 (dedup, 순서 유지).

    각 `<url>` 블록의 `<loc>` 만 사용. `<xhtml:link rel="alternate">` 은
    다국어 대체 URL 이므로 제외.
    """
    if not xml_text:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    # `<loc>...</loc>` 단일 추출
    for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text):
        url = m.group(1).strip()
        if not url:
            continue
        if LOCALE_PATH + "/products/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def extract_sku_from_url(url: str) -> str:
    """상품 URL → SKU 문자열. 실패 시 빈 문자열.

    URL 포맷:
      * 신형: `/ko-kr/products/cloud-6-m-3mf1007/mens/apollo-eclipse-shoes-3MF10074109`
      * 구형: `/ko-kr/products/cloudmonster-61/mens/alloy-silver-shoes-61.97657`
    """
    if not url:
        return ""
    m = _URL_SKU_RE.search(url)
    if m:
        return m.group("sku").strip().upper()
    m = _URL_SKU_TAIL_RE.search(url)
    if m:
        return m.group("sku").strip().upper()
    return ""


def extract_gender_from_url(url: str) -> str:
    """URL → 성별 토큰 소문자 (mens/womens/unisex/kids). 실패 시 ""."""
    if not url:
        return ""
    m = _URL_GENDER_RE.search(url)
    if not m:
        return ""
    return m.group(1).lower()


def extract_color_slug_from_url(url: str) -> str:
    """URL → 컬러 slug (예: ``apollo-eclipse``). 실패 시 ""."""
    if not url:
        return ""
    m = _URL_COLOR_RE.search(url)
    return m.group("color").strip().lower() if m else ""


def is_valid_sku(sku: str) -> bool:
    """신형 또는 구형 SKU 포맷 충족 여부."""
    if not sku:
        return False
    s = sku.strip().upper()
    return bool(SKU_NEW_RE.match(s) or SKU_OLD_RE.match(s))


def normalize_sku_for_kream(sku: str) -> str:
    """On 구형 SKU(`NN.XXXXX`) → 크림 DB 키(`NN-XXXXX`) 로 정규화.

    신형 11자리 SKU 는 그대로 반환 (대문자).
    """
    if not sku:
        return ""
    s = sku.strip().upper()
    if SKU_OLD_RE.match(s):
        return s.replace(".", "-")
    return s


def parse_ldjson_block(html: str) -> dict | None:
    """PDP HTML → id="json-ld" 블록 `@graph` 중 ProductGroup dict 반환.

    실패 시 None. 빈 JSON/파싱 오류/ProductGroup 미존재 시 모두 None.
    """
    if not html:
        return None
    m = _LDJSON_RE.search(html) or _LDJSON_RE_ALT.search(html)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None

    # 단일 객체 또는 @graph 배열
    candidates: list[dict] = []
    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            candidates = [g for g in graph if isinstance(g, dict)]
        else:
            candidates = [data]
    elif isinstance(data, list):
        candidates = [g for g in data if isinstance(g, dict)]
    for g in candidates:
        if g.get("@type") == "ProductGroup":
            return g
    return None


def _parse_price(offer: dict) -> int:
    """offers dict → KRW 정수 가격. 파싱 실패 시 0."""
    if not isinstance(offer, dict):
        return 0
    raw = offer.get("price")
    if raw is None:
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def _offer_currency(offer: dict) -> str:
    if not isinstance(offer, dict):
        return ""
    return str(offer.get("priceCurrency") or "").strip().upper()


@dataclass(frozen=True)
class OnVariant:
    """ProductGroup.hasVariant 한 건 파싱 결과.

    sku: 원본 SKU (신형 11자리 또는 `NN.XXXXX`)
    color: "Apollo | Eclipse" 등
    price: KRW 정수
    in_stock: schema.org/InStock 여부
    url: offers.url (absolute)
    """

    sku: str
    color: str
    price: int
    currency: str
    in_stock: bool
    url: str


def parse_variants(product_group: dict) -> list[OnVariant]:
    """ProductGroup dict → OnVariant 리스트 (유효 SKU 만)."""
    variants_out: list[OnVariant] = []
    raw_variants = product_group.get("hasVariant") or []
    if not isinstance(raw_variants, list):
        return variants_out
    for v in raw_variants:
        if not isinstance(v, dict):
            continue
        sku = str(v.get("sku") or "").strip().upper()
        if not is_valid_sku(sku):
            continue
        color = str(v.get("color") or "").strip()
        offers = v.get("offers")
        # offers 는 단일 Offer dict (관찰된 전 케이스). list 도 방어.
        offer: dict
        if isinstance(offers, list):
            offer = offers[0] if offers and isinstance(offers[0], dict) else {}
        elif isinstance(offers, dict):
            offer = offers
        else:
            offer = {}
        avail = str(offer.get("availability") or "").lower()
        in_stock = "instock" in avail.replace("_", "").replace("/", "")
        variants_out.append(
            OnVariant(
                sku=sku,
                color=color,
                price=_parse_price(offer),
                currency=_offer_currency(offer),
                in_stock=in_stock,
                url=str(offer.get("url") or ""),
            )
        )
    return variants_out


def parse_pdp(html: str) -> dict | None:
    """PDP HTML → 덤프용 표준 dict. 실패 시 None.

    반환 구조:
        {
            "name": str,
            "brand": str,
            "url": str,  # productGroup.url 또는 비어있으면 variants[0].url
            "variants": list[OnVariant],
        }
    """
    pg = parse_ldjson_block(html)
    if pg is None:
        return None
    variants = parse_variants(pg)
    if not variants:
        return None
    brand_raw = pg.get("brand")
    if isinstance(brand_raw, dict):
        brand = str(brand_raw.get("name") or "").strip()
    else:
        brand = str(brand_raw or "").strip()
    return {
        "name": str(pg.get("name") or "").strip(),
        "brand": brand or "On",
        "url": str(pg.get("url") or variants[0].url or ""),
        "variants": variants,
    }


def is_launch_or_coming_soon(html: str) -> bool:
    """PDP HTML 내 발매예정/판매예정/coming soon 표시 감지.

    JSON-LD 는 InStock 으로 찍혀도 UI 에 "발매예정" 배지가 있을 수 있어
    보수적으로 스킵. 정확한 문구만 검사.
    """
    if not html:
        return False
    lower = html.lower()
    # 영문
    if "coming soon" in lower:
        return True
    # JSON-LD 에 'PreOrder' 같은 availability 가 오면 스킵
    if "schema.org/preorder" in lower or "schema.org/soldout" in lower:
        # SoldOut 은 OutOfStock 과 별개로 취급 — 매칭 무의미
        return True
    # 한국어
    if "발매예정" in html or "판매예정" in html or "출시예정" in html:
        return True
    return False


# ============================================================
# HTTP 레이어 — 실호출
# ============================================================


class OnRunningCrawler:
    """On Running 한국 공식몰 크롤러 — httpx 단독, 사이트맵 + PDP."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 보수적 rate limit — 사이트맵 1회 + 수백 PDP 순회를 1.5s 간격으로.
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=1.5)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=20.0,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    async def fetch_sitemap(self) -> list[str]:
        """사이트맵 1회 호출 → ko-kr 상품 URL 리스트."""
        client = await self._get_client()
        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(SITEMAP_URL)
            except httpx.HTTPError as exc:
                logger.warning("[on] sitemap HTTP 오류: %s", exc)
                return []
        if resp.status_code != 200:
            logger.warning("[on] sitemap HTTP %d", resp.status_code)
            return []
        urls = parse_sitemap(resp.text)
        logger.info("[on] sitemap 파싱: %d URL", len(urls))
        return urls

    async def fetch_pdp(self, url: str) -> dict | None:
        """상품 URL → PDP dict. 크롤러 외부는 parse_pdp 구조와 동일."""
        if not url:
            return None
        client = await self._get_client()
        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("[on] PDP HTTP 오류 %s: %s", url, exc)
                return None
        if resp.status_code != 200:
            logger.warning("[on] PDP HTTP %d %s", resp.status_code, url)
            return None
        if is_launch_or_coming_soon(resp.text):
            logger.info("[on] PDP 발매예정/soldout 스킵: %s", url)
            return None
        return parse_pdp(resp.text)

    # ------------------------------------------------------------------
    # BaseAdapter 호환 인터페이스 — 크림봇 기존 패턴 준수
    # ------------------------------------------------------------------
    async def search_products(self, keyword: str, limit: int = 10) -> list[dict]:
        """키워드(= SKU) 로 상품 1건 조회 시도.

        On 은 상품 검색 API 가 GraphQL/인증 뒤에 있어 키워드 → URL 맵이
        자명하지 않다. 크롤러 단건 조회 경로는 `fetch_pdp` 를 직접 쓰는 것이
        가장 단순 — 이 메서드는 BaseAdapter 호환용으로만 두고 빈 리스트 반환.
        """
        logger.debug("[on] search_products 는 지원하지 않음 — fetch_pdp 사용")
        return []

    async def get_product_detail(
        self, product_id: str
    ) -> RetailProduct | None:
        """product_id = PDP URL. RetailProduct 로 래핑해 반환.

        사이즈별 재고 정보는 사이트에서 내려오지 않으므로 색상 variant 단위로
        RetailSizeInfo 1건씩(사이즈="" 컬러명 대신 기록) 채워 넣는다. 이
        메서드는 기존 크롤러 호환 용도이고 실매칭은 어댑터에서 파싱 결과를
        직접 다룬다.
        """
        if not product_id:
            return None
        pdp = await self.fetch_pdp(product_id)
        if pdp is None:
            return None
        variants: list[OnVariant] = pdp["variants"]
        sizes: list[RetailSizeInfo] = []
        for v in variants:
            if not v.in_stock:
                continue
            sizes.append(
                RetailSizeInfo(
                    size=v.color or "",  # 실 사이즈 정보 없음 — 색상 태그로 대체
                    price=v.price,
                    original_price=v.price,
                    in_stock=True,
                )
            )
        if not sizes:
            return None
        # 첫 variant 의 SKU 를 대표 모델번호로 (색상별 개별 매칭은 어댑터가 담당)
        model = variants[0].sku
        return RetailProduct(
            source="on_running",
            product_id=product_id,
            name=pdp["name"],
            model_number=model,
            brand=pdp["brand"] or "On",
            url=pdp["url"] or product_id,
            sizes=sizes,
            fetched_at=datetime.now(),
        )

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("[on] 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
on_running_crawler = OnRunningCrawler()
# 지연 import 로 순환 회피 (registry 가 crawler 를 require 하는 패턴)
from src.crawlers.registry import register as _register  # noqa: E402

_register("on_running", on_running_crawler, "온러닝")


__all__ = [
    "BASE_URL",
    "LOCALE_PATH",
    "SITEMAP_URL",
    "SKU_NEW_RE",
    "SKU_OLD_RE",
    "OnRunningCrawler",
    "OnVariant",
    "extract_color_slug_from_url",
    "extract_gender_from_url",
    "extract_sku_from_url",
    "is_launch_or_coming_soon",
    "is_valid_sku",
    "normalize_sku_for_kream",
    "on_running_crawler",
    "parse_ldjson_block",
    "parse_pdp",
    "parse_sitemap",
    "parse_variants",
]
