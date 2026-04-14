"""Asics 한국 공식몰 크롤러 — Phase 3 배치 6.

배경
----
아식스 한국몰 ``www.asics.co.kr`` 은 NetFunnel 트래픽 대기열을 입구에 건
자체 플랫폼(feelgooni 기반 SSR)이다. 최초 HTML 은 약 3.7KB 스켈레톤 +
``NetFunnel_Action`` 후 ``location.reload()`` 구조로, 쿠키
``NetFunnel_ID_act_1`` 를 보유해야 실제 SSR 페이지(상품/카테고리)가
렌더된다. 쿠키 미보유 시 동일 스켈레톤만 반복 수신된다.

기법
----
- HTTP: curl_cffi(impersonate=safari17_0). Akamai/봇 감지 우회.
- NetFunnel 토큰: 크롤러 외부에서 주입 가능 (``attach_netfunnel_cookie``).
  실운영 배포는 Playwright 헬퍼가 주기적으로 한 번 통과한 쿠키를 주입한다.
  본 크롤러는 쿠키 수급 자체를 책임지지 않는다 — 주입이 없으면 모든 호출은
  스켈레톤을 반환하고 ``empty`` 로 기록될 뿐이다. (읽기 전용 · GET only.)
- 엔드포인트:
    GET /c/{category_code}        — 카테고리 SSR (상품 tile + href + price)
    GET /p/{product_id}           — 상품 상세 SSR (모델번호 + 사이즈/재고)
  NetFunnel 통과 후에는 둘 다 완전한 HTML 을 반환한다.
- 파싱:
    1) 카테고리 페이지에서 ``href="/p/{id}"`` 타일을 추출 + 가격/이름 붙임
    2) 상품 상세에서 ``모델번호`` 또는 ``ProductCode`` 레이블 옆 텍스트에
       ``\\d{4}[A-Z]\\d{3}-\\d{3}`` 정규식으로 모델번호 추출. 메타 태그
       ``<meta property="product:retailer_item_id" content="...">`` fallback.
    3) 사이즈별 재고: ``data-option`` / ``optionStock`` JSON 블록에서 파싱
       (NetFunnel 통과된 SSR 에서만 의미 있음).

모델번호 포맷
-------------
``^\\d{4}[A-Z]\\d{3}-\\d{3}$`` — 예: ``1203A879-021``, ``1011B974-100``.
크림 DB 에 저장된 형식과 동일 — 정규화 후 직접 key 매칭.

한계
----
- 쿠키 미보유 구간에서는 사실상 아무 데이터도 뽑지 못함. 실운영에서는
  ``attach_netfunnel_cookie`` 주입 필수.
- 실계정/로그인 불필요 — 모든 요청은 익명 GET.

Read-only · GET only · POST 금지.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from src.crawlers.registry import register
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("asics_crawler")

BASE_URL = "https://www.asics.co.kr"

# Asics 모델번호: ``\d{4}[A-Z]\d{3}-\d{3}``.
# 샘플: ``1203A879-021``, ``1011B974-100``, ``1201B020-100``.
ASICS_MODEL_RE = re.compile(r"\b(\d{4}[A-Z]\d{3}-\d{3})\b")

# 카테고리 페이지의 상품 타일 href — ``/p/{숫자id}``.
TILE_HREF_RE = re.compile(r'href="(/p/(\d+))"')

# 가격 텍스트 — 카테고리/상세 모두에서 공용.
#   ``<span ...>159,000원</span>`` 또는 ``데이터 속성 data-price="159000"``.
PRICE_ATTR_RE = re.compile(r'data-price="(\d+)"')
PRICE_TEXT_RE = re.compile(r'(\d{2,3}(?:,\d{3})+)\s*원')

# 상세 페이지의 모델번호 근접 레이블.
MODEL_LABEL_RE = re.compile(
    r'(?:모델\s*번호|모델번호|ProductCode|Model\s*No\.?)\s*[:\s]*([A-Z0-9\-]+)',
    flags=re.I,
)

# og:title fallback — 상품명만 뽑는 용도.
OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    flags=re.I,
)

# 카테고리 코드들 — 아식스 한국몰 계층 카테고리 트리 (2026-04-14 실측).
# 코드 구조: ``{gender_top}{category_top}`` (4+4 digit).
#   0018=Men, 0019=Women, 0021=Sportstyle/Unisex
#   ...0001=신발, 0002=의류, 0003=용품
# 페이지당 32상품 · 평균 ~10페이지/카테고리 → 총 ~800~1000 상품 예상.
# 운영 중 추가/삭제 자유. 중복 상품은 hosting_product_id 기준 dedup.
ASICS_CATEGORY_CODES: tuple[str, ...] = (
    "00180001",  # Men 신발
    "00190001",  # Women 신발
    "00210001",  # Sportstyle/Unisex 신발
)


HEADERS_DEFAULT = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Referer": f"{BASE_URL}/main/index",
}


@dataclass
class AsicsTile:
    """카테고리 페이지의 상품 tile — master 단위.

    모델번호는 타일 단계에서 미확보 → ``model_number=""`` 로 뽑고,
    이후 상세에서 보강(또는 보강 실패 시 adapter 가 invalid 로 분류).
    """

    product_id: str           # /p/{id} 의 id
    name: str                 # 타일 title
    url: str                  # 절대 URL
    price_krw: int            # 정가/판매가 (KRW)
    model_number: str = ""    # 상세에서 보강 (4자리+문자+3자리-3자리)
    sizes: list[dict] = field(default_factory=list)  # [{size, available}]
    available: bool = True

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "url": self.url,
            "price_krw": self.price_krw,
            "model_number": self.model_number,
            "sizes": list(self.sizes),
            "available": self.available,
        }


def _clean_text(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def _extract_tiles_from_category_html(text: str) -> list[AsicsTile]:
    """카테고리 페이지 HTML → AsicsTile 리스트.

    전략: ``/p/{id}`` href 를 key 로 dedup. 각 href 근처 블록(±800자)에서
    가격/이름 추출. 상세 보강 없이도 가격/이름/URL 은 전부 확보.
    """
    tiles: dict[str, AsicsTile] = {}
    for m in TILE_HREF_RE.finditer(text):
        pid = m.group(2)
        if pid in tiles:
            continue
        start = max(0, m.start() - 200)
        end = min(len(text), m.end() + 800)
        block = text[start:end]

        price_krw = 0
        pm = PRICE_ATTR_RE.search(block)
        if pm:
            try:
                price_krw = int(pm.group(1))
            except ValueError:
                price_krw = 0
        if not price_krw:
            tm = PRICE_TEXT_RE.search(block)
            if tm:
                try:
                    price_krw = int(tm.group(1).replace(",", ""))
                except ValueError:
                    price_krw = 0

        # tile 내 name — ``alt`` 속성 또는 ``<p class="name">...</p>``.
        name = ""
        name_m = re.search(
            r'<(?:p|div|span)[^>]*class="[^"]*(?:name|title|pname)[^"]*"[^>]*>(.*?)</(?:p|div|span)>',
            block,
            flags=re.S,
        )
        if name_m:
            name = _clean_text(name_m.group(1))
        if not name:
            alt_m = re.search(r'alt="([^"]{4,120})"', block)
            if alt_m:
                name = _clean_text(alt_m.group(1))

        tiles[pid] = AsicsTile(
            product_id=pid,
            name=name,
            url=f"{BASE_URL}/p/{pid}",
            price_krw=price_krw,
            available=True,
        )
    return list(tiles.values())


# ─── 새 파서: dataLayer view_item_list 배열 (2026-04-14 실측) ─────────
#
# 아식스 한국몰 `/goods/catalog?code={code}` 카테고리 페이지는 GA ecommerce
# 이벤트 dataLayer 에 `products` 배열을 임베드한다. 배열 원소 하나당 한 상품
# 이며 아래 필드가 전부 노출된다:
#
#   - hosting_product_id  : 내부 product id (URL /p/ 뒤)
#   - styleCode           : "1203A899.101" 형식 (dot → hyphen = kream 포맷)
#   - name                : "젤 1130_1203A899101_21855" (디스플레이명_id_hostingId)
#   - price / originalPrice / markedDownPrice
#   - stock               : 'yes'|'no' — 총량 재고
#   - stockSize           : {"230":"yes","235":"yes",...} — 사이즈별 재고
#   - url                 : "/p/AKR_112619330-101" (상세 URL)
#   - brand               : "ASICS" 또는 "Onitsuka Tiger"
#   - gender              : MALE/FEMALE/UNISEX
#   - variant             : 색상명
#   - tag                 : [aliases...] — 검색용
#
# 이 배열만 파싱하면 카테고리 덤프 1회로 모델번호·사이즈 재고·가격·이름이
# 전부 확보되며, 상세 페이지 재방문이 불필요하다.

_DL_PRODUCTS_START_RE = re.compile(
    r"'event':\s*'view_item_list'.*?'products':\s*(\[)", re.S
)
_DL_BLOCK_START_RE = re.compile(r"\{'name':\s*'")
_DL_FIELD_STR_RE = {
    "hosting_product_id": re.compile(r"'hosting_product_id':\s*'(\d+)'"),
    "style_code": re.compile(r"'styleCode':\s*'([^']+)'"),
    "raw_name": re.compile(r"'name':\s*'([^']+)'"),
    "url": re.compile(r"'url':\s*'([^']+)'"),
    "brand": re.compile(r"'brand':\s*'([^']+)'"),
    "gender": re.compile(r"'gender':\s*'([^']+)'"),
    "variant": re.compile(r"'variant':\s*'([^']+)'"),
    "product_color": re.compile(r"'product_color':\s*'([^']+)'"),
    "stock": re.compile(r"'stock':\s*'(yes|no)'"),
}
_DL_FIELD_NUM_RE = {
    "price": re.compile(r"'price':\s*(\d+(?:\.\d+)?)"),
    "original_price": re.compile(r"'originalPrice':\s*(\d+)"),
    "marked_down_price": re.compile(r"'markedDownPrice':\s*(\d+)"),
}
_DL_STOCK_SIZE_RE = re.compile(r"'stockSize':\s*(\{[^}]*\})")
_DL_SIZE_PAIR_RE = re.compile(r'"([^"]+)":"(yes|no)"')


def _find_array_span(text: str, open_idx: int) -> int:
    """문자열 리터럴을 건너뛰며 ``text[open_idx]`` 에서 시작하는 배열의 종료
    인덱스(``]`` 의 다음 위치)를 반환. 실패 시 ``open_idx`` 반환.
    """
    if open_idx >= len(text) or text[open_idx] != "[":
        return open_idx
    depth = 0
    in_str = False
    quote = ""
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                in_str = False
        else:
            if c == "'" or c == '"':
                in_str = True
                quote = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return open_idx


def _parse_catalog_product_block(block: str) -> dict | None:
    """dataLayer products 배열의 한 원소(JS object 문자열) → dict.

    hosting_product_id 가 없으면 None 반환. 가격은 int(krw), 사이즈는
    ``[{"size": "255", "available": True}, ...]`` 형식.
    """

    def _s(key: str) -> str:
        m = _DL_FIELD_STR_RE[key].search(block)
        return m.group(1) if m else ""

    def _n(key: str) -> int:
        m = _DL_FIELD_NUM_RE[key].search(block)
        if not m:
            return 0
        try:
            return int(float(m.group(1)))
        except ValueError:
            return 0

    hosting_id = _s("hosting_product_id")
    if not hosting_id:
        return None

    style_code = _s("style_code")
    model_number = style_code.replace(".", "-").upper() if style_code else ""

    raw_name = _s("raw_name")
    # 디스플레이명 추출 — `_styleCodeColor_hostingId` 접미사 제거.
    # 최종 underscore 두 조각이 숫자/모델ID 이므로 첫 underscore 앞까지 취한다.
    display_name = raw_name.split("_", 1)[0].strip() if raw_name else ""

    price_krw = _n("price")
    original_price_krw = _n("original_price") or price_krw
    marked_price_krw = _n("marked_down_price") or price_krw

    stock = _s("stock")
    overall_available = stock == "yes"

    sizes: list[dict] = []
    sz_m = _DL_STOCK_SIZE_RE.search(block)
    if sz_m:
        for size, avail in _DL_SIZE_PAIR_RE.findall(sz_m.group(1)):
            sizes.append({"size": size, "available": avail == "yes"})

    url_path = _s("url")
    url = f"{BASE_URL}{url_path}" if url_path else ""

    return {
        "product_id": hosting_id,
        "name": display_name,
        "brand": _s("brand"),
        "model_number": model_number,
        "style_code": style_code,
        "url": url,
        "price_krw": price_krw,
        "original_price_krw": original_price_krw,
        "marked_down_price_krw": marked_price_krw,
        "sizes": sizes,
        "available": overall_available,
        "variant": _s("variant"),
        "gender": _s("gender"),
        "product_color": _s("product_color"),
    }


def _extract_products_from_catalog_html(text: str) -> list[dict]:
    """카테고리 페이지 HTML → dataLayer products 배열 → dict 리스트.

    파싱 실패(배열 미존재)면 빈 리스트. NetFunnel 통과 전 스켈레톤이면
    _fetch 단계에서 이미 빈 문자열로 차단되므로 여기서는 실 SSR 만 기대.
    """
    m = _DL_PRODUCTS_START_RE.search(text)
    if not m:
        return []
    arr_start = m.start(1)
    arr_end = _find_array_span(text, arr_start)
    if arr_end <= arr_start:
        return []
    arr = text[arr_start:arr_end]

    positions = [p.start() for p in _DL_BLOCK_START_RE.finditer(arr)]
    if not positions:
        return []
    positions.append(len(arr))

    out: list[dict] = []
    for i in range(len(positions) - 1):
        block = arr[positions[i] : positions[i + 1]]
        product = _parse_catalog_product_block(block)
        if product:
            out.append(product)
    return out


def _extract_detail(text: str) -> tuple[str, str, int, list[dict]]:
    """상세 페이지 HTML → (model_number, name, price_krw, sizes).

    NetFunnel 통과 전 스켈레톤이면 전부 빈 값/0 으로 복귀.
    """
    # 모델번호 — 레이블 기반 우선, 없으면 본문 전체 정규식.
    model = ""
    lm = MODEL_LABEL_RE.search(text)
    if lm:
        cand = lm.group(1).strip().upper()
        mm = ASICS_MODEL_RE.search(cand)
        if mm:
            model = mm.group(1)
    if not model:
        mm = ASICS_MODEL_RE.search(text.upper())
        if mm:
            model = mm.group(1)

    # 상품명 — og:title fallback
    name = ""
    om = OG_TITLE_RE.search(text)
    if om:
        name = om.group(1).strip()
        if name == "아식스 공식 온라인스토어":
            name = ""

    # 가격 — data-price 또는 원화 텍스트
    price = 0
    pm = PRICE_ATTR_RE.search(text)
    if pm:
        try:
            price = int(pm.group(1))
        except ValueError:
            price = 0
    if not price:
        tm = PRICE_TEXT_RE.search(text)
        if tm:
            try:
                price = int(tm.group(1).replace(",", ""))
            except ValueError:
                price = 0

    # 사이즈별 재고 — `data-option-size="..." data-option-stock="N"` 패턴 파싱
    sizes: list[dict] = []
    for sm in re.finditer(
        r'data-(?:option-)?size="([^"]+)"[^>]*data-(?:option-)?stock="(-?\d+)"',
        text,
    ):
        size_label = sm.group(1).strip()
        try:
            stock = int(sm.group(2))
        except ValueError:
            stock = 0
        sizes.append({"size": size_label, "available": stock > 0})

    return model, name, price, sizes


class AsicsCrawler:
    """Asics 한국몰 카탈로그 크롤러 (curl_cffi Safari + NetFunnel 쿠키 주입).

    ``_client`` 는 지연 생성되고, ``attach_netfunnel_cookie`` 를 통해
    외부(Playwright 헬퍼)에서 획득한 NetFunnel 토큰 쿠키를 덮어쓸 수 있다.
    """

    def __init__(self) -> None:
        self._client = None  # lazy: curl_cffi AsyncSession
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=2.0)
        self._netfunnel_cookie: str | None = None
        self._skeleton_callback = None  # () -> Awaitable[None] | None
        # code → slug 캐시. /goods/catalog?code=XXX 는 /c/{slug} 로 리다이렉트되는데,
        # 페이지네이션(``?page=N``)은 friendly slug URL 에서만 동작한다. 첫 페이지
        # 리다이렉트 시 추출한 slug 를 캐시해 이후 요청 재사용.
        self._category_slug_cache: dict[str, str] = {}

    async def _get_client(self):
        if self._client is None:
            from curl_cffi.requests import AsyncSession

            self._client = AsyncSession(
                impersonate="safari17_0",
                headers=HEADERS_DEFAULT,
                timeout=20,
            )
            if self._netfunnel_cookie:
                try:
                    self._client.cookies.set(
                        "NetFunnel_ID_act_1", self._netfunnel_cookie, domain=".asics.co.kr"
                    )
                except Exception:  # pragma: no cover
                    pass
        return self._client

    def set_skeleton_callback(self, cb) -> None:
        """스켈레톤(NetFunnel 미통과) 응답 감지 시 호출할 비동기 콜백 등록.

        어댑터는 이 콜백으로 ``NetFunnelCookieCache.invalidate()`` + 재획득을
        트리거한다. None 이면 콜백 비활성화 — 크롤러는 스켈레톤을 그대로
        빈 문자열로 반환한다.
        """
        self._skeleton_callback = cb

    def attach_netfunnel_cookie(self, value: str) -> None:
        """Playwright 헬퍼가 획득한 NetFunnel 토큰 주입.

        크롤러는 쿠키 수급 자체를 책임지지 않는다. 운영 루프가 주기적으로
        Playwright 로 홈 진입 → 쿠키 회수 → 본 메서드로 주입한다.
        """
        self._netfunnel_cookie = value
        if self._client is not None:
            try:
                self._client.cookies.set(
                    "NetFunnel_ID_act_1", value, domain=".asics.co.kr"
                )
            except Exception:  # pragma: no cover
                pass

    async def _fetch(self, path: str) -> str:
        text, _ = await self._fetch_full(path)
        return text

    async def _fetch_full(self, path: str, *, _retried: bool = False) -> tuple[str, str]:
        """``_fetch`` 의 확장판 — (text, final_url) 튜플 반환.

        리다이렉트 후 최종 URL 이 필요한 경로에서 사용한다 (예: 카테고리
        ``/goods/catalog?code=XXX`` → ``/c/{slug}`` 로 302 리다이렉트되는
        경우 slug 추출). 스켈레톤 감지 시 콜백이 쿠키를 재주입하므로
        자동으로 1회 재시도한다 (``_retried`` 로 무한루프 방지).
        """
        client = await self._get_client()
        url = f"{BASE_URL}{path}"
        async with self._rate_limiter.acquire():
            resp = await client.get(url)
        status = getattr(resp, "status_code", 0)
        if status != 200:
            logger.warning("asics GET %s → HTTP %s", path, status)
            return "", ""
        text = resp.text or ""
        final_url = str(getattr(resp, "url", "") or url)
        # NetFunnel 미통과 응답은 두 가지 포맷:
        #   1) 3~5KB 스켈레톤 + ``NetFunnel_Action`` JS 블록 (완전 초기 진입)
        #   2) ~150~200B location.reload() 리다이렉트 + netfunnel.js 참조
        #      (쿠키 만료/무효 시 — 서버측 토큰 TTL ~15s)
        # 둘 다 실SSR(수백 KB) 과 구분 가능. netfunnel 키워드 + 작은 본문 조건.
        is_skeleton = len(text) < 6000 and (
            "NetFunnel_Action" in text
            or "netfunnel.js" in text
            or "location.reload()" in text
        )
        if is_skeleton:
            logger.info("asics netfunnel 미통과 스켈레톤: path=%s len=%d", path, len(text))
            if self._skeleton_callback is not None:
                try:
                    await self._skeleton_callback()
                except Exception:  # pragma: no cover
                    logger.exception("asics skeleton callback 실패")
                # 콜백이 쿠키를 재주입했다면 1회 재시도 — 연속 스켈레톤 시엔
                # 두 번째 호출에서 정상 SSR 이 반환되는 케이스가 잦다.
                if not _retried:
                    return await self._fetch_full(path, _retried=True)
            return "", ""
        return text, final_url

    # ------------------------------------------------------------------
    # 공용 인터페이스
    # ------------------------------------------------------------------
    async def fetch_catalog_page(
        self, category_code: str, page: int = 1
    ) -> list[dict]:
        """단일 페이지 카탈로그 덤프.

        엔드포인트 규칙 (실측 2026-04-14)
        --------------------------------
        - ``/goods/catalog?code={code}`` 는 서버측에서 ``/c/{slug}`` 로
          리다이렉트된다 (``men_shoes``/``women_shoes``/``sps_shoes`` 등).
        - ``?page=N`` 쿼리 파라미터는 friendly slug URL(``/c/{slug}``) 에서만
          동작한다. ``/goods/catalog?page=N&code=XXX`` 는 page 무시하고 항상 1
          페이지를 반환 (서버 라우팅 버그로 보임).
        - 따라서 첫 페이지는 ``/goods/catalog`` 로 진입해 slug 를 추출해
          캐시하고, 2페이지 이후는 ``/c/{slug}?page=N&code={code}`` 로 직접
          요청한다. Referer 는 동일 카테고리 첫 페이지로 고정.
        """
        slug = self._category_slug_cache.get(category_code)
        if page == 1 or not slug:
            path = f"/goods/catalog?code={category_code}"
            text, final_url = await self._fetch_full(path)
            if not text:
                return []
            # 최종 URL 에서 slug 추출: https://.../c/{slug}[?...]
            m = re.search(r"/c/([a-zA-Z0-9_]+)", final_url)
            if m:
                slug = m.group(1)
                self._category_slug_cache[category_code] = slug
            else:
                # 리다이렉트 누락 시 og:url fallback
                og = re.search(
                    r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\'][^"\']*/c/([a-zA-Z0-9_]+)',
                    text,
                )
                if og:
                    slug = og.group(1)
                    self._category_slug_cache[category_code] = slug
            if page == 1:
                products = _extract_products_from_catalog_html(text)
                logger.info(
                    "asics 카테고리 %s[%s] p1: %d products",
                    category_code,
                    slug or "?",
                    len(products),
                )
                return products

        # page >= 2: slug 필요. 없으면 종료.
        if not slug:
            return []
        path = (
            f"/c/{slug}?page={page}&popup=&iframe=&code={category_code}"
        )
        text = await self._fetch(path)
        if not text:
            return []
        products = _extract_products_from_catalog_html(text)
        logger.info(
            "asics 카테고리 %s[%s] p%d: %d products",
            category_code,
            slug,
            page,
            len(products),
        )
        return products

    async def fetch_category_products(
        self,
        category_code: str,
        *,
        max_pages: int = 40,
    ) -> list[dict]:
        """카테고리 전체 페이지 순회 → dedup 후 dict 리스트 반환.

        어댑터 호환 시그니처(단일 인자 `category_code`) 유지. 페이지 순회는
        빈 페이지 또는 이전 페이지와 동일 결과를 만나면 즉시 종료.
        """
        dedup: dict[str, dict] = {}
        prev_first_id = ""
        for page in range(1, max_pages + 1):
            items = await self.fetch_catalog_page(category_code, page=page)
            if not items:
                break
            first_id = items[0].get("product_id") or ""
            if page > 1 and first_id and first_id == prev_first_id:
                # 동일 페이지 반복 — 페이지네이션 종료 또는 슬러그 오류
                logger.info(
                    "asics 카테고리 %s p%d: 첫 상품 동일(%s) — 종료",
                    category_code,
                    page,
                    first_id,
                )
                break
            prev_first_id = first_id
            added = 0
            for item in items:
                pid = (item.get("product_id") or "").strip()
                if not pid or pid in dedup:
                    continue
                dedup[pid] = item
                added += 1
            # 새 상품이 전혀 안 들어오면 종료
            if added == 0:
                break
        logger.info(
            "asics 카테고리 %s 전수: %d products (<=%d pages)",
            category_code,
            len(dedup),
            max_pages,
        )
        return list(dedup.values())

    async def fetch_product_detail(self, product_id: str) -> dict:
        """상품 상세 SSR → 모델번호/이름/가격/사이즈 보강 dict.

        실패 시 빈 dict 가 아닌 partial dict 반환 (model_number 없을 수 있음).
        """
        path = f"/p/{product_id}"
        text = await self._fetch(path)
        if not text:
            return {
                "product_id": product_id,
                "name": "",
                "model_number": "",
                "price_krw": 0,
                "sizes": [],
                "url": f"{BASE_URL}{path}",
                "available": False,
            }
        model, name, price, sizes = _extract_detail(text)
        available = any(s.get("available") for s in sizes) if sizes else True
        return {
            "product_id": product_id,
            "name": name,
            "model_number": model,
            "price_krw": price,
            "sizes": sizes,
            "url": f"{BASE_URL}{path}",
            "available": available,
        }

    async def search_products(
        self, keyword: str, limit: int = 30
    ) -> list[dict]:
        """키워드 검색 — 크림봇 크롤러 공용 인터페이스 호환.

        아식스 한국몰은 검색 엔드포인트가 NetFunnel 후에도 POST 기반이라
        안전상 사용하지 않는다. 입력이 모델번호면 카테고리 순회 결과에서
        필터링하는 대신 곧바로 빈 배열을 반환 (푸시 어댑터 경로 사용 권장).
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        m = ASICS_MODEL_RE.search(keyword.upper())
        if not m:
            return []
        target = m.group(1)
        results: list[dict] = []
        for code in ASICS_CATEGORY_CODES:
            items = await self.fetch_category_products(code)
            for item in items:
                if item.get("model_number") == target:
                    results.append({
                        "product_id": item["product_id"],
                        "name": item.get("name") or "",
                        "brand": item.get("brand") or "Asics",
                        "model_number": target,
                        "price": int(item.get("price_krw") or 0),
                        "original_price": int(item.get("original_price_krw") or 0),
                        "url": item.get("url") or "",
                        "image_url": "",
                        "is_sold_out": not item.get("available", True),
                        "sizes": list(item.get("sizes") or []),
                    })
                    if len(results) >= limit:
                        return results
        return results

    async def dump_catalog_categories(
        self, categories: tuple[str, ...] | list[str] | None = None
    ) -> list[dict]:
        """카테고리 리스트 순회 → master 단위 dict 리스트. 상세 미보강.

        어댑터 ``AsicsAdapter`` 가 이 결과를 받아 상세 보강 + 매칭 수행.
        """
        cats = tuple(categories) if categories else ASICS_CATEGORY_CODES
        dedup: dict[str, dict] = {}
        for code in cats:
            tiles = await self.fetch_category_products(code)
            for t in tiles:
                pid = t.get("product_id") or ""
                if pid and pid not in dedup:
                    dedup[pid] = t
        logger.info(
            "asics 카탈로그 덤프: 카테고리 %d개 → tile %d개",
            len(cats),
            len(dedup),
        )
        return list(dedup.values())

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None
        logger.info("asics 크롤러 연결 해제")


# ─── 싱글톤 + 레지스트리 등록 ─────────────────────────────────
asics_crawler = AsicsCrawler()
register("asics", asics_crawler, "아식스")


__all__ = [
    "ASICS_CATEGORY_CODES",
    "ASICS_MODEL_RE",
    "AsicsCrawler",
    "AsicsTile",
    "asics_crawler",
]
