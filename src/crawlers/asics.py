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

# 카테고리 코드들 — 아식스 한국몰 주요 카테고리 트리. 순회 키로 사용.
# (운영 중 추가/삭제 자유. 중복 상품은 adapter 레벨 dedup.)
ASICS_CATEGORY_CODES: tuple[str, ...] = (
    # RUNNING / 러닝화 메인
    "001100060012",  # #RUN > 컬렉션
    "001100060013",
    "001100060014",
    # SPORTSTYLE
    "sps_collection",
    # MENS SHOES
    "0001",
    "0002",
    # WOMENS SHOES
    "0003",
    "0004",
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
        client = await self._get_client()
        url = f"{BASE_URL}{path}"
        async with self._rate_limiter.acquire():
            resp = await client.get(url)
        status = getattr(resp, "status_code", 0)
        if status != 200:
            logger.warning("asics GET %s → HTTP %s", path, status)
            return ""
        text = resp.text or ""
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
            return ""
        return text

    # ------------------------------------------------------------------
    # 공용 인터페이스
    # ------------------------------------------------------------------
    async def fetch_category_products(self, category_code: str) -> list[dict]:
        """카테고리 SSR → AsicsTile dict 리스트. 상세 미보강."""
        path = f"/c/{category_code}"
        text = await self._fetch(path)
        if not text:
            return []
        tiles = _extract_tiles_from_category_html(text)
        logger.info("asics 카테고리 %s: %d tiles", category_code, len(tiles))
        return [t.as_dict() for t in tiles]

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
        # 카테고리 순회 후 모델번호 매칭 (제한적 사용).
        results: list[dict] = []
        for code in ASICS_CATEGORY_CODES:
            tiles = await self.fetch_category_products(code)
            for t in tiles:
                detail = await self.fetch_product_detail(t["product_id"])
                if detail.get("model_number") == target:
                    results.append({
                        "product_id": t["product_id"],
                        "name": detail.get("name") or t.get("name") or "",
                        "brand": "Asics",
                        "model_number": target,
                        "price": int(detail.get("price_krw") or t.get("price_krw") or 0),
                        "original_price": int(detail.get("price_krw") or 0),
                        "url": t["url"],
                        "image_url": "",
                        "is_sold_out": not detail.get("available", True),
                        "sizes": list(detail.get("sizes") or []),
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
