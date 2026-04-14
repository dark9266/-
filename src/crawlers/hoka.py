"""호카(HOKA) 크롤러 — Phase 3 배치 5.

배경
----
호카는 한국 직판이 없다. `hoka.co.kr` 은 네이버 브랜드스토어로 리다이렉트되며,
`www.hoka.com/kr/ko/` 는 410 응답(사실상 geo 차단). 다만 본사 Salesforce
Commerce Cloud(Demandware) 의 `Coveo-Show` 엔드포인트는 GET 공개이고
curl_cffi Safari 핑거프린트로 200 OK 를 받는다.

기법
----
- HTTP: curl_cffi(impersonate=safari17_0). httpx 는 406/410 으로 차단.
- 엔드포인트:
    GET /on/demandware.store/Sites-HOKA-US-Site/en_US/Coveo-Show?q={kw}&qs=1
- 파싱: HTML 내
    1) 타일 링크 `/(.+)/(\\d{7})\\.html?dwvar_\\d+_color=(\\w+)` →
       masterID + color → 모델번호 `1171904-ASRN`
    2) 타일의 `data-suggestion-pid="{UPC12}"` (보조 키)
    3) 상품명 (`<div class="name">...</div>`)
    4) 가격 `<span class="sales">$155.00</span>` (USD) — 어댑터가 KRW 환산

한계
----
- 호카 상세/재고 API 는 공개 차단. 사이즈별 재고 미확보 → 재고는 True 로
  간주(검색 결과에 노출되면 최소한 일부 사이즈 재고 있다는 뜻). 정확도가
  필요한 수익 판정 단계는 오케스트레이터 아래 단계에서 재검증.
- 검색 키워드를 순회해 카탈로그를 덮는다. 풀 덤프 API 부재.

읽기 전용. GET only.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

from src.crawlers.registry import register
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("hoka_crawler")

BASE_URL = "https://www.hoka.com"
# 2026-04-14: DataDome 이 Coveo XHR 엔드포인트만 선별 차단(403).
# 스토어프론트 HTML 검색 페이지(`/en/us/search?q=`) 는 200 OK 로 동일한
# tile-row 그리드를 제공해 같은 파서로 처리 가능 → 프록시·챌린지 우회 없이
# HTML 경로로 전환.
SEARCH_PATH = "/en/us/search"
COVEO_PATH = "/on/demandware.store/Sites-HOKA-US-Site/en_US/Coveo-Show"  # 레거시 — 폴백 유지


def _redact_proxy(url: str) -> str:
    """프록시 URL 로깅용 마스킹 — scheme://host:port 만 노출, 계정정보 숨김."""
    m = re.match(r"^(\w+)://(?:[^@]+@)?([^/]+)", url)
    return f"{m.group(1)}://***@{m.group(2)}" if m else "***"

# 호카 주요 모델명 — Coveo 검색 키워드로 순회해 카탈로그 커버.
# 편집 가능. 신모델 등장 시 추가.
HOKA_SEARCH_KEYWORDS: tuple[str, ...] = (
    "Mach",
    "Clifton",
    "Bondi",
    "Rincon",
    "Arahi",
    "Gaviota",
    "Speedgoat",
    "Challenger",
    "Stinson",
    "Torrent",
    "Tecton",
    "Zinal",
    "Kawana",
    "Solimar",
    "Transport",
    "Skyflow",
    "Skyward",
    "Cielo",
    "Rocket",
    "Mafate",
    "Kaha",
    "Anacapa",
    "Ora",
)

# ``masterID = 7자리``, ``color = 2~6 대문자``.
# 실제 관측 샘플: ``1171904-ASRN``, ``1168871-NZS``, ``1176251-FCG``.
HOKA_MODEL_RE = re.compile(r"\b(1\d{6})-([A-Z]{2,6})\b")

# 상품 타일 링크 형태:
# /en/us/{cat}/{slug}/{masterID}.html?dwvar_{masterID}_color={COLOR}
TILE_HREF_RE = re.compile(
    r'href="(/en/us/[^"\s]*?/(\d{7})\.html\?dwvar_\d+_color=([A-Z0-9]{2,10}))"'
)

# data-suggestion-pid (UPC 12자리) — autosuggest 드롭다운용 (레거시, 일부 페이지에만 존재)
SUGG_PID_RE = re.compile(r'data-suggestion-pid="(\d{10,14})"')

# 메인 그리드 tile-row 블록 시작 마커. 각 블록은 `<div class="tile-row ..."` 로 시작하고
# `data-tile-analytics="{...JSON...}"` 속성 안에 customData(contentIDValue/name/id) 가 들어있다.
TILE_ROW_START_RE = re.compile(r'<div\s+class="tile-row\b')
TILE_ANALYTICS_RE = re.compile(r'data-tile-analytics="([^"]+)"')

