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
COVEO_PATH = "/on/demandware.store/Sites-HOKA-US-Site/en_US/Coveo-Show"

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

# data-suggestion-pid (UPC 12자리)
SUGG_PID_RE = re.compile(r'data-suggestion-pid="(\d{10,14})"')

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

    전략: `data-suggestion-pid` 블록 단위로 자르고 각 블록에서 href/name/price
    를 독립 파싱. 블록 경계가 모호하면 href 기준 단순 fallback.
    """
    tiles: list[HokaTile] = []

    # 블록 분리: tile 루트 `<div class="... tile-suggest ... data-suggestion-pid="..."`
    # 이후 다음 `data-suggestion-pid` 까지를 한 블록으로 본다.
    positions = [m.start() for m in SUGG_PID_RE.finditer(text)]
    positions.append(len(text))

    seen_skus: set[str] = set()
    for i in range(len(positions) - 1):
        block = text[positions[i] : positions[i + 1]]

        upc_m = SUGG_PID_RE.search(block)
        upc = upc_m.group(1) if upc_m else ""

        href_m = TILE_HREF_RE.search(block)
        if not href_m:
            continue
        href = href_m.group(1)
        master = href_m.group(2)
        color = href_m.group(3)
        sku = f"{master}-{color}"

        # tile 내 첫 번째 가격을 USD 로 채택
        price_m = PRICE_RE.search(block)
        try:
            price_usd = float(price_m.group(1)) if price_m else 0.0
        except ValueError:
            price_usd = 0.0

        # 상품명: `<div class="name">` 블록
        name = ""
        name_m = re.search(
            r'<div class="name">(.*?)</div>', block, flags=re.S
        )
        if name_m:
            raw = name_m.group(1)
            # 태그 제거 + 엔티티 복원 + 공백 정리
            raw = re.sub(r"<[^>]+>", " ", raw)
            name = re.sub(r"\s+", " ", html.unescape(raw)).strip()

        if sku in seen_skus:
            continue
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

            self._client = AsyncSession(
                impersonate="safari17_0",
                headers=HEADERS,
                timeout=20,
            )
        return self._client

    async def _fetch_coveo(self, keyword: str) -> str:
        client = await self._get_client()
        url = f"{BASE_URL}{COVEO_PATH}"
        async with self._rate_limiter.acquire():
            resp = await client.get(url, params={"q": keyword, "qs": "1"})
        if getattr(resp, "status_code", 0) != 200:
            logger.warning(
                "호카 Coveo HTTP %s (keyword=%s)",
                getattr(resp, "status_code", "?"),
                keyword,
            )
            return ""
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
