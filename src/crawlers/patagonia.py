"""파타고니아 코리아(patagonia.co.kr) 크롤러 — Phase 3 배치 6.

배경
----
파타고니아 코리아는 forbiz 기반 자체 몰. `www.patagonia.co.kr` 직판.
GET 공개 API 하나로 전체 카탈로그(현재 345건)를 한 번에 덤프 가능.
crem DB 에는 파타고니아 2,500건 등록(5자리 스타일 코드 매칭).

엔드포인트
----------
GET /controller/product/getGoodslist?cid={cid}&page={n}
    - Content-Type: application/json
    - 응답: {result, data:{total, list:[{pcode, pname, sellprice,
              options:{colors, sizes, options}, status_soldout, ...}]}}
    - 관측: cid/page 파라미터가 사실상 무시되며 전체 345건이 항상 반환된다.
      (forbiz 런타임 세션 기반 필터링인 듯) 한 번 호출로 카탈로그 전체 확보.

모델번호 매칭
-------------
- 사이트 pcode: ``44937R5`` = **5자리 스타일(44937)** + 시즌 revision(R5)
- 크림 model_number: ``44937`` (5자리) 또는 ``25580/25551`` (복수 구분자)
- 매칭 키: pcode 앞 5자리(스타일 코드). 크림 쪽은 ``/`` 구분자 분리 후 조인.

사이즈/재고
----------
- 각 product 의 ``options.options[].option_stock`` 에 실재고 정수 노출
- ``option_code`` 는 "COLOR|HEX|SIZE" 포맷 (예: "CGBX|696946|M")
- 품절 여부: ``status_soldout`` 또는 모든 option_stock == 0

읽기 전용. GET only.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from src.crawlers.registry import register
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("patagonia_crawler")

BASE_URL = "https://www.patagonia.co.kr"
LIST_API = f"{BASE_URL}/controller/product/getGoodslist"
VIEW_PATH = "/shop/goodsView"

# 기본 덤프 카테고리 — 현재 cid 파라미터는 사실상 무시되지만
# 인터페이스 호환을 위해 최상위 카테고리 목록을 유지.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "001001000000000",  # Women's
    "001002000000000",  # Men's
    "001003000000000",  # Kids'
    "001004000000000",  # Equipment
)

# pcode: 5자리 style + revision 2자리 (예: 44937R5, 25580F4)
PATAGONIA_PCODE_RE = re.compile(r"^(\d{5})([A-Z]\d)?$")
# 크림 model_number 분리자 (복수 코드 "85240/85241")
KREAM_MN_SEP_RE = re.compile(r"[/,;\s]+")

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/",
}


# --------------- 순수 파싱 함수 ---------------


def extract_style_code(pcode: str) -> str:
    """파타고니아 pcode → 5자리 스타일 코드.

    Examples:
        "44937R5" -> "44937"
        "25580F4" -> "25580"
        "44937"   -> "44937"
        ""        -> ""
    """
    if not pcode:
        return ""
    m = PATAGONIA_PCODE_RE.match(pcode.strip().upper())
    if m:
        return m.group(1)
    # fallback — 앞 5자리가 숫자면 그대로
    stripped = re.sub(r"[^0-9A-Z]", "", pcode.upper())
    if len(stripped) >= 5 and stripped[:5].isdigit():
        return stripped[:5]
    return ""


def split_kream_model_numbers(model_number: str) -> list[str]:
    """크림 model_number 문자열 → 5자리 코드 리스트.

    Examples:
        "85240/85241" -> ["85240", "85241"]
        "24142"       -> ["24142"]
        ""            -> []
    """
    if not model_number:
        return []
    out: list[str] = []
    for tok in KREAM_MN_SEP_RE.split(model_number):
        tok = tok.strip().upper()
        if not tok:
            continue
        code = extract_style_code(tok)
        if code:
            out.append(code)
    return out


def parse_sizes_from_options(options_block: dict) -> list[dict]:
    """`options.options` 배열 → 표준 사이즈 재고 리스트.

    option_code 포맷: "COLOR|HEX|SIZE" (예: "CGBX|696946|M")
    option_stock: int (실재고)
    """
    result: list[dict] = []
    if not isinstance(options_block, dict):
        return result
    raw = options_block.get("options") or []
    if not isinstance(raw, list):
        return result
    for o in raw:
        if not isinstance(o, dict):
            continue
        code = str(o.get("option_code") or "")
        parts = code.split("|")
        size = parts[2] if len(parts) >= 3 else ""
        try:
            stock = int(o.get("option_stock") or 0)
        except (TypeError, ValueError):
            stock = 0
        result.append({
            "size": size.strip(),
            "stock": stock,
            "in_stock": stock > 0,
            "color": parts[0] if parts else "",
        })
    return result


def is_soldout(product: dict) -> bool:
    """상품 전체 품절 여부. status_soldout 또는 모든 옵션 재고 0."""
    if product.get("status_soldout"):
        return True
    sizes = parse_sizes_from_options(product.get("options") or {})
    if not sizes:
        # 사이즈 정보 자체가 없으면 보수적으로 재고 없음 간주
        return True
    return not any(s.get("in_stock") for s in sizes)


def _parse_price(raw: Any) -> int:
    """"169,000" → 169000. 숫자/빈 처리."""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().replace(",", "")
    if not s:
        return 0
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def _parse_product(raw: dict) -> dict:
    """API raw product → 어댑터/스캐너 공용 dict."""
    pcode = str(raw.get("pcode") or "").strip()
    style = extract_style_code(pcode)
    pid = str(raw.get("id") or "").strip()
    sell = _parse_price(raw.get("sellprice") or raw.get("dcprice"))
    list_price = _parse_price(raw.get("listprice") or raw.get("sellprice"))
    sizes = parse_sizes_from_options(raw.get("options") or {})
    url = f"{BASE_URL}{VIEW_PATH}/{pid}" if pid else ""
    return {
        "product_id": pid,
        "pcode": pcode,
        "style_code": style,
        "model_number": style,  # 크림 매칭용 키
        "name": raw.get("pname") or "",
        "name_kr": raw.get("pname_kr") or "",
        "brand": "Patagonia",
        "price": sell,
        "original_price": list_price,
        "url": url,
        "image_url": raw.get("image_src") or "",
        "is_sold_out": is_soldout(raw),
        "is_specialty_only": bool(raw.get("is_specialty_only")),
        "sizes": sizes,
    }


# --------------- 크롤러 ---------------


class PatagoniaCrawler:
    """파타고니아 코리아 getGoodslist 기반 크롤러."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=2.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    async def _fetch_list_raw(
        self, cid: str, page: int = 1
    ) -> dict:
        """카테고리 리스트 GET. 품절 포함 전체 응답 JSON 반환."""
        client = await self._get_client()
        params = {"cid": cid, "page": str(page)}
        async with self._rate_limiter.acquire():
            resp = await client.get(LIST_API, params=params)
        if resp.status_code != 200:
            logger.warning(
                "파타고니아 list HTTP %s cid=%s page=%s",
                resp.status_code,
                cid,
                page,
            )
            return {}
        try:
            return resp.json()
        except ValueError:
            logger.warning("파타고니아 list JSON 파싱 실패 cid=%s", cid)
            return {}

    async def fetch_catalog(
        self, categories: tuple[str, ...] | None = None
    ) -> list[dict]:
        """카테고리 순회 덤프. 중복 pcode 는 dedup.

        Notes
        -----
        현재 API 는 cid 와 무관하게 전체 목록을 반환하지만, 차후 API
        변경 대비로 인터페이스는 다중 카테고리를 유지한다.
        """
        cats = categories or DEFAULT_CATEGORIES
        dedup: dict[str, dict] = {}
        for cid in cats:
            data = await self._fetch_list_raw(cid, page=1)
            items = ((data or {}).get("data") or {}).get("list") or []
            if not isinstance(items, list):
                continue
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                parsed = _parse_product(raw)
                pcode = parsed["pcode"]
                if not pcode or pcode in dedup:
                    continue
                dedup[pcode] = parsed
        logger.info(
            "파타고니아 카탈로그 덤프: 카테고리 %d개 → %d건",
            len(cats),
            len(dedup),
        )
        return list(dedup.values())

    async def search_products(
        self, keyword: str, limit: int = 30
    ) -> list[dict]:
        """키워드 검색(클라이언트 측 필터).

        getGoodslist 가 키워드 검색을 지원하지 않아 전체 카탈로그를
        덤프한 뒤 pcode/name 부분 일치로 필터. 호출 횟수 억제를 위해
        호출자가 빈번히 부르지 않도록 주의. 크림봇 정방향 스캔 호환.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        catalog = await self.fetch_catalog()
        kw_upper = keyword.upper()
        style_hint = extract_style_code(kw_upper)
        results: list[dict] = []
        for item in catalog:
            hit = False
            if style_hint and item.get("style_code") == style_hint:
                hit = True
            elif kw_upper in (item.get("pcode") or "").upper():
                hit = True
            elif kw_upper in (item.get("name") or "").upper():
                hit = True
            elif kw_upper in (item.get("name_kr") or "").upper():
                hit = True
            if hit:
                results.append(item)
                if len(results) >= limit:
                    break
        logger.info("파타고니아 검색 '%s': %d건", keyword, len(results))
        return results

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover
                pass
            self._client = None
        logger.info("파타고니아 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
patagonia_crawler = PatagoniaCrawler()
register("patagonia", patagonia_crawler, "파타고니아")


__all__ = [
    "BASE_URL",
    "DEFAULT_CATEGORIES",
    "LIST_API",
    "PATAGONIA_PCODE_RE",
    "PatagoniaCrawler",
    "_parse_product",
    "extract_style_code",
    "is_soldout",
    "parse_sizes_from_options",
    "patagonia_crawler",
    "split_kream_model_numbers",
]