# 가격 (USD)
PRICE_RE = re.compile(r"\$(\d+(?:\.\d{2})?)")


HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


@dataclass
class HokaTile:
    """Coveo 검색 결과에서 뽑은 tile 단위 flat row.

    다른 어댑터들과 스펙을 맞추기 위해 dict 대신 dataclass 로 유지하되
    어댑터에서는 dict 로 변환해 넘긴다.
    """

    master_id: str
    color_code: str
    sku: str  # master-color
    name: str
    href: str  # 상대 경로
    url: str  # 절대 경로
    upc: str
    price_usd: float
    available: bool = True

    def as_dict(self) -> dict:
        return {
            "master_id": self.master_id,
            "color_code": self.color_code,
            "sku": self.sku,
            "name": self.name,
            "href": self.href,
            "url": self.url,
            "upc": self.upc,
            "price_usd": self.price_usd,
            "available": self.available,
        }


def _extract_analytics_masters(text: str) -> list[dict]:
    """검색 결과 HTML 의 `data-search-analytics` JSON 을 파싱.

    Coveo 응답 헤더에 `data-search-analytics="{...}"` 가 HTML 엔티티로
    인코딩되어 있다. 디코드 후 JSON 파싱, masterIDs/productSKUs/productNames
    배열을 pair-wise 로 묶어 dict 리스트로 반환.
    """
    m = re.search(r'data-search-analytics="([^"]+)"', text)
    if not m:
        return []
    try:
        decoded = html.unescape(m.group(1))
        payload = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return []

    masters = payload.get("masterIDs") or []
    skus = payload.get("productSKUs") or []
    names = payload.get("productNames") or []
    out: list[dict] = []
    for i, master in enumerate(masters):
        out.append({
            "master_id": str(master),
            "full_sku": skus[i] if i < len(skus) else "",
            "name": names[i] if i < len(names) else "",
        })
    return out


