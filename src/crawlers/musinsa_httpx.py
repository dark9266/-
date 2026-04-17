"""무신사(Musinsa) httpx 기반 크롤러.

세션 쿠키는 data/musinsa_session.json에서 로드.
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

import httpx

from src.config import DATA_DIR, settings
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("musinsa_crawler")

MUSINSA_BASE = "https://www.musinsa.com"
MUSINSA_API_BASE = "https://api.musinsa.com/api2/dp/v1/plp"
GOODS_DETAIL_BASE = "https://goods-detail.musinsa.com"
SESSION_FILE = DATA_DIR / "musinsa_session.json"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

_API_HEADERS = {
    "User-Agent": _BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.musinsa.com/",
    "Origin": "https://www.musinsa.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive",
}


def _diagnose_options_data(options_data: object) -> str:
    """옵션 데이터 구조 진단 문자열."""
    if options_data is None:
        return "None"
    if not isinstance(options_data, dict):
        return f"type={type(options_data).__name__}"
    data = options_data.get("data")
    if not isinstance(data, dict):
        return f"data.type={type(data).__name__}"
    basic = data.get("basic")
    if not isinstance(basic, list):
        return f"basic.type={type(basic).__name__}"
    if not basic:
        return "basic=empty"
    first = basic[0] if isinstance(basic[0], dict) else {}
    return f"basic[0].keys={list(first.keys())[:5]}"


class MusinsaHttpxCrawler:
    """httpx 기반 무신사 크롤러."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        self._rate_limiter = AsyncRateLimiter(
            max_concurrent=10, min_interval=0.5,
        )
        self._inventory_api_warned = False

    async def connect(self) -> httpx.AsyncClient:
        """httpx 클라이언트 생성 + 세션 쿠키 로드."""
        if self._connected and self._client:
            return self._client

        cookies = httpx.Cookies()
        if SESSION_FILE.exists():
            try:
                state = json.loads(SESSION_FILE.read_text())
                for c in state.get("cookies", []):
                    cookies.set(
                        c["name"], c["value"],
                        domain=c.get("domain", ".musinsa.com"),
                        path=c.get("path", "/"),
                    )
                logger.info("무신사 세션 쿠키 로드: %d개", len(cookies))
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning("세션 쿠키 로드 실패: %s", e)

        self._client = httpx.AsyncClient(
            cookies=cookies,
            headers=_BROWSER_HEADERS,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            http2=False,
        )
        self._connected = True
        return self._client

    async def close(self) -> None:
        """클라이언트 종료."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._connected = False
        logger.info("무신사 크롤러 종료")

    async def disconnect(self) -> None:
        """close() 별칭 (기존 호환)."""
        await self.close()

    async def check_login_status(self) -> bool:
        """로그인 상태 확인."""
        client = await self.connect()
        try:
            resp = await client.get(
                f"{MUSINSA_BASE}/my-page",
                follow_redirects=False,
            )
            # 로그인 안 되면 302 → /login
            if resp.status_code in (301, 302):
                location = resp.headers.get("location", "")
                is_logged_in = "/login" not in location and "/member/login" not in location
            else:
                is_logged_in = "/login" not in str(resp.url)
            logger.info("무신사 로그인 상태: %s", "로그인됨" if is_logged_in else "로그아웃")
            return is_logged_in
        except Exception as e:
            logger.error("무신사 로그인 확인 실패: %s", e)
            return False

    # ─── 카테고리 코드 ──────────────────────────────────
    CATEGORY_CODES: dict[str, str] = {
        "신발": "103",
        "스니커즈": "103",
        "스포츠": "017",
        "바지": "003",
        "상의": "001",
        "아우터": "002",
        "가방": "004",
    }

    # ─── 검색 ────────────────────────────────────────────

    async def search_products(self, keyword: str, category: str = "") -> list[dict]:
        """무신사 키워드 검색 — api.musinsa.com API 호출.

        기존 HTML 파싱은 CSR 전환으로 깨짐. 카테고리 리스팅과 동일한
        /api2/dp/v1/plp/goods 엔드포인트를 caller=SEARCH로 호출한다.
        """
        keyword = keyword.strip()
        if len(keyword) < 2 or "/" in keyword:
            return []

        client = await self.connect()
        try:
            params = {
                "keyword": keyword,
                "caller": "SEARCH",
                "page": 1,
                "size": 30,
                "sortCode": "POPULAR",
            }
            if category:
                params["category"] = category

            async with self._rate_limiter.acquire():
                resp = await client.get(
                    f"{MUSINSA_API_BASE}/goods",
                    params=params,
                    headers=_API_HEADERS,
                )

            if resp.status_code != 200:
                logger.warning("무신사 검색 실패: status=%d, keyword=%r", resp.status_code, keyword)
                return []

            data = resp.json()
            goods_list = (data.get("data") or {}).get("list") or []

            results = []
            for item in goods_list:
                goods_no = str(item.get("goodsNo", ""))
                if not goods_no:
                    continue
                name = item.get("goodsName", "")
                brand = item.get("brand", "") or item.get("brandName", "")
                price = int(item.get("price", 0) or 0)
                coupon_price = item.get("couponPrice")
                if coupon_price and int(coupon_price) > 0:
                    price = int(coupon_price)

                # 상품명 끝 "/ MODEL-NUMBER" 패턴에서 모델번호 추출
                model_number = ""
                if " / " in name:
                    model_number = name.rsplit(" / ", 1)[-1].strip()

                results.append({
                    "product_id": goods_no,
                    "name": name,
                    "brand": brand,
                    "model_number": model_number,
                    "url": f"{MUSINSA_BASE}/products/{goods_no}",
                    "price": price,
                    "is_sold_out": bool(item.get("isSoldOut")),
                })

            logger.info("무신사 검색 '%s': %d건", keyword, len(results))
            return results

        except Exception as e:
            logger.warning("무신사 검색 실패 (%s): %s", keyword, e)
            return []

    # ─── 카테고리 리스팅 ─────────────────────────────────

    async def fetch_category_listing(
        self,
        category: str,
        max_pages: int = 1,
        *,
        brand: str | None = None,
        sort_code: str = "NEW",
    ) -> list[dict]:
        """무신사 카테고리 리스팅 — api.musinsa.com API 호출.

        1페이지(60건) 단위로 조회.
        신규 API: api.musinsa.com/api2/dp/v1/plp/goods
        파라미터: category(코드), caller=CATEGORY, page, size, sortCode, (brand)

        Parameters
        ----------
        category:
            카테고리 코드 (예: ``"103"`` 신발).
        max_pages:
            최대 페이지 수. 페이지당 60건. 빈 페이지 또는 중복만 나오면 조기 종료.
        brand:
            선택 — 브랜드 필터 (예: ``"nike"``, ``"adidas"``). ``None`` 이면
            카테고리 전량. 실측 2026-04-14 서버가 unknown brand 에 대해
            빈 리스트를 반환하므로 알려지지 않은 값에 대해 안전.
        sort_code:
            ``NEW`` (기본, 신규 업로드 순) / ``POPULAR`` / ``RECOMMEND`` /
            ``DISCOUNT_RATE`` / ``PRICE_ASC`` 등. 매칭률 관점에서는
            ``POPULAR`` 가 크림 등재 상품 비율이 높다.
        """
        client = await self.connect()
        all_items: list[dict] = []
        seen_goods: set[str] = set()

        for page_num in range(1, max_pages + 1):
            try:
                params: dict[str, str | int] = {
                    "category": category,
                    "caller": "CATEGORY",
                    "page": page_num,
                    "size": 60,
                    "sortCode": sort_code,
                }
                if brand:
                    params["brand"] = brand
                async with self._rate_limiter.acquire():
                    resp = await client.get(
                        f"{MUSINSA_API_BASE}/goods",
                        params=params,
                        headers=_API_HEADERS,
                    )

                if resp.status_code != 200:
                    logger.warning(
                        "카테고리 리스팅 실패: cat=%s brand=%s sort=%s page=%d status=%d",
                        category, brand or "-", sort_code, page_num, resp.status_code,
                    )
                    break

                data = resp.json()
                goods_list = (data.get("data") or {}).get("list") or []
                if not goods_list:
                    break

                for item in goods_list:
                    if not isinstance(item, dict):
                        continue
                    goods_no = str(item.get("goodsNo") or "")
                    if not goods_no or goods_no in seen_goods:
                        continue
                    seen_goods.add(goods_no)
                    all_items.append({
                        "goodsNo": goods_no,
                        "goodsName": item.get("goodsName") or "",
                        "brand": item.get("brand") or "",
                        "brandName": item.get("brandName") or "",
                        "price": item.get("price") or item.get("normalPrice") or 0,
                        "saleRate": item.get("saleRate") or 0,
                        "isSoldOut": bool(item.get("isSoldOut", False)),
                    })

            except Exception as e:
                logger.error("카테고리 리스팅 에러: cat=%s page=%d %s", category, page_num, e)
                break

        logger.info(
            "카테고리 리스팅 완료: category=%s brand=%s sort=%s, %d건",
            category, brand or "-", sort_code, len(all_items),
        )
        return all_items

    # ─── 상품 상세 ───────────────────────────────────────

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 — goods-detail API(가격) + options/inventory API(사이즈)."""
        client = await self.connect()

        try:
            url = f"{MUSINSA_BASE}/products/{product_id}"

            # Phase A: goods-detail API에서 이름/브랜드/가격/모델번호 조회
            api_id = product_id
            goods_data = await self._fetch_goods_detail_api(api_id)

            if goods_data:
                # 전체 품절 상품 스킵 (재고 API 불능 시 유일한 품절 게이트)
                if goods_data.get("isOutOfStock"):
                    return None

                name = goods_data.get("goodsNm") or ""
                brand_info = goods_data.get("brandInfo")
                brand = (
                    brand_info.get("brandName", "")
                    if isinstance(brand_info, dict)
                    else goods_data.get("brand", "")
                )
                model_number = goods_data.get("styleNo") or ""
                image_url = goods_data.get("thumbnailImageUrl") or ""
                goods_price = goods_data.get("goodsPrice") or {}
                sale_price = goods_price.get("salePrice") or 0
                original_price = goods_price.get("normalPrice") or sale_price
                discount_rate = round(goods_price.get("discountRate", 0) / 100, 3)
                discount_type = "할인" if discount_rate > 0 else ""
                api_id = str(goods_data.get("goodsNo") or product_id)
            else:
                # API 실패 시 HTML 폴백
                async with self._rate_limiter.acquire():
                    resp = await client.get(url)
                if resp.status_code != 200:
                    logger.warning("무신사 상품 조회 실패: pid=%s status=%d", product_id, resp.status_code)
                    return None
                html = resp.text
                if self._is_offline_or_upcoming(html):
                    return None
                name, brand = self._extract_name_brand_from_html(html)
                model_number = self._extract_model_from_html(html)
                original_price, sale_price, discount_type, discount_rate = (
                    self._extract_prices_from_html(html)
                )
                image_url = ""
                m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
                if m:
                    image_url = m.group(1)
                numeric_id = self._extract_numeric_id(html, product_id)
                api_id = numeric_id or product_id

            if not name:
                name = f"상품 {product_id}"

            # 모델번호가 무신사 SKU면 상품명에서 재추출
            if model_number and self._is_musinsa_sku(model_number):
                from src.matcher import extract_model_from_name
                name_model = extract_model_from_name(name)
                if name_model:
                    model_number = name_model

            # Phase B: options API → 사이즈 데이터
            options_data = await self._fetch_options_api(api_id)

            # Phase C: inventory API → 품절 필터 (POST + 전 optionValue NO)
            inventory_data = None
            if options_data:
                all_value_nos = self._get_all_option_value_nos(options_data)
                if all_value_nos:
                    inventory_data = await self._fetch_inventories_api(
                        api_id, all_value_nos,
                    )

            # 사이즈 파싱
            sizes: list[RetailSizeInfo] = []
            if options_data:
                sizes = self._parse_sizes_from_api(
                    options_data, inventory_data,
                    sale_price, original_price, discount_type, discount_rate,
                )

            product = RetailProduct(
                source="musinsa",
                product_id=product_id,
                name=name,
                model_number=model_number,
                brand=brand,
                url=url,
                image_url=image_url,
                sizes=sizes,
                fetched_at=datetime.now(),
            )

            logger.info(
                "무신사 상품: %s | 모델번호: %s | 가격: %s원 | 사이즈: %d개",
                name[:40], model_number,
                f"{sale_price:,}" if sale_price else "?",
                len(sizes),
            )
            return product

        except Exception as e:
            logger.error("무신사 상품 조회 실패 (%s): %s", product_id, e)
            return None

    async def _fetch_goods_detail_api(self, product_id: str) -> dict | None:
        """goods-detail API에서 상품 기본 정보 조회 (가격 포함)."""
        client = await self.connect()
        api_url = f"{GOODS_DETAIL_BASE}/api2/goods/{product_id}"
        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(api_url, headers=_API_HEADERS)
            if resp.status_code != 200:
                logger.debug("goods-detail API 실패: pid=%s status=%d", product_id, resp.status_code)
                return None
            body = resp.json()
            data = body.get("data") if isinstance(body, dict) else body
            if isinstance(data, dict) and data.get("goodsNo"):
                return data
            return None
        except Exception as e:
            logger.debug("goods-detail API 예외: pid=%s %s", product_id, e)
            return None

    # ─── API 호출 ────────────────────────────────────────

    async def _fetch_options_api(self, product_id: str) -> dict | None:
        """상품 옵션 API 직접 호출."""
        client = await self.connect()
        api_url = f"{GOODS_DETAIL_BASE}/api2/goods/{product_id}/options"
        try:
            async with self._rate_limiter.acquire():
                resp = await client.get(api_url, headers=_API_HEADERS)
            if resp.status_code != 200:
                logger.debug("옵션 API 실패: pid=%s status=%d", product_id, resp.status_code)
                return None
            result = resp.json()
            if not isinstance(result, dict):
                return None
            data = result.get("data")
            if isinstance(data, dict) and isinstance(data.get("basic"), list):
                logger.debug("옵션 API 성공: pid=%s basic=%d개", product_id, len(data["basic"]))
                return result
            return None
        except Exception as e:
            logger.debug("옵션 API 예외: pid=%s %s", product_id, e)
            return None

    async def _fetch_inventories_api(
        self,
        product_id: str,
        option_value_nos: list[int] | None = None,
    ) -> list | None:
        """재고 API 호출 (POST).

        2026-04-17: GET `selectedOptionValueNos=...` 는 전 상품 400 반환 — 실제
        브라우저는 **POST** `options/v2/prioritized-inventories` + JSON body
        `{"optionValueNos":[...]}` 로 호출 (Playwright 캡처 확인). 기존 GET 경로는
        상시 400 → `inventory_data=None` → `_parse_sizes_from_api` 폴백이
        `isDeleted=False` 만 확인하고 전 사이즈 in_stock=True 로 처리.
        결과: 전체 품절 상품에도 허위 알림 (삼바 JR2660 pid=4375143 실측).
        """
        if not option_value_nos:
            return None

        client = await self.connect()
        api_url = f"{GOODS_DETAIL_BASE}/api2/goods/{product_id}/options/v2/prioritized-inventories"
        headers = dict(_API_HEADERS)
        headers["Content-Type"] = "application/json"

        try:
            async with self._rate_limiter.acquire():
                resp = await client.post(
                    api_url,
                    json={"optionValueNos": list(option_value_nos)},
                    headers=headers,
                )
            if resp.status_code != 200:
                logger.debug(
                    "재고 API 실패: pid=%s status=%d body=%s",
                    product_id, resp.status_code, resp.text[:200],
                )
                return None
            body = resp.json()
            if isinstance(body, dict):
                inv = body.get("data", [])
                if isinstance(inv, list):
                    logger.debug("재고 API 성공: pid=%s %d건", product_id, len(inv))
                    return inv
            elif isinstance(body, list):
                return body
        except Exception as e:
            logger.debug("재고 API 예외: pid=%s %s", product_id, e)

        if not self._inventory_api_warned:
            logger.warning("재고 API 불능 — optionItems.activated 폴백 (품절 감지 불가)")
            self._inventory_api_warned = True
        return None

    # ─── HTML 파싱 헬퍼 ──────────────────────────────────

    def _extract_name_brand_from_html(self, html: str) -> tuple[str, str]:
        """HTML에서 상품명 + 브랜드 추출."""
        name = ""
        brand = ""

        # og:title 메타 태그
        m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if m:
            name = m.group(1).strip()

        # 브랜드: BrandName 컴포넌트 또는 메타
        m = re.search(
            r'class="[^"]*BrandName[^"]*"[^>]*>([^<]+)<', html,
        )
        if m:
            brand = m.group(1).strip()
        if not brand:
            m = re.search(r'"brand"\s*:\s*"([^"]+)"', html)
            if m:
                brand = m.group(1).strip()

        # name에 브랜드 포함 시 분리
        if not name:
            m = re.search(
                r'class="[^"]*GoodsName[^"]*"[^>]*>([^<]+)<', html,
            )
            if m:
                name = m.group(1).strip()

        return name, brand

    def _extract_model_from_html(self, html: str) -> str:
        """HTML에서 모델번호 추출 (5단계 폴백)."""
        # 1. 품번/모델 테이블
        table_model = ""
        m = re.search(r'품번[:\s</>\w]*?([A-Za-z0-9][-A-Za-z0-9/\s]+)', html)
        if m:
            candidate = m.group(1).strip()
            if candidate and candidate != "-":
                table_model = candidate
        if not table_model:
            m = re.search(r'모델[:\s</>\w]*?([A-Za-z0-9][-A-Za-z0-9/\s]+)', html)
            if m:
                candidate = m.group(1).strip()
                if candidate and candidate != "-":
                    table_model = candidate

        # 2. data-model-number 속성
        if not table_model:
            m = re.search(r'data-model-number="([^"]+)"', html)
            if m:
                table_model = m.group(1).strip()
            else:
                m = re.search(r'data-product-code="([^"]+)"', html)
                if m:
                    table_model = m.group(1).strip()

        # 3. 상품명에서 모델번호 패턴 추출
        name_model = ""
        name_text = ""
        m = re.search(r'class="[^"]*GoodsName[^"]*"[^>]*>([^<]+)<', html)
        if m:
            name_text = m.group(1).strip()
        if not name_text:
            m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
            if m:
                name_text = m.group(1).strip()

        if name_text:
            # "/" 뒤의 모델번호
            m = re.search(r"/\s*([A-Za-z0-9][-A-Za-z0-9]+)", name_text)
            if m:
                candidate = m.group(1).strip().upper()
                if re.match(r"[A-Z]{1,3}\d{3,5}[-\s]?\d{2,4}", candidate):
                    name_model = candidate
            # Nike/Jordan: XX1234-123
            if not name_model:
                m = re.search(r"\b([A-Z]{1,3}\d{3,5}-\d{2,4})\b", name_text.upper())
                if m:
                    name_model = m.group(1)
            # NB/기타: U7408PL, MT410GC5
            if not name_model:
                m = re.search(
                    r"\b([A-Z]{1,2}\d{3,5}[A-Z]{0,3}\d{0,2})\b", name_text.upper(),
                )
                if m and len(m.group(1)) >= 5:
                    name_model = m.group(1)

        # table_model이 무신사 SKU면 name_model 우선
        if table_model and not self._is_musinsa_sku(table_model):
            return table_model
        if name_model:
            return name_model
        if table_model:
            return table_model

        # 4. HTML 전체에서 정규식 추출
        patterns = [
            r"[A-Z]{1,3}\d{3,5}[-\s]?\d{2,4}",
            r"\d{6}[-\s]?\d{3}",
            r"[A-Z]{1,2}\d{3,5}[A-Z]{0,3}",
        ]
        for pattern in patterns:
            m = re.search(pattern, html)
            if m:
                return m.group(0).strip()

        # 5. "품번" 근처 텍스트
        m = re.search(r"품번[:\s]*([A-Za-z0-9\-]+)", html)
        if m:
            return m.group(1).strip()

        return ""

    def _extract_prices_from_html(self, html: str) -> tuple[int, int, str, float]:
        """HTML에서 가격 정보 추출."""
        original_price = 0
        sale_price = 0
        discount_type = ""
        discount_rate = 0.0

        # JSON-LD에서 가격 추출 시도
        m = re.search(r'"price"\s*:\s*"?(\d+)"?', html)
        if m:
            sale_price = int(m.group(1))

        # PriceWrap 컴포넌트 영역에서 가격 추출
        price_section = ""
        m = re.search(
            r'class="[^"]*(?:PriceWrap|PriceTotalWrap)[^"]*"[^>]*>(.*?)</div>',
            html, re.DOTALL,
        )
        if m:
            price_section = m.group(1)
        if not price_section:
            # og:description에서 가격 추출
            m = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', html)
            if m:
                price_section = m.group(1)

        if price_section:
            prices = re.findall(r"(\d[\d,]+)원", price_section)
            if prices:
                parsed = [int(p.replace(",", "")) for p in prices]
                if len(parsed) >= 2:
                    original_price = parsed[0]
                    sale_price = parsed[-1]
                elif len(parsed) == 1:
                    if not sale_price:
                        sale_price = parsed[0]
                    original_price = sale_price

        # 할인율
        if original_price and sale_price and original_price > sale_price:
            discount_rate = round(1 - sale_price / original_price, 3)
            discount_type = "할인"

        if not sale_price and original_price:
            sale_price = original_price

        return original_price, sale_price, discount_type, discount_rate

    def _is_offline_or_upcoming(self, html: str) -> bool:
        """오프라인전용 / 발매예정 체크."""
        if "판매예정" in html or "출시예정" in html:
            logger.info("발매예정 스킵")
            return True
        if "오프라인 전용 상품" in html:
            # 구매버튼 없으면 오프라인
            if not re.search(r'구매하기|바로구매|장바구니', html):
                logger.info("오프라인전용 스킵")
                return True
        return False

    def _extract_numeric_id(self, html: str, fallback: str) -> str | None:
        """HTML에서 숫자 상품 ID(goodsNo) 추출."""
        m = re.search(r'"goodsNo"\s*:\s*(\d+)', html)
        if m:
            return m.group(1)
        m = re.search(r'/goods/(\d+)/options', html)
        if m:
            return m.group(1)
        # fallback이 이미 숫자면 그대로
        if fallback.isdigit():
            return fallback
        return None

    # ─── 사이즈 파싱 (기존 로직 그대로) ──────────────────

    def _parse_sizes_from_api(
        self,
        options_data: dict | None,
        inventory_data: list | None,
        sale_price: int,
        original_price: int,
        discount_type: str,
        discount_rate: float,
    ) -> list[RetailSizeInfo]:
        """API 응답에서 사이즈/재고 정보 파싱."""
        if not options_data:
            return []

        if isinstance(options_data, list):
            if not options_data:
                return []
            options_data = options_data[0] if isinstance(options_data[0], dict) else {}

        data = options_data.get("data") if isinstance(options_data, dict) else None
        if not isinstance(data, dict):
            return []
        basic_options = data.get("basic", [])
        if not isinstance(basic_options, list):
            return []

        # 사이즈 옵션 찾기
        _COLOR_NAMES = {"c", "color", "색상", "컬러"}
        _SIZE_NAMES = {"s", "size", "사이즈", "신발", "shoes"}
        _SKIP_NAMES = {"none", ""}

        size_option = None
        for opt in basic_options:
            if not isinstance(opt, dict):
                continue
            name_lower = (opt.get("name") or "").strip().lower()
            std_no = opt.get("standardOptionNo")
            if std_no in (3, 4, 6):
                size_option = opt
                break
            if name_lower in _SIZE_NAMES:
                size_option = opt
                break

        if not size_option and len(basic_options) == 1:
            name_lower = (basic_options[0].get("name") or "").strip().lower()
            if name_lower not in _COLOR_NAMES and name_lower not in _SKIP_NAMES:
                size_option = basic_options[0]

        if not size_option:
            return []

        option_values = size_option.get("optionValues", [])
        if not isinstance(option_values, list):
            return []

        # optionItems 비활성 매핑
        option_items = data.get("optionItems", [])
        if not isinstance(option_items, list):
            option_items = []
        deactivated: set[int] = set()
        for item in option_items:
            if not isinstance(item, dict):
                continue
            if not item.get("activated", True):
                for val_no in item.get("optionValueNos", []):
                    deactivated.add(val_no)

        variant_to_values: dict[int, list[int]] = {}
        for item in option_items:
            if isinstance(item, dict):
                variant_to_values[item["no"]] = item.get("optionValueNos", [])

        # 다중 옵션 처리
        is_multi_option = len(basic_options) > 1
        valid_size_nos: set[int] = set()
        if is_multi_option and option_items:
            size_opt_no = size_option.get("no")
            for item in option_items:
                if not isinstance(item, dict):
                    continue
                item_opt_values = item.get("optionValues", [])
                for iov in item_opt_values:
                    if isinstance(iov, dict) and iov.get("optionNo") == size_opt_no:
                        valid_size_nos.add(iov["no"])
                        break
                else:
                    for vno in item.get("optionValueNos", []):
                        valid_size_nos.add(vno)

        # inventory 품절 매핑
        inventory_stock: dict[int, bool] = {}
        if inventory_data and isinstance(inventory_data, list):
            for inv in inventory_data:
                if not isinstance(inv, dict):
                    continue
                out_of_stock = inv.get("outOfStock", False)
                quantity = inv.get("quantity", -1)
                in_stock = not out_of_stock and quantity != 0
                related = inv.get("relatedOption")
                if isinstance(related, dict):
                    val_no = related.get("optionValueNo")
                    if val_no is not None:
                        inventory_stock[val_no] = in_stock
                        continue
                variant_id = inv.get("productVariantId")
                if variant_id and variant_id in variant_to_values:
                    for val_no in variant_to_values[variant_id]:
                        inventory_stock[val_no] = in_stock

        sizes: list[RetailSizeInfo] = []
        for ov in option_values:
            if not isinstance(ov, dict):
                continue
            size_name = ov.get("name", "")
            if not size_name:
                continue

            ov_no = ov.get("no")

            if valid_size_nos and ov_no not in valid_size_nos:
                in_stock = False
            elif ov_no in deactivated:
                in_stock = False
            elif inventory_stock:
                in_stock = inventory_stock.get(ov_no, False)
            elif inventory_data is not None:
                in_stock = False
            else:
                # 2026-04-17: 재고 API 실패 시 "isDeleted=False → 재고 있음" 폴백은
                # 전체 품절 상품까지 in_stock=True 로 처리 → 허위 알림 (JR2660 실측).
                # 거짓 알림 0 지향 원칙 (CLAUDE.md 축 ①) 에 따라 "미확인 → 품절"
                # 보수적으로 처리한다. isDeleted=True 는 명시적 삭제이므로 유지.
                in_stock = False

            if ov.get("isRestock") or "재입고" in size_name:
                in_stock = False

            if not in_stock:
                continue

            sizes.append(RetailSizeInfo(
                size=size_name,
                price=sale_price,
                original_price=original_price,
                in_stock=True,
                discount_type=discount_type,
                discount_rate=discount_rate,
            ))

        return sizes

    def _get_all_option_value_nos(self, options_data: dict | None) -> list[int]:
        """옵션 API 응답에서 **모든** optionValue NO 수집 (POST 재고 API 용)."""
        if not options_data or not isinstance(options_data, dict):
            return []
        data = options_data.get("data")
        if not isinstance(data, dict):
            return []
        basics = data.get("basic", [])
        if not isinstance(basics, list):
            return []
        nos: list[int] = []
        for opt in basics:
            if not isinstance(opt, dict):
                continue
            for v in opt.get("optionValues", []):
                if isinstance(v, dict) and isinstance(v.get("no"), int):
                    nos.append(v["no"])
        return nos

    def _get_color_option_values(self, options_data: dict | None) -> list[int]:
        """다중 옵션 상품에서 컬러 옵션 값 추출."""
        if not options_data or not isinstance(options_data, dict):
            return []
        data = options_data.get("data")
        if not isinstance(data, dict):
            return []
        basics = data.get("basic", [])
        if not isinstance(basics, list) or len(basics) < 2:
            return []

        _COLOR_STD_NOS = {1, 2}
        _COLOR_NAMES = {"c", "color", "색상", "컬러"}
        for opt in basics:
            if not isinstance(opt, dict):
                continue
            name_lower = (opt.get("name") or "").strip().lower()
            std_no = opt.get("standardOptionNo")
            if std_no in _COLOR_STD_NOS or name_lower in _COLOR_NAMES:
                values = opt.get("optionValues", [])
                return [v["no"] for v in values if isinstance(v, dict) and "no" in v]
        return []

    @staticmethod
    def _is_musinsa_sku(model: str) -> bool:
        """무신사 자체 SKU인지 판별."""
        if not model:
            return False
        if "_" in model:
            return True
        upper = model.upper()
        if re.match(r"^[A-Z]{1,3}\d{3,5}[-]\d{2,4}$", upper):
            return False
        if re.match(r"^[A-Z]{1,2}\d{3,5}[A-Z]{0,3}$", upper):
            return False
        if len(model) >= 8 and "-" not in model and re.match(r"^[A-Z]{2,}", upper):
            return True
        return False

    @staticmethod
    def _parse_price(text: str) -> int | None:
        if not text:
            return None
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else None


# 싱글톤 (import 호환: from src.crawlers.musinsa_httpx import musinsa_crawler)
musinsa_crawler = MusinsaHttpxCrawler()

from src.crawlers.registry import register  # noqa: E402
register("musinsa", musinsa_crawler, "무신사")
