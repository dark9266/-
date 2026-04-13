"""더한섬닷컴(thehandsome.com) 크롤러 — Phase 3 배치 5.

한섬(Handsome Corp.) 공식 통합몰. EQL(eqlstore.com) 이 한섬 편집숍이라면
thehandsome.com 은 TIME/SYSTEM/SJSJ/MINE/TOM GREYHOUND/BALLY/LANVIN 등
한섬이 직영/라이선스로 운영하는 프리미엄·럭셔리 브랜드 본진이다.

백엔드 아키텍처:
    - Nuxt2 SSR + Spring Boot REST JSON API
    - `www.thehandsome.com/api/{goods|display|common|member}/1/{locale}/...`
    - GET 전용 (읽기 목적). 인증/CSRF/쿠키 없이도 카탈로그/상세 접근 가능.
    - EQL 은 godNo/godNm HTML 속성 — thehandsome 은 goodsNo/goodsNm JSON.
      **백엔드 완전 분리**. 다만 ERP 네이밍(itmNo/erpBrandNo/onlSzCd/stkQty)
      은 한섬 그룹 공통 — 데이터 스키마만 일부 공유.

엔드포인트 요약 (GET)
---------------------
* 브랜드 리스트        /api/display/1/{locale}/brand/getDispCtgBrandList
                      params: dispCtgNo=10000001 (메인), dispMediaCd=10
* 카테고리 덤프        /api/display/1/{locale}/category/categoryGoodsList
                      params: mainCategory=10000001, brandNo=..., norOutletGbCd=J,
                              sortGbn=10, pageNo=N, rowsPerPage=K (최대 100 추정)
* 상품 상세 + 사이즈   /api/goods/1/{locale}/goods/{goodsNo}
                      payload.colorList[].sizeList[].stkQty / onlSzCd

모델번호 전략
-------------
더한섬 goodsNo 는 `ON2G3ABG001NWU` 같은 한섬 내부 ERP 코드로, 크림
(나이키 SKU, 아디다스 모델번호 등) 과 포맷이 다르다. 현재 크림 DB 129개
브랜드에 한섬 계열 브랜드 교집합은 **0** — 스니커/스트리트 중심 크림
컬렉션과 럭셔리/디자이너 중심 한섬 컬렉션의 타깃이 다르기 때문.

따라서 이 크롤러는:
    1) goodsNo 를 model_number 필드로 그대로 노출 (exact match 만 지원)
    2) 필요 시 상품명/브랜드명 메타만 제공
    3) 어댑터는 대부분 `collected_to_queue` 로 적재 — 향후 크림 럭셔리 확장 대비

읽기 전용 정책
--------------
* GET 만 사용 — 검색/리스팅/상세
* POST 사용 안 함 (로그인/위시/주문 일체 금지)
* min_interval 2.0 초, max_concurrent 2 — 카탈로그 대형·보수적
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("thehandsome_crawler")

BASE_URL = "https://www.thehandsome.com"
LOCALE = "ko"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/{LOCALE}",
}

# 크림 DB 에 있는 (가능성이 있는) 럭셔리/하이엔드 브랜드 — 한섬 계열 중 우선.
# 없으면 전체 브랜드 덤프로 fallback.
DEFAULT_BRAND_WHITELIST: tuple[str, ...] = (
    "BALLY",
    "LANVIN",
    "LANVIN COLLECTION",
    "LANVIN BLANC",
    "FEAR OF GOD",
    "MOOSE KNUCKLES",
    "OUR LEGACY",
    "RE/DONE",
    "DKNY",
    "VERONICA BEARD",
    "NILI LOTAN",
    "AGNONA",
    "ASPESI",
    "TEN C",
)


class ThehandsomeCrawler:
    """더한섬닷컴 크롤러 — Spring Boot REST JSON API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 카탈로그 대형 + 보수적 — 2.0s 간격, 동시 2
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

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("더한섬닷컴 크롤러 연결 해제")

    # ------------------------------------------------------------------
    # 1) 브랜드 리스트
    # ------------------------------------------------------------------
    async def list_brands(self, disp_ctg_no: str = "10000001") -> list[dict]:
        """전체 브랜드 리스트.

        GET /api/display/1/{locale}/brand/getDispCtgBrandList
        """
        client = await self._get_client()
        params = {"dispCtgNo": disp_ctg_no, "dispMediaCd": "10"}

        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(
                    f"{BASE_URL}/api/display/1/{LOCALE}/brand/getDispCtgBrandList",
                    params=params,
                )
            except httpx.HTTPError as exc:
                logger.warning("더한섬 브랜드 리스트 HTTP 에러: %s", exc)
                return []

        if resp.status_code != 200:
            logger.warning("더한섬 브랜드 리스트 HTTP %d", resp.status_code)
            return []

        try:
            data = resp.json()
        except ValueError:
            logger.warning("더한섬 브랜드 리스트 JSON 파싱 실패")
            return []

        if data.get("code") != "0000":
            logger.warning("더한섬 브랜드 리스트 code=%s", data.get("code"))
            return []

        payload = data.get("payload") or []
        if not isinstance(payload, list):
            return []
        logger.info("더한섬 브랜드 리스트: %d개", len(payload))
        return payload

    # ------------------------------------------------------------------
    # 2) 카테고리/브랜드 덤프
    # ------------------------------------------------------------------
    async def dump_brand_goods(
        self,
        brand_no: str,
        *,
        main_category: str = "10000001",
        page_size: int = 40,
        max_pages: int = 5,
    ) -> list[dict]:
        """특정 브랜드의 카탈로그 덤프.

        GET /api/display/1/{locale}/category/categoryGoodsList
        """
        client = await self._get_client()
        all_items: list[dict] = []
        seen_ids: set[str] = set()

        for page_no in range(1, max_pages + 1):
            params: dict[str, str] = {
                "mainCategory": main_category,
                "mainBrand": brand_no,
                "brandNo": brand_no,
                "norOutletGbCd": "J",  # 정가 라인 (O=아울렛)
                "sortGbn": "10",  # 기본 정렬
                "pageNo": str(page_no),
                "rowsPerPage": str(page_size),
                "pageSize": str(page_size),
            }
            async with self._rate_limiter.acquire():
                try:
                    resp = await client.get(
                        f"{BASE_URL}/api/display/1/{LOCALE}/category/categoryGoodsList",
                        params=params,
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "더한섬 브랜드 %s 페이지 %d HTTP 에러: %s",
                        brand_no, page_no, exc,
                    )
                    break

            if resp.status_code != 200:
                logger.warning(
                    "더한섬 브랜드 %s 페이지 %d HTTP %d",
                    brand_no, page_no, resp.status_code,
                )
                break

            try:
                data = resp.json()
            except ValueError:
                logger.warning("더한섬 브랜드 %s JSON 파싱 실패", brand_no)
                break

            if data.get("code") != "0000":
                break

            payload = data.get("payload") or {}
            goods_list = payload.get("goodsList") or []
            if not isinstance(goods_list, list) or not goods_list:
                break

            new_in_page = 0
            for raw in goods_list:
                gid = str(raw.get("goodsNo") or "")
                if not gid or gid in seen_ids:
                    continue
                seen_ids.add(gid)
                all_items.append(self._normalize_listing_item(raw))
                new_in_page += 1

            if new_in_page == 0:
                break
            if len(goods_list) < page_size:
                break

        logger.info("더한섬 브랜드 %s 덤프: %d건", brand_no, len(all_items))
        return all_items

    def _normalize_listing_item(self, raw: dict) -> dict:
        """카테고리 리스팅 응답 → 공통 dict.

        goodsNo 를 model_number 로 사용 (한섬 내부 ERP 코드).
        """
        goods_no = str(raw.get("goodsNo") or "")
        name = raw.get("goodsNm") or ""
        brand = raw.get("brandNm") or raw.get("lowrBrandNm") or ""
        price = int(raw.get("salePrc") or 0)
        nor_price = int(raw.get("norPrc") or price)
        is_sold_out = (raw.get("soutYn") == "Y")
        return {
            "product_id": goods_no,
            "name": name,
            "brand": brand,
            "brand_no": raw.get("brandNo") or "",
            "lowr_brand_nm": raw.get("lowrBrandNm") or "",
            "model_number": goods_no,  # 한섬 ERP 코드 == 모델번호
            "price": price,
            "original_price": nor_price,
            "dc_rate": float(raw.get("dcRate") or 0.0),
            "url": _build_product_url(goods_no),
            "image_url": _build_image_url(raw.get("dispGoodsContUrl") or ""),
            "is_sold_out": is_sold_out,
        }

    # ------------------------------------------------------------------
    # 3) 검색 (일반 키워드) — 어댑터는 사용하지 않지만 API 일관성 목적.
    # ------------------------------------------------------------------
    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """키워드 검색 — 브랜드명 기반. 브랜드 목록에서 매칭되는 brandNo 를
        찾아 `dump_brand_goods` 로 위임한다. 매칭 실패 시 빈 리스트.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        brands = await self.list_brands()
        target_no: str | None = None
        for b in brands:
            nm = (b.get("brandNm") or "").strip().upper()
            if nm == keyword.upper():
                target_no = b.get("brandNo")
                break
        if not target_no:
            logger.info("더한섬 검색 '%s': 브랜드 매칭 없음", keyword)
            return []

        items = await self.dump_brand_goods(
            target_no,
            page_size=min(limit, 40),
            max_pages=1,
        )
        return items[:limit]

    # ------------------------------------------------------------------
    # 4) 상세 + 사이즈별 재고
    # ------------------------------------------------------------------
    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 + 사이즈별 재고.

        GET /api/goods/1/{locale}/goods/{goodsNo}
        payload.colorList[].sizeList[] 순회 → stkQty > 0 인 사이즈만 추출.
        """
        if not product_id:
            return None

        client = await self._get_client()

        async with self._rate_limiter.acquire():
            try:
                resp = await client.get(
                    f"{BASE_URL}/api/goods/1/{LOCALE}/goods/{product_id}"
                )
            except httpx.HTTPError as exc:
                logger.error("더한섬 상세 HTTP 에러 (%s): %s", product_id, exc)
                return None

        if resp.status_code != 200:
            logger.warning("더한섬 상세 HTTP %d (%s)", resp.status_code, product_id)
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("더한섬 상세 JSON 파싱 실패 (%s)", product_id)
            return None

        if data.get("code") != "0000":
            return None

        payload = data.get("payload") or {}
        name = payload.get("goodsNm") or ""
        brand = payload.get("brandNm") or payload.get("lowrBrandNm") or ""

        sizes: list[RetailSizeInfo] = []
        base_price = 0
        color_list = payload.get("colorList") or []
        if not isinstance(color_list, list):
            color_list = []

        for color in color_list:
            if not isinstance(color, dict):
                continue
            color_nor = int(color.get("norPrc") or 0)
            color_sale = int(color.get("salePrc") or color_nor)
            if base_price == 0 and color_sale:
                base_price = color_sale
            size_list = color.get("sizeList") or []
            if not isinstance(size_list, list):
                continue
            for sz in size_list:
                if not isinstance(sz, dict):
                    continue
                # 안전 기본값: in_stock False. stkQty > 0 + statCd=10 일 때만 True.
                qty = int(sz.get("stkQty") or 0)
                stat = str(sz.get("statCd") or "")
                in_stock = qty > 0 and stat == "10"
                if not in_stock:
                    continue
                size_code = sz.get("onlSzCd") or sz.get("erpSzCd") or ""
                sale_prc = int(sz.get("salePrc") or color_sale)
                nor_prc = int(sz.get("norPrc") or color_nor or sale_prc)
                dc = 0.0
                if nor_prc > 0 and sale_prc < nor_prc:
                    dc = 1.0 - (sale_prc / nor_prc)
                sizes.append(
                    RetailSizeInfo(
                        size=str(size_code).strip(),
                        price=sale_prc,
                        original_price=nor_prc,
                        in_stock=True,
                        discount_type="세일" if dc > 0 else "",
                        discount_rate=round(dc, 4),
                    )
                )

        product = RetailProduct(
            source="thehandsome",
            product_id=str(product_id),
            name=name,
            model_number=str(product_id),  # ERP 코드
            brand=brand,
            url=_build_product_url(product_id),
            image_url="",
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "더한섬 상품: %s | 브랜드: %s | %s원 | 사이즈: %d개",
            name, brand,
            f"{base_price:,}" if base_price else "?",
            len(sizes),
        )
        return product

    # ------------------------------------------------------------------
    # 5) 카탈로그 덤프 (어댑터가 호출)
    # ------------------------------------------------------------------
    async def dump_catalog(
        self,
        *,
        brand_whitelist: tuple[str, ...] | None = None,
        page_size: int = 40,
        max_pages: int = 5,
    ) -> list[dict]:
        """지정 브랜드 카탈로그 덤프. whitelist 미지정 시 DEFAULT 사용."""
        wl = tuple(n.upper() for n in (brand_whitelist or DEFAULT_BRAND_WHITELIST))
        brands = await self.list_brands()
        target: list[dict[str, Any]] = []
        for b in brands:
            nm = (b.get("brandNm") or "").strip()
            if nm.upper() in wl:
                target.append(b)
        logger.info("더한섬 카탈로그 덤프 대상 브랜드: %d개", len(target))
        all_items: list[dict] = []
        for b in target:
            brand_no = b.get("brandNo") or ""
            if not brand_no:
                continue
            items = await self.dump_brand_goods(
                brand_no,
                page_size=page_size,
                max_pages=max_pages,
            )
            for it in items:
                it["_brand_label"] = b.get("brandNm") or ""
            all_items.extend(items)
        return all_items


def _build_product_url(goods_no: str) -> str:
    return f"{BASE_URL}/{LOCALE}/PM/productDetail/{goods_no}" if goods_no else ""


def _build_image_url(rel: str) -> str:
    if not rel:
        return ""
    if rel.startswith("http"):
        return rel
    return f"https://cdn-img.thehandsome.com{rel}"


# 정규식 노출 — 모델번호 추출 보조(어댑터 필요 시 사용)
# 한섬 goodsNo 포맷: 14자리 영숫자 (예: ON2G3ABG001NWU)
_HANDSOME_GOODS_NO_RE = re.compile(r"\b[A-Z]{2}\d[A-Z0-9]{10,12}\b")


def extract_goods_no(text: str) -> str:
    """텍스트에서 한섬 goodsNo 패턴 추출 (보조용)."""
    if not text:
        return ""
    m = _HANDSOME_GOODS_NO_RE.search(text)
    return m.group(0) if m else ""


# 싱글톤 + 레지스트리 등록
thehandsome_crawler = ThehandsomeCrawler()
register("thehandsome", thehandsome_crawler, "더한섬닷컴")