def _extract_tiles_from_html(text: str) -> list[HokaTile]:
    """HTML 파싱 → HokaTile 리스트.

    전략 (2026-04-14 HTML search 전환):
    1) 스토어프론트 HTML 검색 페이지(`/en/us/search`)는
       `<div class="product" data-pid data-master-id>` 블록 단위로 렌더링.
       각 블록의 이미지 URL `/{master}-{COLOR}_N.png` 에서 색상 코드,
       JSON `&quot;title&quot;:&quot;...&quot;` 에서 상품명을 추출.
    2) 레거시 Coveo-Show 응답(`<div class="tile-row data-tile-analytics>`)
       도 지원 — analytics customData JSON 에서 master/name/sku.
    3) autosuggest 드롭다운 `tile-suggest` 폴백.
    """
    tiles: list[HokaTile] = []
    seen_skus: set[str] = set()

    # 1) 스토어프론트 `<div class="product" data-pid ...>` 블록 (HTML search)
    product_blocks = re.split(r'(?=<div class="product" data-pid=)', text)
    if len(product_blocks) > 1:
        for block in product_blocks[1:]:
            master_m = re.search(r'data-master-id="(\d+)"', block)
            if not master_m:
                continue
            master = master_m.group(1)
            color_m = re.search(rf"/{master}-([A-Z0-9]{{2,10}})_", block)
            if not color_m:
                continue
            color = color_m.group(1)
            sku = f"{master}-{color}"
            if sku in seen_skus:
                continue
            title_m = re.search(r"&quot;title&quot;:&quot;([^&]+)&quot;", block)
            name = html.unescape(title_m.group(1)).strip() if title_m else ""
            price_m = PRICE_RE.search(block)
            try:
                price_usd = float(price_m.group(1)) if price_m else 0.0
            except ValueError:
                price_usd = 0.0
            href_m = TILE_HREF_RE.search(block)
            href = href_m.group(1) if href_m else f"/en/us/p/{master}.html?dwvar_{master}_color={color}"
            block_lower = block.lower()
            available = "sold-out" not in block_lower and "notify me" not in block_lower
            seen_skus.add(sku)
            tiles.append(
                HokaTile(
                    master_id=master,
                    color_code=color,
                    sku=sku,
                    name=name,
                    href=href,
                    url=f"{BASE_URL}{href}",
                    upc="",
                    price_usd=price_usd,
                    available=available,
                )
            )
        if tiles:
            return tiles

    positions = [m.start() for m in TILE_ROW_START_RE.finditer(text)]
    if positions:
        positions.append(len(text))
        for i in range(len(positions) - 1):
            block = text[positions[i] : positions[i + 1]]
            ta_m = TILE_ANALYTICS_RE.search(block)
            if not ta_m:
                continue
            try:
                payload = json.loads(html.unescape(ta_m.group(1)))
            except (ValueError, json.JSONDecodeError):
                continue
            custom = payload.get("customData") or {}
            master = str(custom.get("contentIDValue") or "").strip()
            full_id = str(custom.get("id") or "").strip()  # 1147810-RSLT-10.5B
            name = str(custom.get("name") or "").strip()
            if not master or not full_id:
                continue
            # full_id 에서 color_code 추출 — 형태 master-COLOR-size
            parts = full_id.split("-")
            if len(parts) < 2:
                continue
            color = parts[1]
            sku = f"{master}-{color}"
            if sku in seen_skus:
                continue

            href = ""
            href_m = TILE_HREF_RE.search(block)
            if href_m:
                href = href_m.group(1)

            price_m = PRICE_RE.search(block)
            try:
                price_usd = float(price_m.group(1)) if price_m else 0.0
            except ValueError:
                price_usd = 0.0

            # 품절 감지: 메인 그리드는 `add-to-cart` 영역 대신 `sold-out` 클래스나
            # `Notify Me` 버튼이 노출된다. 블록 내 단순 문자열 탐지.
            block_lower = block.lower()
            available = "sold-out" not in block_lower and "notify me" not in block_lower

            seen_skus.add(sku)
            tiles.append(
                HokaTile(
                    master_id=master,
                    color_code=color,
                    sku=sku,
                    name=name,
                    href=href,
                    url=f"{BASE_URL}{href}" if href else "",
                    upc="",
                    price_usd=price_usd,
                    available=available,
                )
            )
        return tiles

    # Fallback: tile-suggest 경로 (호카 자체 레거시 검색결과 포맷 또는 테스트 픽스처)
    sugg_positions = [m.start() for m in SUGG_PID_RE.finditer(text)]
    sugg_positions.append(len(text))
    for i in range(len(sugg_positions) - 1):
        block = text[sugg_positions[i] : sugg_positions[i + 1]]
        upc_m = SUGG_PID_RE.search(block)
        upc = upc_m.group(1) if upc_m else ""
        href_m = TILE_HREF_RE.search(block)
        if not href_m:
            continue
        href = href_m.group(1)
        master = href_m.group(2)
        color = href_m.group(3)
        sku = f"{master}-{color}"
        if sku in seen_skus:
            continue
        price_m = PRICE_RE.search(block)
        try:
            price_usd = float(price_m.group(1)) if price_m else 0.0
        except ValueError:
            price_usd = 0.0
        name = ""
        name_m = re.search(r'<div class="name">(.*?)</div>', block, flags=re.S)
        if name_m:
            raw = re.sub(r"<[^>]+>", " ", name_m.group(1))
            name = re.sub(r"\s+", " ", html.unescape(raw)).strip()
        seen_skus.add(sku)
        tiles.append(
            HokaTile(
                master_id=master,
                color_code=color,
                sku=sku,
                name=name,
                href=href,
                url=f"{BASE_URL}{href}",
                upc=upc,
                price_usd=price_usd,
                available=True,
            )
        )
    return tiles


