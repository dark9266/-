"""노스페이스 한국 공식몰(thenorthfacekorea.co.kr) 크롤러 — Phase 3 배치 6.

플랫폼
------
Topick Commerce (반스와 동일). 다만 노스페이스 한국은 JSON 검색 API
(`/search/ajax`) 가 빈 배열만 돌려주도록 차단돼 있어 실전 경로는 **카테고리
리스팅 HTML** 과 **검색 결과 HTML** 이다. 두 경로 모두 서버사이드 렌더링돼
있어서 httpx + 단순 정규식 파싱으로 충분히 덮인다.

주요 엔드포인트 (GET only)
---------------------------
* 카테고리 리스팅: ``/category/n/{path}?page=N``
  - 예: ``/category/n/men``, ``/category/n/women/jacket/padding``
  - 페이지네이션: ``?page=2,3,...``. 마지막 페이지 이후엔 상품 0건.
* 검색: ``/search?q={keyword}``
* 상품 상세: ``/product/{model}``  (model 은 8자리 영숫자 style code)

파싱 규칙
----------
카테고리/검색 HTML 내 각 타일은 ``data-product-id="숫자"`` (4000xxxxxx 형태
내부 PK) 와 ``data-product-url="/product/NA5AS41B"`` (model number) 를
동시에 노출한다. **크림 모델번호 = URL path 의 style code** (`NA5AS41B`).
크림 DB 에 노스페이스 2,240개가 전부 이 형식으로 저장돼 있다.

* 모델번호: ``data-product-url="/product/([A-Z0-9]{6,12})"``
* 상품명: 타일 블록 내 ``<span class="name">...</span>``
* 가격: 타일 블록 내 첫 ``\\d{1,3}(?:,\\d{3})*\\s*원``
* 품절: ``data-product-sold-out="true"`` 이면 soldout
* 이미지: ``data-product-image-urls`` 의 첫 세그먼트

한계
-----
* 사이즈별 재고는 상세 페이지(HTML) 내 서버 렌더 `variation-size` 블록에
  노출되지만 검색·수익 파이프라인은 일단 리스팅 단계에서 종료. 사이즈 정보는
  수익 consumer 단계에서 별도 보강 예정(사이즈 교차검증 Phase 3 배치 7+).
* JSON API 차단: ``/search/ajax?q=keyword`` → ``{"productList":[]}`` 상시.
  HTML 파싱이 유일한 읽기 경로.

읽기 전용. GET only. POST 일체 사용하지 않는다.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

import httpx

from src.crawlers.registry import register
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("thenorthface_crawler")

BASE_URL = "https://www.thenorthfacekorea.co.kr"

# 카테고리 키 → 한글 라벨. 어댑터에서 override 가능.
# 홈페이지 네비게이션에서 관측된 실제 경로. 잘못된 경로는 404 대신
# 빈 리스트를 돌려주므로 소폭 안전.
DEFAULT_CATEGORIES: dict[str, str] = {
    "men": "남성",
    "women": "여성",
    "kids": "키즈",
    "equipment": "이큅먼트",
    "whitelabel": "화이트라벨",
    "shoes": "신발",
}

# 모델번호 (style code) — URL path segment 의 대문자 영숫자.
# 실 관측 샘플: ``NA5AS41B``, ``NJ3NP80A``, ``NC2HS31C``, ``NV1DR92A``.
# 길이 6~12 로 둬서 추후 포맷 변화에 여유.
TNF_MODEL_RE = re.compile(r"^[A-Z0-9]{6,12}$")

# HTML 타일에서 data-product-url 로 style code 추출
_TILE_URL_RE = re.compile(
    r'data-product-url="/product/([A-Z0-9]{6,12})"'
)
# 백업: href="/product/XXX"
_TILE_HREF_RE = re.compile(
    r'href="(?:https://www\.thenorthfacekorea\.co\.kr)?/product/([A-Z0-9]{6,12})"'
)
_TILE_PID_RE = re.compile(r'data-product-id="(\d+)"')
_TILE_SOLDOUT_RE = re.compile(r'data-product-sold-out="(true|false)"')
_TILE_IMG_RE = re.compile(r'data-product-image-urls="([^"]*)"')
# 상품명은 타일 블록 앵커(data-product-id)보다 앞쪽 `<a data-name="X" data-id="MODEL">`
# 위치에 있어 블록 슬라이스로는 못 잡음. 전역 맵을 만들어 lookup.
_NAME_BY_MODEL_RE = re.compile(
    r'data-name="([^"]+)"\s+data-id="([A-Z0-9]{6,12})"'
)
_PRICE_RE = re.compile(r"([\d,]{3,})\s*원")

# 타일 블록 분리용 앵커 — `data-product-id="숫자"` 가 타일 시작 위치.
_ANCHOR_RE = re.compile(r'data-product-id="\d+"')


HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/",
}


# ─── dataclass ────────────────────────────────────────────


@dataclass
class TnfTile:
    """카테고리/검색 HTML 에서 추출한 단일 상품 타일."""

    model_number: str  # style code — `NA5AS41B`
    product_id: str  # 내부 PK — `4000059442`
    name: str
    price: int
    is_sold_out: bool
    url: str
    image_url: str

    def as_dict(self) -> dict:
        return {
            "model_number": self.model_number,
            "product_id": self.product_id,
            "name": self.name,
            "brand": "The North Face",
            "price": self.price,
            "original_price": self.price,
            "url": self.url,
            "image_url": self.image_url,
            "is_sold_out": self.is_sold_out,
            "sizes": [],
        }


@dataclass
class TnfSizeInfo:
    size: str
    in_stock: bool


@dataclass
class TnfProductDetail:
    model_number: str
    name: str
    price: int
    sizes: list[TnfSizeInfo]


# ─── 순수 파싱 함수 ──────────────────────────────────────


def _clean_text(raw: str) -> str:
    """HTML 태그 제거 + 엔티티 복원 + 공백 정리."""
    if not raw:
        return ""
    stripped = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(stripped)).strip()


def _parse_price(text: str) -> int:
    m = _PRICE_RE.search(text)
    if not m:
        return 0
    digits = re.sub(r"[^\d]", "", m.group(1))
    return int(digits) if digits else 0


def _iter_tile_blocks(text: str) -> list[str]:
    """``data-product-id="..."`` 앵커 기준으로 타일 블록을 잘라 반환.

    한 앵커부터 다음 앵커 직전까지를 하나의 블록으로 간주한다.
    간헐적으로 동일 타일 내에서 여러 colour swatch 가 ``data-product-id`` 를
    반복하는데, 그 경우에도 각 swatch 별로 블록이 나뉘어 style code 중복이
    생길 수 있으므로 호출자가 model_number dedup 해야 한다.
    """
    positions = [m.start() for m in _ANCHOR_RE.finditer(text)]
    if not positions:
        return []
    positions.append(len(text))
    return [text[positions[i] : positions[i + 1]] for i in range(len(positions) - 1)]


def parse_listing_html(html_text: str) -> list[TnfTile]:
    """카테고리/검색 HTML → TnfTile 리스트 (model_number dedup)."""
    if not html_text:
        return []

    # 상품명 전역 맵 — `<a data-name data-id>` 는 타일 앵커 앞에 위치해
    # 블록 슬라이스로는 못 잡기 때문에 문서 전체에서 미리 추출.
    name_by_model: dict[str, str] = {}
    for m in _NAME_BY_MODEL_RE.finditer(html_text):
        model = m.group(2).upper()
        if model not in name_by_model:
            name_by_model[model] = _clean_text(m.group(1))

    # 타일 블록 단위로 자르기 — 같은 블록 안에서 name/price/model 을 묶어야
    # 타 상품의 값이 섞이지 않는다.
    tiles: dict[str, TnfTile] = {}

    for block in _iter_tile_blocks(html_text):
        url_m = _TILE_URL_RE.search(block) or _TILE_HREF_RE.search(block)
        if not url_m:
            continue
        model_number = url_m.group(1).upper()
        if not TNF_MODEL_RE.match(model_number):
            continue
        if model_number in tiles:
            continue

        pid_m = _TILE_PID_RE.search(block)
        product_id = pid_m.group(1) if pid_m else ""

        soldout_m = _TILE_SOLDOUT_RE.search(block)
        is_sold_out = bool(soldout_m and soldout_m.group(1) == "true")

        name = name_by_model.get(model_number, "")

        price = _parse_price(block)

        img_m = _TILE_IMG_RE.search(block)
        image_url = ""
        if img_m:
            first = (img_m.group(1).split("@") or [""])[0]
            if first:
                image_url = (
                    first if first.startswith("http") else f"{BASE_URL}{first}"
                )

        tiles[model_number] = TnfTile(
            model_number=model_number,
            product_id=product_id,
            name=name,
            price=price,
            is_sold_out=is_sold_out,
            url=f"{BASE_URL}/product/{model_number}",
            image_url=image_url,
        )

    return list(tiles.values())


_SKU_DATA_RE = re.compile(r'data-sku-data="(\[.*?\])"', re.S)
_PRODUCT_OPTIONS_RE = re.compile(r'data-product-options="(\[.*?\])"', re.S)
_OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', re.I)


def _parse_pdp_sizes(html_text: str) -> list[TnfSizeInfo]:
    """PDP HTML에서 사이즈별 재고를 파싱."""
    opts_m = _PRODUCT_OPTIONS_RE.search(html_text)
    if not opts_m:
        return []
    try:
        options = json.loads(opts_m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
    except (json.JSONDecodeError, ValueError):
        return []

    size_map: dict[int, str] = {}
    for opt in options:
        if opt.get("type") == "SIZE":
            for k, v in (opt.get("values") or {}).items():
                try:
                    size_map[int(k)] = str(v)
                except (TypeError, ValueError):
                    pass
    if not size_map:
        return []

    sku_m = _SKU_DATA_RE.search(html_text)
    if not sku_m:
        return []
    try:
        skus = json.loads(sku_m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
    except (json.JSONDecodeError, ValueError):
        return []

    seen: set[str] = set()
    sizes: list[TnfSizeInfo] = []
    for sku in skus:
        sold = sku.get("isSoldOut", False)
        qty = sku.get("quantity", 0)
        for opt_id in sku.get("selectedOptions") or []:
            label = size_map.get(opt_id)
            if label and label not in seen:
                seen.add(label)
                sizes.append(TnfSizeInfo(size=label, in_stock=not sold and qty > 0))
    return sizes


# ─── 크롤러 클래스 ───────────────────────────────────────


class TheNorthFaceCrawler:
    """노스페이스 한국 공식몰 카테고리/검색 HTML 크롤러."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 공식 CLAUDE.md Rate Limit 표 준수 — 2 req/1.5s
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

    async def _fetch_html(self, path: str, params: dict | None = None) -> str:
        client = await self._get_client()
        url = f"{BASE_URL}{path}"
        async with self._rate_limiter.acquire():
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning(
                "노스페이스 HTTP %d (%s params=%s)",
                resp.status_code,
                path,
                params,
            )
            return ""
        return resp.text or ""

    async def search_products(
        self, keyword: str, limit: int = 30
    ) -> list[dict]:
        """키워드로 상품 검색 (HTML 파싱).

        ``/search/ajax`` JSON 은 빈 배열만 돌려주므로 ``/search`` HTML
        엔드포인트를 쓴다.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        text = await self._fetch_html("/search", params={"q": keyword})
        tiles = parse_listing_html(text)
        results = [t.as_dict() for t in tiles[:limit]]
        logger.info("노스페이스 검색 '%s': %d건", keyword, len(results))
        return results

    async def fetch_category_page(
        self, category_path: str, page: int = 1
    ) -> list[TnfTile]:
        """카테고리 한 페이지 덤프 → TnfTile 리스트.

        Parameters
        ----------
        category_path:
            ``men`` / ``women/jacket/padding`` 등 ``/category/n/`` 뒤에
            붙는 path. 선행 슬래시는 제거된 형태.
        page:
            1-based 페이지 번호.
        """
        clean = (category_path or "").strip().strip("/")
        if not clean:
            return []
        params = {"page": str(page)} if page > 1 else None
        text = await self._fetch_html(f"/category/n/{clean}", params=params)
        return parse_listing_html(text)

    async def dump_catalog_categories(
        self,
        categories: list[str] | tuple[str, ...] | None = None,
        *,
        max_pages: int = 20,
    ) -> list[TnfTile]:
        """카테고리 리스트를 순회해 카탈로그 덤프 (model_number dedup).

        한 카테고리 내에서 빈 페이지(상품 0건) 가 나오면 중단. 최대
        ``max_pages`` 까지만 돈다.
        """
        cats = list(categories or DEFAULT_CATEGORIES.keys())
        dedup: dict[str, TnfTile] = {}
        for cat in cats:
            for page in range(1, max_pages + 1):
                try:
                    tiles = await self.fetch_category_page(cat, page=page)
                except Exception:
                    logger.exception(
                        "노스페이스 카테고리 덤프 실패: %s page=%d", cat, page
                    )
                    break
                if not tiles:
                    break
                new_rows = 0
                for t in tiles:
                    if t.model_number not in dedup:
                        dedup[t.model_number] = t
                        new_rows += 1
                # 새 상품이 하나도 추가되지 않으면 다음 카테고리로
                if new_rows == 0:
                    break
        logger.info(
            "노스페이스 카탈로그 덤프: 카테고리 %d개 → %d개 모델",
            len(cats),
            len(dedup),
        )
        return list(dedup.values())

    async def _ensure_session(self) -> None:
        """PDP 접근 전 세션 쿠키 확보 — 카테고리 방문으로 워밍업."""
        if getattr(self, "_session_warm", False):
            return
        await self._fetch_html("/category/n/shoes", params={"page": "1"})
        self._session_warm = True

    async def get_product_detail(self, model_number: str) -> TnfProductDetail | None:
        """PDP HTML에서 사이즈별 재고 파싱."""
        model_number = (model_number or "").strip().upper()
        if not model_number:
            return None
        await self._ensure_session()
        html_text = await self._fetch_html(f"/product/{model_number}")
        if not html_text or len(html_text) < 5000:
            return None
        sizes = _parse_pdp_sizes(html_text)
        if not sizes:
            return None
        title_m = _OG_TITLE_RE.search(html_text)
        name = title_m.group(1) if title_m else ""
        price = _parse_price(html_text)
        return TnfProductDetail(
            model_number=model_number, name=name, price=price, sizes=sizes
        )

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("노스페이스 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
thenorthface_crawler = TheNorthFaceCrawler()
register("thenorthface", thenorthface_crawler, "노스페이스")


__all__ = [
    "BASE_URL",
    "DEFAULT_CATEGORIES",
    "HEADERS",
    "TNF_MODEL_RE",
    "TheNorthFaceCrawler",
    "TnfTile",
    "parse_listing_html",
    "thenorthface_crawler",
]
