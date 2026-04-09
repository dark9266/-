"""카시나(kasina.co.kr) 크롤러.

NHN Commerce shopby 백엔드 API 사용. GET 전용, 인증 불필요.

매칭 전략
---------
* 나이키/아디다스: `productManagementCd`가 크림 포맷 그대로 → EXACT 1회 질의.
* 뉴발란스: `productManagementCd`는 카시나 내부 SKU. productName/productNameEn에
  실제 모델(`U2000ETC`, `M2002RXD` 등)이 포함되므로 브랜드 전량 덤프 → regex 매칭.

samples
-------
/tmp/kasina_sample_search.json, kasina_sample_detail.json, kasina_sample_options.json
탐색 스크립트: scripts/probe_kasina.py
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("kasina_crawler")

BASE = "https://shop-api-secondary.shopby.co.kr"
WEB_BASE = "https://www.kasina.co.kr"

HEADERS = {
    "company": "Kasina/Request",
    "clientid": "183SVEgDg5nHbILW//3jvg==",
    "platform": "PC",
    "version": "1.0",
    "Accept": "application/json",
    "Origin": WEB_BASE,
    "Referer": f"{WEB_BASE}/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# 카시나 brandNo
BRAND_NIKE = 40331479
BRAND_ADIDAS = 40331389
BRAND_NEWBALANCE = 40331363

# 크림 포맷 모델번호 (예: HF3704-003, IB7862-100)
KREAM_MODEL_RE = re.compile(r"^[A-Z]{1,4}\d{3,5}-\d{2,4}$")
# 상품명 안에서 추출할 모델 토큰 (NB U2000ETC, ML860XA 등)
NB_INLINE_RE = re.compile(r"\b[A-Z]{1,4}\d{3,5}[A-Z0-9]{0,4}\b")

# 뉴발란스 브랜드 덤프 캐시 (6시간)
_NB_CACHE_TTL = 6 * 3600
_nb_cache: dict | None = None  # {"ts": float, "items": list[dict]}
_nb_cache_lock = asyncio.Lock()


def _normalize_token(t: str) -> str:
    return t.upper().replace(" ", "").replace("-", "")


def _build_url(product_no: int | str) -> str:
    return f"{WEB_BASE}/products/{product_no}"


def _parse_search_item(it: dict) -> dict:
    """shopby search item → 표준 dict."""
    pno = it.get("productNo")
    return {
        "product_id": str(pno) if pno is not None else "",
        "name": it.get("productName") or it.get("productNameEn") or "",
        "brand": (it.get("brandNameKo") or it.get("brandNameEn") or "").strip(),
        "model_number": it.get("productManagementCd") or "",
        "price": int(it.get("salePrice") or 0),
        "original_price": int(it.get("salePrice") or 0),
        "url": _build_url(pno) if pno else "",
        "image_url": "",
        "is_sold_out": bool(it.get("isSoldOut", False)),
        # sizes는 reverse_scanner._enrich_missing_sizes 가 get_product_detail 호출로 채움
    }


def _parse_sizes_from_options(options: dict) -> list[dict]:
    """options 응답 → [{size, in_stock, price}, ...]

    multiLevelOptions[*].children[*] 구조 (COLOR > SIZE).
    SIZE 단일 레이어일 경우 children 없이 multiLevelOptions[*]가 사이즈.
    """
    rows: list[dict] = []
    nodes = options.get("multiLevelOptions") or []

    for top in nodes:
        children = top.get("children")
        if children:
            # COLOR 레이어 → SIZE children
            for c in children:
                _append_size_row(rows, c)
        else:
            # SIZE 단일 레이어
            _append_size_row(rows, top)

    return rows


def _append_size_row(rows: list[dict], node: dict) -> None:
    size = (node.get("value") or "").strip()
    if not size:
        return
    sale_type = node.get("saleType")
    forced = bool(node.get("forcedSoldOut", False))
    in_stock = (sale_type == "AVAILABLE") and not forced
    price = int(node.get("buyPrice") or 0)
    rows.append({"size": size, "in_stock": in_stock, "price": price})


class KasinaCrawler:
    """카시나 (kasina.co.kr) 크롤러 — NHN shopby API."""

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
        logger.info("카시나 크롤러 연결 해제")

    # ----- low-level API -----

    async def _search_raw(
        self,
        *,
        management_cd: str | None = None,
        brand_no: int | None = None,
        page_size: int = 20,
        page_number: int = 1,
    ) -> dict:
        client = await self._get_client()
        params: dict[str, str] = {
            "pageNumber": str(page_number),
            "pageSize": str(page_size),
            "order.by": "POPULAR",
            "order.direction": "DESC",
        }
        if management_cd:
            params["filter.productManagementCd"] = management_cd
        if brand_no is not None:
            params["brandNos"] = str(brand_no)
        async with self._rate_limiter.acquire():
            resp = await client.get(f"{BASE}/products/search", params=params)
        if resp.status_code != 200:
            logger.warning("Kasina 검색 HTTP %d (%s)", resp.status_code, params)
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    async def _detail_raw(self, product_no: str) -> dict:
        client = await self._get_client()
        async with self._rate_limiter.acquire():
            resp = await client.get(f"{BASE}/products/{product_no}")
        if resp.status_code != 200:
            logger.warning("Kasina detail HTTP %d (%s)", resp.status_code, product_no)
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    async def _options_raw(self, product_no: str) -> dict:
        client = await self._get_client()
        async with self._rate_limiter.acquire():
            resp = await client.get(f"{BASE}/products/{product_no}/options")
        if resp.status_code != 200:
            logger.warning("Kasina options HTTP %d (%s)", resp.status_code, product_no)
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ----- 뉴발란스 브랜드 덤프 캐시 -----

    async def _get_nb_dump(self) -> list[dict]:
        global _nb_cache
        async with _nb_cache_lock:
            now = time.monotonic()
            if _nb_cache and now - _nb_cache["ts"] < _NB_CACHE_TTL:
                return _nb_cache["items"]

            items: list[dict] = []
            page = 1
            while True:
                data = await self._search_raw(
                    brand_no=BRAND_NEWBALANCE, page_size=100, page_number=page,
                )
                chunk = data.get("items") or []
                items.extend(chunk)
                total = int(data.get("totalCount") or 0)
                if not chunk or len(items) >= total or page >= 10:
                    break
                page += 1

            _nb_cache = {"ts": now, "items": items}
            logger.info("Kasina NB 브랜드 덤프 갱신: %d건", len(items))
            return items

    # ----- 인터페이스 -----

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """모델번호로 카시나 상품 검색.

        호출자(reverse_scanner)는 단일 model_number를 keyword로 넘긴다.

        전략:
          A. 크림 포맷이면 EXACT productManagementCd 질의 (Nike/adidas).
          B. 그 외(또는 EXACT 미적중 시)이고 NB-like이면 NB 브랜드 덤프 + regex.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        results: list[dict] = []
        seen: set[str] = set()

        # A. EXACT 질의 — productManagementCd 직접 조회 (Nike/adidas/카시나 내부 SKU)
        # 카시나 mgmt 코드는 Nike(`HF3704-003`), adidas(`KI0824`), NB 내부(`NBPDFF003Z`)
        # 등 패턴이 다양 → regex 검증 없이 항상 시도. 1 call로 0 또는 1건.
        data = await self._search_raw(management_cd=keyword, page_size=5)
        for it in data.get("items") or []:
            row = _parse_search_item(it)
            if row["product_id"] and row["product_id"] not in seen:
                results.append(row)
                seen.add(row["product_id"])

        if results:
            return results[:limit]

        # B. NB regex 매칭 — Kream 포맷이 아닌 NB 모델일 때만 (브랜드 덤프 + 정규식)
        norm_kw = _normalize_token(keyword)
        if not KREAM_MODEL_RE.match(keyword) and len(norm_kw) >= 4:
            try:
                items = await self._get_nb_dump()
            except Exception as exc:
                logger.debug("Kasina NB 덤프 실패: %s", exc)
                items = []

            for it in items:
                hay = " ".join(
                    filter(None, [it.get("productName"), it.get("productNameEn")])
                )
                tokens = NB_INLINE_RE.findall(hay)
                norm_tokens = {_normalize_token(t) for t in tokens}
                if norm_kw in norm_tokens:
                    row = _parse_search_item(it)
                    # NB는 productManagementCd가 내부 SKU → 매칭용으로 keyword 노출
                    row["model_number"] = keyword
                    if row["product_id"] and row["product_id"] not in seen:
                        results.append(row)
                        seen.add(row["product_id"])
                    if len(results) >= limit:
                        break

        return results[:limit]

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 + 사이즈별 재고."""
        try:
            detail, options = await asyncio.gather(
                self._detail_raw(product_id),
                self._options_raw(product_id),
            )
        except Exception as exc:
            logger.error("Kasina 상품 조회 에러 (%s): %s", product_id, exc)
            return None

        if not detail:
            return None

        base = detail.get("baseInfo") or {}
        price_info = detail.get("price") or {}
        status = detail.get("status") or {}
        brand_info = detail.get("brand") or {}

        if status.get("soldout"):
            logger.info("Kasina 품절 상품 스킵: %s", product_id)
            return None

        sale_status = status.get("saleStatusType")
        if sale_status and sale_status not in ("ONSALE", "READY"):
            logger.info("Kasina 비판매 상품 스킵: %s (%s)", product_id, sale_status)
            return None

        name = base.get("productName") or base.get("productNameEn") or ""
        model_number = base.get("productManagementCd") or ""
        sale_price = int(price_info.get("salePrice") or 0)
        immediate_disc = int(price_info.get("immediateDiscountAmt") or 0)
        current_price = sale_price - immediate_disc if immediate_disc > 0 else sale_price

        size_rows = _parse_sizes_from_options(options) if options else []
        sizes: list[RetailSizeInfo] = []
        for r in size_rows:
            if not r["in_stock"]:
                continue
            opt_price = r["price"] or current_price
            discount_rate = 0.0
            if sale_price and opt_price and sale_price > opt_price:
                discount_rate = round(1 - opt_price / sale_price, 3)
            sizes.append(
                RetailSizeInfo(
                    size=r["size"],
                    price=opt_price,
                    original_price=sale_price or opt_price,
                    in_stock=True,
                    discount_type="할인" if discount_rate > 0 else "",
                    discount_rate=discount_rate,
                )
            )

        product = RetailProduct(
            source="kasina",
            product_id=str(product_id),
            name=name,
            model_number=model_number,
            brand=brand_info.get("nameKo") or brand_info.get("name") or "",
            url=_build_url(product_id),
            image_url="",
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "Kasina 상품: %s | 모델: %s | %s원 | 사이즈: %d개",
            name, model_number,
            f"{current_price:,}" if current_price else "?",
            len(sizes),
        )
        return product


# 싱글톤 + 레지스트리 등록
kasina_crawler = KasinaCrawler()
register("kasina", kasina_crawler, "카시나")