class HokaCrawler:
    """호카 Coveo-Show 기반 카탈로그 크롤러 (curl_cffi Safari)."""

    def __init__(self) -> None:
        self._client = None  # lazy: curl_cffi AsyncSession
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=2.0)

    async def _get_client(self):
        if self._client is None:
            # 지연 import — 테스트에서 의존 없이 모듈 로드 가능
            from curl_cffi.requests import AsyncSession

            # DataDome 우회용 프록시 (`HOKA_PROXY_URL` env). 미설정 시 직결.
            # 설정 시 curl_cffi 의 Safari impersonation 유지한 채 프록시 터널링.
            from src.config import settings

            proxy = (settings.hoka_proxy_url or "").strip() or None
            kwargs = {
                "impersonate": "safari17_0",
                "headers": HEADERS,
                "timeout": 20,
            }
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
                logger.info("호카 크롤러 프록시 주입: %s", _redact_proxy(proxy))
            self._client = AsyncSession(**kwargs)
        return self._client

    async def _warmup(self) -> None:
        """첫 요청 전 루트 방문 — DataDome 쿠키/세션 확보용."""
        if getattr(self, "_warmed", False):
            return
        client = await self._get_client()
        try:
            await client.get(f"{BASE_URL}/")
        except Exception:  # warmup 실패해도 본 요청은 진행
            logger.debug("호카 warmup 실패 — 본 요청 계속")
        self._warmed = True

    async def _fetch_coveo(self, keyword: str) -> str:
        client = await self._get_client()
        await self._warmup()
        # 2026-04-14: Coveo XHR 이 DataDome 에 선별 차단되어 HTML search 로 전환.
        # 동일 `tile-row` 그리드를 반환하므로 기존 파서 그대로 재사용.
        url = f"{BASE_URL}{SEARCH_PATH}"
        async with self._rate_limiter.acquire():
            resp = await client.get(
                url,
                params={"q": keyword},
                headers={"Referer": f"{BASE_URL}/en/us/"},
            )
        status = getattr(resp, "status_code", 0)
        if status != 200:
            # DataDome 봇 차단이 걸리면 전 키워드가 동일 상태로 쏟아진다.
            # 반복 경고 대신 첫 실패만 WARN, 이후는 DEBUG 로 낮춰 로그 스팸 방지.
            # `_blocked_streak` 연속 카운트는 상위 `dump_catalog_keywords` 가
            # early-exit 판단에 사용.
            self._blocked_streak = getattr(self, "_blocked_streak", 0) + 1
            if self._blocked_streak == 1:
                logger.warning(
                    "호카 Coveo HTTP %s (keyword=%s) — DataDome 차단 의심, "
                    "이후 동일 상태 DEBUG 강등",
                    status,
                    keyword,
                )
            else:
                logger.debug(
                    "호카 Coveo HTTP %s (keyword=%s, streak=%d)",
                    status,
                    keyword,
                    self._blocked_streak,
                )
            return ""
        self._blocked_streak = 0
        return resp.text or ""

    async def search_products(
        self, keyword: str, limit: int = 30
    ) -> list[dict]:
        """키워드 검색.

        크림봇의 정방향 검색 API 호환 인터페이스. 모델번호(``1176251-FCG``)
        또는 모델명(``Mach 7``) 둘 다 허용한다. 모델번호가 들어오면 master
        부분만 추출해 키워드로 다시 쿼리한 뒤 sku 정합성 검증.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        # 모델번호면 모델명으로 재키워드 — master 만 주면 Coveo 가 name 매핑
        query = keyword
        sku_filter: str | None = None
        m = HOKA_MODEL_RE.search(keyword.upper())
        if m:
            sku_filter = f"{m.group(1)}-{m.group(2)}"
            query = m.group(1)

        text = await self._fetch_coveo(query)
        if not text:
            return []

        tiles = _extract_tiles_from_html(text)
        if sku_filter:
            tiles = [t for t in tiles if t.sku == sku_filter]
        if limit:
            tiles = tiles[:limit]

        results = [
            {
                "product_id": t.master_id,
                "name": t.name,
                "brand": "HOKA",
                "model_number": t.sku,
                "price": int(round(t.price_usd * 1400)),  # 기본 환산
                "original_price": int(round(t.price_usd * 1400)),
                "url": t.url,
                "image_url": "",
                "is_sold_out": not t.available,
                "sizes": [],
            }
            for t in tiles
        ]
        logger.info("호카 검색 '%s': %d건", keyword, len(results))
        return results

    async def dump_catalog_keywords(
        self, keywords: tuple[str, ...] | list[str] | None = None
    ) -> list[HokaTile]:
        """키워드 리스트를 순회해 카탈로그 덤프.

        동일 sku 는 자동 dedup.
        """
        keywords = tuple(keywords) if keywords else HOKA_SEARCH_KEYWORDS
        dedup: dict[str, HokaTile] = {}
        for kw in keywords:
            text = await self._fetch_coveo(kw)
            # 연속 3회 차단이면 나머지 키워드 전부 낭비 — early exit.
            if getattr(self, "_blocked_streak", 0) >= 3:
                logger.warning(
                    "호카 Coveo 연속 차단 %d회 — 남은 키워드 %d개 스킵",
                    self._blocked_streak,
                    len(keywords) - keywords.index(kw) - 1,
                )
                break
            if not text:
                continue
            for tile in _extract_tiles_from_html(text):
                dedup.setdefault(tile.sku, tile)
        logger.info(
            "호카 카탈로그 덤프: 키워드 %d개 → tile %d개",
            len(keywords),
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
        logger.info("호카 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
hoka_crawler = HokaCrawler()
register("hoka", hoka_crawler, "호카")


__all__ = [
    "BASE_URL",
    "COVEO_PATH",
    "HOKA_MODEL_RE",
    "HOKA_SEARCH_KEYWORDS",
    "HokaCrawler",
    "HokaTile",
    "_extract_analytics_masters",
    "_extract_tiles_from_html",
    "hoka_crawler",
]
