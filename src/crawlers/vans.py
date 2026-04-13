"""반스 공식몰(vans.co.kr) 크롤러.

Topick Commerce 플랫폼.
검색: JSON API, 상세: HTML data-sku-data 파싱.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("vans_crawler")

BASE_URL = "https://www.vans.co.kr"
SEARCH_URL = f"{BASE_URL}/search/ajax"

HEADERS = {
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


# --------------- 순수 파싱 함수 ---------------


def _parse_price(price_str: str) -> int:
    """가격 문자열 → 정수 변환.

    "39,200 원" → 39200
    "69000" → 69000
    "" → 0
    """
    if not price_str:
        return 0
    cleaned = re.sub(r"[^\d]", "", price_str)
    return int(cleaned) if cleaned else 0


def _parse_sku_data(html: str) -> list[dict]:
    """HTML에서 data-sku-data 속성의 JSON 배열 추출.

    data-sku-data='[{...}, {...}]' 형태.
    """
    match = re.search(r"data-sku-data='(\[.*?\])'", html)
    if not match:
        # 큰따옴표 버전도 시도
        match = re.search(r'data-sku-data="(\[.*?\])"', html)
    if not match:
        return []
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        logger.warning("data-sku-data JSON 파싱 실패")
        return []


def _parse_size_options(html: str) -> dict[int, str]:
    """HTML에서 사이즈 옵션 ID → mm 사이즈 매핑 추출.

    option value="1384" 같은 태그에서 사이즈 레이블 추출.
    패턴: <option value="ID">사이즈</option> 또는
          data-option-id="ID" ... >사이즈<
    """
    # select 태그 내 option 패턴
    options: dict[int, str] = {}

    # 패턴 1: <option value="215">220</option>
    for m in re.finditer(
        r'<option[^>]+value="(\d+)"[^>]*>\s*(\d{3})\s*</option>', html
    ):
        opt_id = int(m.group(1))
        size_mm = m.group(2)
        options[opt_id] = size_mm

    # 패턴 2: data-option-id="215" ... data-label="220" 또는 유사
    if not options:
        for m in re.finditer(
            r'data-option-id="(\d+)"[^>]*data-label="(\d{3})"', html
        ):
            opt_id = int(m.group(1))
            size_mm = m.group(2)
            options[opt_id] = size_mm

    # 패턴 3: 리스트 아이템에서 ID와 사이즈 텍스트 추출
    if not options:
        for m in re.finditer(
            r'data-value="(\d+)"[^>]*>\s*(\d{3})\s*<', html
        ):
            opt_id = int(m.group(1))
            size_mm = m.group(2)
            options[opt_id] = size_mm

    return options


def _match_sku_to_size(
    sku_list: list[dict], size_options: dict[int, str]
) -> list[dict]:
    """SKU 데이터와 사이즈 옵션을 매칭하여 사이즈별 정보 반환.

    각 SKU의 selectedOptions에서 사이즈 옵션 ID를 찾아 mm 사이즈로 변환.
    """
    result: list[dict] = []
    for sku in sku_list:
        selected = sku.get("selectedOptions") or []
        size_label = ""

        # selectedOptions에서 사이즈 옵션 ID 찾기
        for opt_id in selected:
            if isinstance(opt_id, int) and opt_id in size_options:
                size_label = size_options[opt_id]
                break

        # 사이즈를 못 찾으면 UPC에서 추출 시도
        if not size_label:
            upc = sku.get("upc") or ""
            # UPC 예: VN000CRYRUS104000M → 끝에서 사이즈 추출은 어려움
            # selectedOptions 순서대로 size_options 매핑 시도
            pass

        # 가격 파싱
        sale_price = _parse_price(str(sku.get("salePrice") or ""))
        retail_price = _parse_price(str(sku.get("retailPrice") or ""))
        price = _parse_price(str(sku.get("price") or ""))

        effective_price = sale_price or price or retail_price
        original_price = retail_price or price

        # 재고 확인
        qty = sku.get("quantity") or 0
        location_qty = sku.get("locationSumQuantity") or 0
        in_stock = qty > 0 or location_qty > 0

        result.append({
            "size": size_label,
            "price": effective_price,
            "original_price": original_price,
            "in_stock": in_stock,
            "quantity": max(qty, location_qty),
            "sku_id": sku.get("skuId"),
        })

    return result


# --------------- 크롤러 클래스 ---------------


class VansCrawler:
    """반스 공식몰(vans.co.kr) 크롤러."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(
            max_concurrent=2, min_interval=1.5
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=15.0,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    async def search_products(
        self, keyword: str, limit: int = 30
    ) -> list[dict]:
        """키워드로 상품 검색."""
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        client = await self._get_client()
        async with self._rate_limiter.acquire():
            resp = await client.get(SEARCH_URL, params={"q": keyword})

        if resp.status_code != 200:
            logger.warning(
                "Vans 검색 HTTP %d (keyword=%s)",
                resp.status_code,
                keyword,
            )
            return []

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Vans 검색 JSON 파싱 실패 (keyword=%s)", keyword)
            return []

        product_list = data.get("productList") or []
        results: list[dict] = []

        for item in product_list:
            # active=false, soldOut=true 필터
            if not item.get("active", True):
                continue
            if item.get("soldOut", False):
                continue

            model = item.get("model") or ""
            name = item.get("name") or ""

            # 가격
            retail_price_obj = item.get("retailPrice") or {}
            retail_price = retail_price_obj.get("amount") or 0
            if isinstance(retail_price, str):
                retail_price = int(float(retail_price))

            sale_price_obj = item.get("salePrice")
            sale_price = 0
            if sale_price_obj and isinstance(sale_price_obj, dict):
                sale_price = sale_price_obj.get("amount") or 0
                if isinstance(sale_price, str):
                    sale_price = int(float(sale_price))

            # 이미지
            media = item.get("productMedia") or {}
            image_url = media.get("url") or ""

            results.append({
                "product_id": model,
                "name": name,
                "brand": "Vans",
                "model_number": model,
                "price": sale_price or retail_price,
                "original_price": retail_price,
                "url": f"{BASE_URL}/PRODUCT/{model}" if model else "",
                "image_url": image_url,
                "is_sold_out": False,
                "sizes": [],
            })

            if len(results) >= limit:
                break

        logger.info("Vans 검색 '%s': %d건", keyword, len(results))
        return results

    async def fetch_category_models(
        self, category: str = "SHOES", max_pages: int = 30
    ) -> list[dict]:
        """카테고리 HTML 리스팅에서 모델코드 + 이름 discovery.

        `search_products` 는 키워드 5개 seed 로 약 20건만 긁지만, Vans 공식몰
        ``/category/{cat}`` 페이지는 페이지당 ~25 모델씩 수백 페이지까지
        열려있다. 가격/사이즈 정보는 없고 모델코드와 대략적 상품명(alt) 만
        얻지만 kream_collect_queue discovery 용도로는 충분하다.

        Returns
        -------
        list[dict]
            `[{"model_number": "VN000...", "name": "...", "url": "..."}]`.
            중복 모델코드는 dedup. 품절/비활성 필터는 없음 (카테고리 HTML
            에서는 판별 불가).
        """
        client = await self._get_client()
        seen: set[str] = set()
        results: list[dict] = []

        # 상품 카드 컨텍스트에서 가장 가까운 alt 를 짝짓기 위한 근사 파서.
        # 정확한 블록 매칭보다는 "카드 경계 ≒ product-tile-main 시작점" 휴리스틱.
        _card_pat = re.compile(
            r'product-tile-main.*?href="/PRODUCT/([A-Z0-9]{5,20})".*?alt="([^"]{2,80})"',
            re.DOTALL,
        )

        for page in range(1, max_pages + 1):
            try:
                async with self._rate_limiter.acquire():
                    resp = await client.get(
                        f"{BASE_URL}/category/{category}",
                        params={"page": page},
                    )
            except Exception:
                logger.exception("Vans 카테고리 %s page=%d fetch 실패", category, page)
                break

            if resp.status_code != 200:
                logger.warning(
                    "Vans 카테고리 HTTP %d (%s page=%d)",
                    resp.status_code,
                    category,
                    page,
                )
                break

            html = resp.text
            page_new = 0
            for m in _card_pat.finditer(html):
                model = m.group(1).strip()
                name = m.group(2).strip()
                # alt 는 Handlebars 템플릿 토큰({{...}}) 인 경우 스킵
                if name.startswith("{{"):
                    name = ""
                if not model or model in seen:
                    continue
                seen.add(model)
                results.append(
                    {
                        "model_number": model,
                        "name": name,
                        "url": f"{BASE_URL}/PRODUCT/{model}",
                        "brand": "Vans",
                        "is_sold_out": False,
                    }
                )
                page_new += 1

            if page_new == 0:
                # 새 모델이 없으면 종료 (HTML 템플릿 fallback 가능성)
                break

        logger.info(
            "Vans 카테고리 %s 덤프: %d 모델 (%d페이지 스캔)",
            category,
            len(results),
            page,
        )
        return results

    async def get_product_detail(
        self, product_id: str
    ) -> RetailProduct | None:
        """상품 상세 조회 (HTML 파싱)."""
        if not product_id:
            return None

        url = f"{BASE_URL}/PRODUCT/{product_id}"
        client = await self._get_client()
        async with self._rate_limiter.acquire():
            resp = await client.get(url)

        if resp.status_code != 200:
            logger.warning(
                "Vans 상세 HTTP %d (id=%s)", resp.status_code, product_id
            )
            return None

        html = resp.text

        # data-sku-data 파싱
        sku_list = _parse_sku_data(html)
        if not sku_list:
            logger.warning("Vans 상세 SKU 데이터 없음 (id=%s)", product_id)
            return None

        # 사이즈 옵션 매핑
        size_options = _parse_size_options(html)

        # SKU-사이즈 매칭
        matched = _match_sku_to_size(sku_list, size_options)

        # 상품명 추출 (HTML title 또는 og:title)
        name = ""
        title_match = re.search(
            r'<meta\s+property="og:title"\s+content="([^"]*)"', html
        )
        if title_match:
            name = title_match.group(1)
        if not name:
            title_match = re.search(r"<title>([^<]*)</title>", html)
            if title_match:
                name = title_match.group(1).strip()

        # 이미지 추출
        image_url = ""
        img_match = re.search(
            r'<meta\s+property="og:image"\s+content="([^"]*)"', html
        )
        if img_match:
            image_url = img_match.group(1)

        # 재고 있는 사이즈만 RetailSizeInfo 변환
        sizes: list[RetailSizeInfo] = []
        for s in matched:
            if not s["in_stock"]:
                continue
            opt_price = s["price"]
            orig = s["original_price"] or opt_price
            discount_rate = 0.0
            if orig and opt_price and orig > opt_price:
                discount_rate = round(1 - opt_price / orig, 3)
            sizes.append(
                RetailSizeInfo(
                    size=s["size"],
                    price=opt_price,
                    original_price=orig,
                    in_stock=True,
                    discount_type="할인" if discount_rate > 0 else "",
                    discount_rate=discount_rate,
                )
            )

        product = RetailProduct(
            source="vans",
            product_id=product_id,
            name=name,
            model_number=product_id,
            brand="Vans",
            url=url,
            image_url=image_url,
            sizes=sizes,
            fetched_at=datetime.now(),
        )

        first_price = sizes[0].price if sizes else 0
        logger.info(
            "Vans 상품: %s | 모델: %s | %s원 | 사이즈: %d개",
            name,
            product_id,
            f"{first_price:,}" if first_price else "?",
            len(sizes),
        )
        return product

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("반스 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
vans_crawler = VansCrawler()
register("vans", vans_crawler, "반스")
