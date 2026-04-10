"""뉴발란스 한국 공식몰(nbkorea.com) 크롤러.

검색 API가 POST 전용이라 카테고리 SSR 매핑 + 재고 GET API 하이브리드 방식.
매핑: 카테고리 SSR HTML에서 displayName -> styleCode 변환 테이블 구축.
재고: /product/light/getOtherColorOptInfo.action (GET, JSON)
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

logger = setup_logger("nbkorea_crawler")

BASE_URL = "https://www.nbkorea.com"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/",
}

JSON_HEADERS = {
    **HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

# 카테고리 코드 — 신발 위주
NB_CATEGORIES = [
    "250110",  # 남성 신발
    "250210",  # 여성 신발
    "250310",  # 키즈 신발
]

# 매핑 캐시 (12시간 TTL)
_NB_MAPPING_TTL = 12 * 3600
_nb_mapping: dict | None = None  # {"ts": float, "map": {display_name -> [(style_code, col_code)]}}
_nb_mapping_lock = asyncio.Lock()

# SSR HTML 파싱 패턴
# data-style="NBP7GS114F" data-col="85" data-display-name="U20024VT"
_PRODUCT_ATTR_RE = re.compile(
    r'data-style="([^"]+)"[^>]*?data-col="([^"]+)"[^>]*?data-display-name="([^"]+)"',
    re.DOTALL,
)
# 역순 속성도 대비 — display-name이 style보다 앞에 올 수 있음
_PRODUCT_ATTR_RE_ALT = re.compile(
    r'data-display-name="([^"]+)"[^>]*?data-style="([^"]+)"[^>]*?data-col="([^"]+)"',
    re.DOTALL,
)

# URL 패턴: /product/productDetail.action?styleCode=XXX&colCode=YY
_URL_STYLE_RE = re.compile(
    r'styleCode=([A-Za-z0-9]+)(?:&|&amp;)colCode=([A-Za-z0-9]+)'
)

# 상품명에서 모델번호 추출 (NB 모델 형태: M2002RXD, U20024VT, ML860XA 등)
_NB_MODEL_RE = re.compile(r'\b[A-Z]{1,4}\d{3,5}[A-Z0-9]{0,4}\b')


def _normalize_nb_model(model: str) -> str:
    """모델번호 정규화 — 대문자 변환, 공백/하이픈 제거."""
    return model.strip().upper().replace(" ", "").replace("-", "")


def _parse_category_mapping(html: str) -> dict[str, list[tuple[str, str]]]:
    """카테고리 SSR HTML에서 {display_name: [(style_code, col_code), ...]} 추출.

    두 가지 패턴을 시도:
    1. data-style / data-col / data-display-name 속성
    2. productDetail.action URL 파라미터 + 근처 텍스트에서 모델명
    """
    mapping: dict[str, list[tuple[str, str]]] = {}

    # 패턴 1: data-* 속성
    for m in _PRODUCT_ATTR_RE.finditer(html):
        style_code, col_code, display_name = m.group(1), m.group(2), m.group(3)
        norm = _normalize_nb_model(display_name)
        if norm:
            mapping.setdefault(norm, [])
            pair = (style_code, col_code)
            if pair not in mapping[norm]:
                mapping[norm].append(pair)

    # 패턴 1-alt: 속성 순서 역전
    for m in _PRODUCT_ATTR_RE_ALT.finditer(html):
        display_name, style_code, col_code = m.group(1), m.group(2), m.group(3)
        norm = _normalize_nb_model(display_name)
        if norm:
            mapping.setdefault(norm, [])
            pair = (style_code, col_code)
            if pair not in mapping[norm]:
                mapping[norm].append(pair)

    return mapping


def _parse_prod_opt(prod_opt: list[dict]) -> list[dict]:
    """prodOpt 배열 -> [{size, price, original_price, in_stock, model_number, style_code}, ...]

    Qty > 0 = in_stock, SizeName = 사이즈, Price = 판매가, NorPrice = 정상가.
    """
    rows: list[dict] = []
    for item in prod_opt:
        size_name = str(item.get("SizeName") or "").strip()
        if not size_name:
            continue
        qty = int(item.get("Qty") or 0)
        price = int(item.get("Price") or 0)
        nor_price = int(item.get("NorPrice") or 0)
        rows.append({
            "size": size_name,
            "price": price,
            "original_price": nor_price or price,
            "in_stock": qty > 0,
            "model_number": str(item.get("DispStyleName") or "").strip(),
            "style_code": str(item.get("StyleCode") or "").strip(),
            "col_code": str(item.get("ColCode") or "").strip(),
        })
    return rows


class NbKoreaCrawler:
    """뉴발란스 한국 공식몰(nbkorea.com) 크롤러."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=2.0)

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
        logger.info("NB Korea 크롤러 연결 해제")

    # ----- low-level API -----

    async def _fetch_category_page(self, cate_code: str) -> str:
        """카테고리 SSR HTML 조회."""
        client = await self._get_client()
        url = f"{BASE_URL}/product/productList.action"
        params = {"cateGrpCode": cate_code}
        async with self._rate_limiter.acquire():
            resp = await client.get(url, params=params, headers=HEADERS)
        if resp.status_code != 200:
            logger.warning("NB Korea 카테고리 HTTP %d (%s)", resp.status_code, cate_code)
            return ""
        return resp.text

    async def _fetch_opt_info(self, style_code: str, col_code: str) -> dict:
        """재고/가격 JSON API 조회."""
        client = await self._get_client()
        url = f"{BASE_URL}/product/light/getOtherColorOptInfo.action"
        params = {
            "comStyleCode": style_code,
            "styleCode": style_code,
            "colCode": col_code,
        }
        async with self._rate_limiter.acquire():
            resp = await client.get(url, params=params, headers=JSON_HEADERS)
        if resp.status_code != 200:
            logger.warning(
                "NB Korea opt HTTP %d (%s/%s)", resp.status_code, style_code, col_code
            )
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    async def _fetch_opt_status(self, style_code: str, col_code: str) -> dict:
        """상태 확인 API (품절/출시예정)."""
        client = await self._get_client()
        url = f"{BASE_URL}/product/light/getOtherColorOptStatus.action"
        params = {"styleCode": style_code, "colCode": col_code}
        async with self._rate_limiter.acquire():
            resp = await client.get(url, params=params, headers=JSON_HEADERS)
        if resp.status_code != 200:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ----- 매핑 캐시 -----

    async def _get_mapping(self) -> dict[str, list[tuple[str, str]]]:
        """카테고리 SSR에서 display_name -> [(style_code, col_code)] 매핑 구축.

        12시간 TTL 캐시.
        """
        global _nb_mapping
        async with _nb_mapping_lock:
            now = time.monotonic()
            if _nb_mapping and now - _nb_mapping["ts"] < _NB_MAPPING_TTL:
                return _nb_mapping["map"]

            merged: dict[str, list[tuple[str, str]]] = {}
            for cate in NB_CATEGORIES:
                try:
                    html = await self._fetch_category_page(cate)
                    if html:
                        partial = _parse_category_mapping(html)
                        for k, v in partial.items():
                            merged.setdefault(k, [])
                            for pair in v:
                                if pair not in merged[k]:
                                    merged[k].append(pair)
                except Exception as exc:
                    logger.warning("NB Korea 카테고리 %s 파싱 실패: %s", cate, exc)

            _nb_mapping = {"ts": now, "map": merged}
            logger.info("NB Korea 매핑 캐시 갱신: %d건", len(merged))
            return merged

    # ----- 인터페이스 -----

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """모델번호(크림 포맷)로 NB Korea 상품 검색.

        1. keyword 정규화
        2. 매핑 캐시에서 display_name 조회
        3. 매칭 시 재고 API 호출 -> 사이즈별 가격/재고 반환
        4. 미매칭 시 빈 리스트 반환
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        norm_kw = _normalize_nb_model(keyword)
        if len(norm_kw) < 4:
            return []

        mapping = await self._get_mapping()
        pairs = mapping.get(norm_kw)
        if not pairs:
            return []

        results: list[dict] = []
        for style_code, col_code in pairs[:limit]:
            try:
                data = await self._fetch_opt_info(style_code, col_code)
            except Exception as exc:
                logger.debug("NB Korea opt 조회 실패 (%s/%s): %s", style_code, col_code, exc)
                continue

            prod_opt = data.get("prodOpt") or []
            if not prod_opt:
                continue

            # 첫 항목에서 기본 정보 추출
            first = prod_opt[0]
            display_name = first.get("DispStyleName") or keyword
            price = int(first.get("Price") or 0)

            sizes = _parse_prod_opt(prod_opt)
            in_stock_sizes = [s for s in sizes if s["in_stock"]]

            product_id = f"{style_code}_{col_code}"
            url = (
                f"{BASE_URL}/product/productDetail.action"
                f"?styleCode={style_code}&colCode={col_code}"
            )

            results.append({
                "product_id": product_id,
                "name": display_name,
                "brand": "New Balance",
                "model_number": keyword,
                "price": price,
                "original_price": int(first.get("NorPrice") or price),
                "url": url,
                "image_url": "",
                "is_sold_out": len(in_stock_sizes) == 0,
                "sizes": sizes,
                "_style_code": style_code,
                "_col_code": col_code,
            })

        return results[:limit]

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 + 사이즈별 재고.

        product_id: "{styleCode}_{colCode}" 형식.
        """
        if "_" not in product_id:
            logger.warning("NB Korea 잘못된 product_id: %s", product_id)
            return None

        style_code, col_code = product_id.split("_", 1)

        # 상태 확인
        try:
            status = await self._fetch_opt_status(style_code, col_code)
        except Exception:
            status = {}

        if status.get("soldOutYn") == "Y":
            logger.info("NB Korea 품절 상품 스킵: %s", product_id)
            return None

        if status.get("comingSoonYn") == "Y":
            logger.info("NB Korea 발매예정 상품 스킵: %s", product_id)
            return None

        # 재고/가격
        try:
            data = await self._fetch_opt_info(style_code, col_code)
        except Exception as exc:
            logger.error("NB Korea 상품 조회 에러 (%s): %s", product_id, exc)
            return None

        prod_opt = data.get("prodOpt") or []
        if not prod_opt:
            return None

        first = prod_opt[0]
        display_name = first.get("DispStyleName") or ""
        model_number = display_name or style_code

        rows = _parse_prod_opt(prod_opt)
        sizes: list[RetailSizeInfo] = []
        for r in rows:
            if not r["in_stock"]:
                continue
            sale_price = r["original_price"]
            opt_price = r["price"]
            discount_rate = 0.0
            if sale_price and opt_price and sale_price > opt_price:
                discount_rate = round(1 - opt_price / sale_price, 3)
            sizes.append(
                RetailSizeInfo(
                    size=r["size"],
                    price=opt_price,
                    original_price=sale_price,
                    in_stock=True,
                    discount_type="할인" if discount_rate > 0 else "",
                    discount_rate=discount_rate,
                )
            )

        url = (
            f"{BASE_URL}/product/productDetail.action"
            f"?styleCode={style_code}&colCode={col_code}"
        )

        product = RetailProduct(
            source="nbkorea",
            product_id=product_id,
            name=display_name or model_number,
            model_number=model_number,
            brand="New Balance",
            url=url,
            image_url="",
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "NB Korea 상품: %s | 모델: %s | %s원 | 사이즈: %d개",
            display_name,
            model_number,
            f"{rows[0]['price']:,}" if rows else "?",
            len(sizes),
        )
        return product


# 싱글톤 + 레지스트리 등록
nbkorea_crawler = NbKoreaCrawler()
register("nbkorea", nbkorea_crawler, "뉴발란스")
