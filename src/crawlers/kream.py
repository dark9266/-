"""크림(KREAM) __NUXT_DATA__ 기반 크롤러.

KREAM이 Next.js에서 Nuxt 3로 전환됨에 따라,
페이지의 __NUXT_DATA__ (devalue 직렬화 형식)를 파싱하여 데이터를 수집한다.

전략:
1차: 상품 페이지 HTML → __NUXT_DATA__ 파싱 (주력)
2차: 크림 내부 API 호출 (일부 엔드포인트 유효)
3차: HTML 메타 태그 (최후의 수단)
"""

import asyncio
import json
import random
import re
from datetime import datetime

from curl_cffi.requests import AsyncSession

from src.config import settings
from src.models.product import KreamProduct, KreamSizePrice
from src.utils.logging import setup_logger

logger = setup_logger("kream_crawler")

KREAM_BASE = "https://kream.co.kr"
KREAM_API_BASE = "https://api.kream.co.kr"

# ─── 브라우저 위장 헤더 ─────────────────────────────────

_SAFARI_USER_AGENTS = [
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Mobile/15E148 Safari/604.1"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
]

_COMMON_HEADERS = {
    "User-Agent": _SAFARI_USER_AGENTS[0],
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://kream.co.kr/",
    "Connection": "keep-alive",
}

_API_HEADERS = {
    **_COMMON_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://kream.co.kr",
}

_PAGE_HEADERS = {
    **_COMMON_HEADERS,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Upgrade-Insecure-Requests": "1",
}


async def _random_delay() -> None:
    """차단 방지용 랜덤 딜레이."""
    delay = random.uniform(settings.request_delay_min, settings.request_delay_max)
    logger.debug("딜레이: %.1f초", delay)
    await asyncio.sleep(delay)


# ─── devalue unflatten (Nuxt 3 __NUXT_DATA__ 역직렬화) ───

def _unflatten_nuxt(parsed: list):
    """Nuxt 3의 devalue 직렬화 형식을 Python 객체로 복원.

    __NUXT_DATA__의 JSON 배열은 devalue 라이브러리 형식:
    - 배열 인덱스 0이 루트
    - 각 요소가 원시값이면 그대로 사용
    - dict이면 {key: 인덱스참조} 형태로, 값이 다른 인덱스를 참조
    - list이면 [인덱스참조, ...] 형태
    - 특수 태그: ["Reactive", idx], ["ShallowRef", idx] 등은 래퍼
    """
    if not parsed or not isinstance(parsed, list):
        return None

    n = len(parsed)
    hydrated = [None] * n
    filled = [False] * n

    def hydrate(index):
        # 음수 인덱스: 특수값
        if isinstance(index, int) and index < 0:
            specials = {-1: None, -2: float("nan"), -3: float("inf"), -4: float("-inf")}
            return specials.get(index)

        if not isinstance(index, int) or index >= n:
            return None

        if filled[index]:
            return hydrated[index]

        filled[index] = True
        value = parsed[index]

        if value is None or isinstance(value, bool):
            hydrated[index] = value
        elif isinstance(value, str):
            hydrated[index] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            hydrated[index] = value
        elif isinstance(value, list):
            if len(value) >= 2 and isinstance(value[0], str):
                tag = value[0]
                if tag in ("Reactive", "ShallowReactive", "ShallowRef", "Ref"):
                    hydrated[index] = hydrate(value[1])
                elif tag == "Date":
                    hydrated[index] = value[1] if len(value) > 1 else None
                elif tag == "Set":
                    hydrated[index] = [hydrate(v) for v in value[1:]]
                elif tag == "Map":
                    m = {}
                    hydrated[index] = m
                    for i in range(1, len(value) - 1, 2):
                        k = hydrate(value[i])
                        v = hydrate(value[i + 1])
                        if k is not None:
                            m[str(k)] = v
                    return m
                else:
                    # 일반 배열 (첫 요소가 문자열이지만 태그가 아님)
                    arr = []
                    hydrated[index] = arr
                    for v in value:
                        arr.append(hydrate(v))
            else:
                arr = []
                hydrated[index] = arr
                for v in value:
                    arr.append(hydrate(v))
        elif isinstance(value, dict):
            obj = {}
            hydrated[index] = obj
            for key, ref in value.items():
                obj[key] = hydrate(ref)
        else:
            hydrated[index] = value

        return hydrated[index]

    return hydrate(0)


class KreamCrawler:
    """크림 __NUXT_DATA__ 기반 크롤러.

    공개 인터페이스:
    - search_product(): 키워드 검색
    - get_product_detail(): 상품 상세 정보
    - get_sell_prices() / get_buy_prices(): 사이즈별 시세
    - get_trade_history(): 체결 내역
    - get_full_product_info(): 전체 정보 한번에 수집
    - ensure_login(): (비활성 — pinia 전용 모드)
    """

    def __init__(self):
        self._session: AsyncSession | None = None
        self._initialized: bool = False
        self._logged_in: bool = False
        self._api_version: str = "56"
        self._build_version: str = ""
        self._device_id: str = ""

    @property
    def is_active(self) -> bool:
        return self._session is not None

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    # ─── 세션 관리 ──────────────────────────────────────

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                impersonate="safari17_0",
                timeout=30,
            )
            self._initialized = False
            self._logged_in = False

        if not self._initialized:
            await self._init_cookies()

        return self._session

    async def _init_cookies(self) -> None:
        """크림 메인 페이지 방문으로 세션 쿠키 + API 설정값 확보."""
        try:
            resp = await self._session.get(
                KREAM_BASE, headers=_PAGE_HEADERS, allow_redirects=True
            )
            # 쿠키에서 device ID 추출
            self._device_id = str(self._session.cookies.get("webDid", ""))
            # HTML에서 API 설정값 추출
            if resp.status_code == 200:
                html = resp.text
                m = re.search(r'apiVersion["\']?\s*[:=]\s*["\']([^"\']+)', html)
                if m:
                    self._api_version = m.group(1)
                m = re.search(r'buildVersion["\']?\s*[:=]\s*["\']([^"\']+)', html)
                if m:
                    self._build_version = m.group(1)
            logger.info(
                "초기 쿠키 확보: status=%d, api_ver=%s, build=%s",
                resp.status_code, self._api_version, self._build_version,
            )
            self._initialized = True
        except Exception as e:
            logger.warning("초기 쿠키 확보 실패 (계속 진행): %s", e)
            self._initialized = True

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._initialized = False
            self._logged_in = False
            logger.info("크림 크롤러 세션 종료")

    # ─── 로그인 (pinia 전용 모드) ────────────────────────

    async def ensure_login(self) -> bool:
        """Pinia 전용 모드 — 로그인 비활성.

        Returns: False (인증 불필요, pinia 데이터로 충분).
        """
        logger.debug("pinia 전용 모드 — 로그인 스킵")
        return False

    def _build_api_auth_headers(self) -> dict:
        """api.kream.co.kr 호출에 필요한 인증 헤더 생성."""
        from datetime import datetime as dt

        device_id = f"web;{self._device_id}" if self._device_id else "web;unknown"
        client_dt = dt.now().strftime("%Y%m%d%H%M%S") + "+0900"
        auth = {
            "X-KREAM-DEVICE-ID": device_id,
            "X-KREAM-CLIENT-DATETIME": client_dt,
            "X-KREAM-API-VERSION": self._api_version,
            "X-KREAM-WEB-REQUEST-SECRET": "kream-djscjsghdkd",
        }
        if self._build_version:
            auth["X-KREAM-WEB-BUILD-VERSION"] = self._build_version
        return auth

    # ─── HTTP 요청 (재시도 + 지수 백오프) ─────────────────

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        max_retries: int = 3,
        parse_json: bool = True,
    ) -> dict | str | None:
        session = await self._get_session()

        # API 호출은 api.kream.co.kr 도메인으로 라우팅 + 인증 헤더 추가
        is_api_call = False
        if url.startswith("/api/"):
            url = f"{KREAM_API_BASE}{url}"
            is_api_call = True
        elif url.startswith("/"):
            url = f"{KREAM_BASE}{url}"

        req_headers = dict(_API_HEADERS if parse_json else _PAGE_HEADERS)
        req_headers["User-Agent"] = random.choice(_SAFARI_USER_AGENTS)
        if is_api_call:
            req_headers.update(self._build_api_auth_headers())
        if headers:
            req_headers.update(headers)

        for attempt in range(max_retries):
            try:
                resp = await session.request(
                    method, url, headers=req_headers, params=params,
                    allow_redirects=True,
                )
                status = resp.status_code

                if 200 <= status < 300:
                    if parse_json:
                        return resp.json()
                    return resp.text

                if status == 429:
                    wait = (2 ** attempt) * 10
                    logger.warning("요청 제한(429) — %d초 대기 (%d/%d)", wait, attempt + 1, max_retries)
                    await asyncio.sleep(wait)
                    continue

                if status >= 500:
                    wait = (2 ** attempt) * 3
                    logger.warning("서버 에러 %d — %d초 후 재시도 (%d/%d)", status, wait, attempt + 1, max_retries)
                    await asyncio.sleep(wait)
                    continue

                if status == 403:
                    logger.error("접근 차단(403): %s", url)
                    if attempt == 0:
                        self._initialized = False
                        await self._init_cookies()
                        continue
                    return None

                logger.error("HTTP %d: %s", status, url)
                return None

            except asyncio.TimeoutError:
                wait = (2 ** attempt) * 2
                logger.warning("타임아웃 (%s) — %d초 후 재시도", url, wait)
                await asyncio.sleep(wait)
            except Exception as e:
                wait = (2 ** attempt) * 2
                logger.warning("연결 에러 (%s): %s — %d초 후 재시도", url, e, wait)
                await asyncio.sleep(wait)

        logger.error("최대 재시도(%d회) 초과: %s", max_retries, url)
        return None

    # ─── __NUXT_DATA__ 파싱 ───────────────────────────────

    @staticmethod
    def _extract_nuxt_data(html: str) -> dict | list | None:
        """HTML에서 __NUXT_DATA__ 스크립트를 추출하고 devalue 역직렬화.

        Nuxt 3는 __NUXT_DATA__에 devalue 형식으로 페이지 데이터를 삽입한다.
        여러 청크로 나뉠 수 있으므로 모든 __NUXT_DATA__ 스크립트를 합친다.
        """
        # 단일 또는 다중 __NUXT_DATA__ 스크립트 태그 검색
        pattern = r'<script[^>]*\bid=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>'
        matches = re.findall(pattern, html, re.DOTALL)

        if not matches:
            # data-ssr 변형도 시도
            pattern2 = r'<script[^>]*data-ssr[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>'
            matches = re.findall(pattern2, html, re.DOTALL)

        if not matches:
            return None

        # 첫 번째 매치 사용 (보통 하나)
        raw = matches[0].strip()
        if not raw:
            return None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("__NUXT_DATA__ JSON 파싱 실패: %s", e)
            return None

        if not isinstance(parsed, list):
            return parsed if isinstance(parsed, dict) else None

        # devalue unflatten
        try:
            result = _unflatten_nuxt(parsed)
            if result is not None:
                logger.debug("__NUXT_DATA__ 역직렬화 성공 (원소 %d개)", len(parsed))
            return result
        except Exception as e:
            logger.warning("__NUXT_DATA__ unflatten 실패: %s", e)
            return None

    @staticmethod
    def _extract_next_data(html: str) -> dict | None:
        """레거시: __NEXT_DATA__ 파싱 (하위 호환용)."""
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*({.+?})\s*</script>',
            html, re.DOTALL,
        )
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _deep_find(data, key: str, max_depth: int = 10):
        """중첩 구조에서 특정 키의 값을 재귀 탐색.

        여러 결과가 있을 수 있으므로 리스트로 반환.
        """
        results = []
        if max_depth <= 0:
            return results

        if isinstance(data, dict):
            if key in data:
                results.append(data[key])
            for v in data.values():
                results.extend(KreamCrawler._deep_find(v, key, max_depth - 1))
        elif isinstance(data, list):
            for item in data:
                results.extend(KreamCrawler._deep_find(item, key, max_depth - 1))

        return results

    @staticmethod
    def _deep_find_dict(data, required_keys: set, max_depth: int = 10) -> list[dict]:
        """required_keys를 모두 포함하는 dict를 재귀 탐색."""
        results = []
        if max_depth <= 0:
            return results

        if isinstance(data, dict):
            if required_keys.issubset(data.keys()):
                results.append(data)
            for v in data.values():
                results.extend(KreamCrawler._deep_find_dict(v, required_keys, max_depth - 1))
        elif isinstance(data, list):
            for item in data:
                results.extend(KreamCrawler._deep_find_dict(item, required_keys, max_depth - 1))

        return results

    def _extract_page_data(self, html: str) -> dict | list | None:
        """HTML에서 페이지 데이터 추출. __NUXT_DATA__ 우선, __NEXT_DATA__ 폴백."""
        # 1차: __NUXT_DATA__
        data = self._extract_nuxt_data(html)
        if data is not None:
            return data

        # 2차: __NEXT_DATA__ (레거시 호환)
        next_data = self._extract_next_data(html)
        if next_data:
            return next_data.get("props", {}).get("pageProps", {})

        return None

    # ─── 검색 ───────────────────────────────────────────

    async def search_product(self, keyword: str) -> list[dict]:
        """크림에서 키워드로 상품 검색.

        1차: 검색 페이지 __NUXT_DATA__ 파싱
        2차: 내부 검색 API 호출
        3차: HTML에서 /products/{id} 링크 추출
        """
        await _random_delay()

        # 1차: 검색 페이지 HTML → __NUXT_DATA__
        results = await self._search_via_nuxt(keyword)
        if results:
            return results

        # 2차: HTML 링크 추출
        logger.info("검색 폴백(HTML 링크): '%s'", keyword)
        return await self._search_via_html_links(keyword)

    async def _search_via_nuxt(self, keyword: str) -> list[dict]:
        """검색 페이지의 __NUXT_DATA__에서 상품 목록 추출."""
        url = f"{KREAM_BASE}/search?keyword={keyword}&tab=products"
        html = await self._request("GET", url, parse_json=False)
        if not html:
            return []

        data = self._extract_page_data(html)
        if not data:
            return []

        results = []

        # Nuxt 데이터에서 상품 목록 탐색
        # 가능한 구조: data.products, data.items, 또는 깊은 중첩
        product_lists = self._deep_find(data, "products")
        if not product_lists:
            product_lists = self._deep_find(data, "items")
        if not product_lists:
            product_lists = self._deep_find(data, "searchResult")

        for pl in product_lists:
            if isinstance(pl, list):
                for item in pl[:20]:
                    parsed = self._extract_product_summary(item)
                    if parsed:
                        results.append(parsed)
                if results:
                    break
            elif isinstance(pl, dict):
                # searchResult 같은 래퍼 dict
                inner = pl.get("items") or pl.get("products") or pl.get("data")
                if isinstance(inner, list):
                    for item in inner[:20]:
                        parsed = self._extract_product_summary(item)
                        if parsed:
                            results.append(parsed)
                    if results:
                        break

        # 상품 목록 키를 못 찾은 경우: id+name을 가진 dict 직접 탐색
        if not results:
            candidates = self._deep_find_dict(data, {"id", "name"}, max_depth=8)
            for c in candidates[:30]:
                parsed = self._extract_product_summary(c)
                if parsed:
                    results.append(parsed)

        if results:
            logger.info("크림 검색 '%s': %d건 (NUXT_DATA)", keyword, len(results))
        return results[:20]

    async def _search_via_html_links(self, keyword: str) -> list[dict]:
        """HTML에서 product_card + img alt로 상품 목록 추출 (최후의 수단)."""
        url = f"{KREAM_BASE}/search?keyword={keyword}&tab=products"
        html = await self._request("GET", url, parse_json=False)
        if not html:
            return []

        # data-sdui-id="product_card/{id}" 블록 → img alt에서 상품명 추출
        results = []
        seen = set()
        cards = list(re.finditer(r'data-sdui-id="product_card/(\d+)"', html))
        for i, match in enumerate(cards):
            pid = match.group(1)
            if pid in seen:
                continue
            seen.add(pid)

            # 다음 카드까지의 블록 (마지막이면 3000자)
            end = cards[i + 1].start() if i + 1 < len(cards) else match.end() + 3000
            block = html[match.end():min(end, len(html))]

            # img alt="상품명(English Name)"
            name = ""
            alt_m = re.search(r'alt="([^"]+)"', block)
            if alt_m:
                name = alt_m.group(1)

            # 이미지 URL
            image_url = ""
            src_m = re.search(r'srcset="([^"?\s]+)', block)
            if src_m:
                image_url = src_m.group(1)

            results.append({
                "product_id": pid,
                "name": name,
                "brand": "",
                "image_url": image_url,
                "url": f"{KREAM_BASE}/products/{pid}",
            })

        # product_card 패턴 실패 시 기본 링크 추출 폴백
        if not results:
            for match in re.finditer(r'/products/(\d+)', html):
                pid = match.group(1)
                if pid not in seen:
                    seen.add(pid)
                    results.append({
                        "product_id": pid,
                        "name": "",
                        "brand": "",
                        "url": f"{KREAM_BASE}/products/{pid}",
                    })

        logger.info("크림 검색 '%s': %d건 (HTML 링크)", keyword, len(results[:20]))
        return results[:20]

    def _parse_search_response(self, data: dict, keyword: str) -> list[dict]:
        """검색 API JSON 응답에서 상품 목록 추출."""
        results = []
        items = (
            data.get("items")
            or data.get("products")
            or (data.get("data", {}).get("items") if isinstance(data.get("data"), dict) else None)
            or (data.get("data", {}).get("products") if isinstance(data.get("data"), dict) else None)
            or (data.get("data") if isinstance(data.get("data"), list) else None)
            or []
        )
        for item in items[:20]:
            parsed = self._extract_product_summary(item)
            if parsed:
                results.append(parsed)
        logger.info("크림 검색 '%s': %d건 (API)", keyword, len(results))
        return results

    def _extract_product_summary(self, item) -> dict | None:
        """개별 상품 아이템에서 요약 정보 추출."""
        if not isinstance(item, dict):
            return None

        product_id = str(
            item.get("id")
            or item.get("product_id")
            or item.get("productId")
            or ""
        )
        if not product_id or product_id == "None":
            return None

        name = (
            item.get("name")
            or item.get("translated_name")
            or item.get("title")
            or ""
        )
        brand_raw = item.get("brand")
        if isinstance(brand_raw, dict):
            brand = brand_raw.get("name", "")
        else:
            brand = (
                item.get("brand_name")
                or item.get("brandName")
                or str(brand_raw or "")
            )

        return {
            "product_id": product_id,
            "name": str(name).strip(),
            "brand": str(brand).strip(),
            "url": f"{KREAM_BASE}/products/{product_id}",
        }

    # ─── 상품 상세 ──────────────────────────────────────

    async def get_product_detail(self, product_id: str) -> KreamProduct | None:
        """상품 상세 정보 수집.

        1차: 상품 페이지 __NUXT_DATA__ 파싱
        2차: HTML 메타 태그 (최후의 수단)
        3차: options/display API로 시세 보충
        """
        await _random_delay()

        # 1차: HTML → __NUXT_DATA__
        product = await self._detail_via_nuxt(product_id)
        if not product:
            # 2차: 메타 태그
            logger.info("상세 폴백(메타): %s", product_id)
            product = await self._detail_via_meta(product_id)

        if not product:
            return None

        # 3차: 시세 미확보 시 options/display API 보충
        if not product.size_prices:
            api_prices = await self._fetch_options_display(product_id)
            if api_prices:
                product.size_prices = api_prices
                logger.info("상세 시세 보충: %d건 (%s)", len(api_prices), product_id)

        return product

    async def _detail_via_nuxt(self, product_id: str) -> KreamProduct | None:
        """상품 페이지의 __NUXT_DATA__에서 상세 정보 추출."""
        url = f"{KREAM_BASE}/products/{product_id}"
        html = await self._request("GET", url, parse_json=False)
        if not html:
            return None

        data = self._extract_page_data(html)
        if not data:
            return None

        return self._find_product_in_data(data, product_id)

    def _find_product_in_data(self, data, product_id: str) -> KreamProduct | None:
        """역직렬화된 데이터에서 상품 정보를 탐색하여 KreamProduct 생성."""
        # 1차: pinia 스토어에서 직접 추출 (SDUI 구조)
        product = self._find_product_in_pinia(data, product_id)
        if product:
            return product

        # 2차: deep_find 폴백 (레거시 구조)
        candidates = self._deep_find_dict(data, {"name"}, max_depth=8)

        best = None
        for c in candidates:
            has_style = bool(c.get("style_code") or c.get("styleCode") or c.get("model_number"))
            has_id = str(c.get("id", "")) == str(product_id)
            has_brand = bool(c.get("brand") or c.get("brand_name") or c.get("brandName"))

            if has_style and (has_id or has_brand):
                best = c
                break
            if has_style and best is None:
                best = c
            if has_id and has_brand and best is None:
                best = c

        if not best:
            product_data_list = self._deep_find(data, "product")
            for pd in product_data_list:
                if isinstance(pd, dict) and pd.get("name"):
                    best = pd
                    break

        if not best:
            return None

        return self._parse_product_data(best, product_id)

    def _find_product_in_pinia(self, data, product_id: str) -> KreamProduct | None:
        """pinia 스토어의 productDetail.productDetailContent.meta에서 상품 추출."""
        if not isinstance(data, dict):
            return None
        pinia = data.get("pinia")
        if not isinstance(pinia, dict):
            return None

        meta = (
            pinia.get("productDetail", {})
            .get("productDetailContent", {})
            .get("meta")
        )
        if not isinstance(meta, dict) or not meta.get("name"):
            return None

        product = KreamProduct(
            product_id=str(product_id),
            name=str(meta.get("name", "")).strip(),
            model_number=str(meta.get("style_code", "")).strip(),
            brand=str(meta.get("brand_name", "")).strip(),
            image_url=self._extract_image_url(meta),
            category=str(meta.get("category", "sneakers")).strip(),
            url=f"{KREAM_BASE}/products/{product_id}",
        )
        logger.info("크림 상품 (pinia): %s (%s)", product.name, product.model_number)
        return product

    async def _detail_via_meta(self, product_id: str) -> KreamProduct | None:
        """HTML 메타 태그에서 최소 정보 추출."""
        url = f"{KREAM_BASE}/products/{product_id}"
        html = await self._request("GET", url, parse_json=False)
        if not html:
            return None
        return self._parse_product_from_meta(html, product_id)

    def _parse_product_data(self, data: dict, product_id: str) -> KreamProduct | None:
        """API 응답 또는 Nuxt 데이터에서 KreamProduct 생성."""
        if not data:
            return None

        # 중첩 구조 언래핑
        if "product" in data and isinstance(data["product"], dict):
            data = data["product"]
        elif "data" in data and isinstance(data["data"], dict):
            data = data["data"]

        name = (
            data.get("name")
            or data.get("translated_name")
            or data.get("title")
            or ""
        )
        if not name:
            return None

        model_number = (
            data.get("style_code")
            or data.get("styleCode")
            or data.get("model_number")
            or data.get("modelNumber")
            or ""
        )

        brand_raw = data.get("brand")
        if isinstance(brand_raw, dict):
            brand = brand_raw.get("name", "")
        else:
            brand = data.get("brand_name") or data.get("brandName") or ""

        image_url = self._extract_image_url(data)

        product = KreamProduct(
            product_id=str(product_id),
            name=str(name).strip(),
            model_number=str(model_number).strip(),
            brand=str(brand).strip(),
            image_url=str(image_url),
            url=f"{KREAM_BASE}/products/{product_id}",
        )
        logger.info("크림 상품: %s (%s)", product.name, product.model_number)
        return product

    def _extract_image_url(self, data: dict) -> str:
        for key in ("media", "images", "image_urls"):
            media = data.get(key)
            if isinstance(media, list) and media:
                first = media[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, dict):
                    return first.get("url") or first.get("src") or first.get("image_url") or ""
        return data.get("image_url") or data.get("imageUrl") or data.get("thumbnail") or ""

    def _parse_product_from_meta(self, html: str, product_id: str) -> KreamProduct | None:
        try:
            name_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
            name = name_match.group(1).strip() if name_match else ""
            img_match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
            image_url = img_match.group(1) if img_match else ""
            model_match = re.search(r"[A-Z]{1,3}\d{3,5}[-\s]?\d{2,4}", html)
            model_number = model_match.group(0) if model_match else ""
            if not name:
                return None
            return KreamProduct(
                product_id=str(product_id),
                name=name,
                model_number=model_number,
                brand="",
                image_url=image_url,
                url=f"{KREAM_BASE}/products/{product_id}",
            )
        except Exception as e:
            logger.warning("메타 태그 파싱 실패 (%s): %s", product_id, e)
            return None

    # ─── 사이즈별 시세 (즉시구매가 / 즉시판매가) ────────────

    async def get_sell_prices(self, product_id: str) -> list[KreamSizePrice]:
        """사이즈별 즉시판매가 수집."""
        prices = await self._get_market_prices(product_id)
        return [p for p in prices if p.sell_now_price is not None]

    async def get_buy_prices(self, product_id: str) -> list[KreamSizePrice]:
        """사이즈별 즉시구매가 수집."""
        prices = await self._get_market_prices(product_id)
        return [p for p in prices if p.buy_now_price is not None]

    async def _get_market_prices(self, product_id: str) -> list[KreamSizePrice]:
        """사이즈별 즉시구매가/즉시판매가를 수집.

        1차: 상품 페이지 __NUXT_DATA__에서 사이즈/가격 추출
        2차: 시세 API 호출
        """
        await _random_delay()

        # __NUXT_DATA__에서 시세 추출
        prices = await self._prices_from_nuxt(product_id)
        if prices:
            return prices

        # 폴백: options/display API (SDUI)
        api_prices = await self._fetch_options_display(product_id)
        if api_prices:
            return api_prices

        return []

    async def _prices_from_nuxt(self, product_id: str) -> list[KreamSizePrice]:
        """상품 페이지 __NUXT_DATA__에서 사이즈별 시세 추출."""
        url = f"{KREAM_BASE}/products/{product_id}"
        html = await self._request("GET", url, parse_json=False)
        if not html:
            return []

        data = self._extract_page_data(html)
        if not data:
            return []

        return self._find_prices_in_data(data, product_id)

    def _find_prices_in_data(self, data, product_id: str) -> list[KreamSizePrice]:
        """역직렬화된 __NUXT_DATA__에서 사이즈별 가격 탐색."""
        # 1차: pinia 스토어의 transactionHistorySummary에서 추출 (SDUI 구조)
        pinia_prices = self._find_prices_in_pinia(data, product_id)
        if pinia_prices:
            return pinia_prices

        # 2차: 기존 deep_find 방식 (레거시)
        prices: list[KreamSizePrice] = []

        for key in ("sizes", "sizeOptions", "options", "size_prices", "sizeMap"):
            found_lists = self._deep_find(data, key)
            for found in found_lists:
                if isinstance(found, list) and found:
                    parsed = self._parse_size_list(found)
                    if parsed:
                        return parsed
                elif isinstance(found, dict):
                    parsed = self._parse_size_map(found)
                    if parsed:
                        return parsed

        price_dicts = self._deep_find_dict(data, {"size"}, max_depth=8)
        for pd in price_dicts:
            size = str(pd.get("size") or pd.get("name") or pd.get("option") or "").strip()
            if not size:
                continue

            buy_price = self._to_int(
                pd.get("buy_now_price") or pd.get("buyNowPrice")
                or pd.get("lowest_ask") or pd.get("lowestAsk") or pd.get("ask")
                or pd.get("buyPrice") or pd.get("buy_price")
            )
            sell_price = self._to_int(
                pd.get("sell_now_price") or pd.get("sellNowPrice")
                or pd.get("highest_bid") or pd.get("highestBid") or pd.get("bid")
                or pd.get("sellPrice") or pd.get("sell_price")
            )

            if buy_price or sell_price:
                prices.append(KreamSizePrice(
                    size=size,
                    buy_now_price=buy_price,
                    sell_now_price=sell_price,
                ))

        seen = set()
        unique = []
        for p in prices:
            if p.size not in seen:
                seen.add(p.size)
                unique.append(p)

        if unique:
            logger.info("사이즈별 시세: %d건 (%s) [NUXT_DATA]", len(unique), product_id)
        return unique

    def _find_prices_in_pinia(self, data, product_id: str) -> list[KreamSizePrice]:
        """pinia의 transactionHistorySummary에서 사이즈별 asks/bids 추출.

        asks (판매 입찰) → 즉시구매가 (buy_now_price): 사이즈별 최저 매도호가
        bids (구매 입찰) → 즉시판매가 (sell_now_price): 사이즈별 최고 매수호가
        sales (체결 거래) → 최근 체결가 (last_sale_price)
        """
        if not isinstance(data, dict):
            return []
        pinia = data.get("pinia")
        if not isinstance(pinia, dict):
            return []

        th = (
            pinia.get("transactionHistorySummary", {})
            .get("previousItem", {})
            .get("meta", {})
            .get("transaction_history")
        )
        if not isinstance(th, dict):
            return []

        # 사이즈별 데이터 수집 (size key → KreamSizePrice)
        size_map: dict[str, dict] = {}

        # asks → 즉시구매가 (사이즈별 최저 판매 희망가 = 구매자가 바로 살 수 있는 가격)
        asks = th.get("asks", {}).get("items", [])
        for item in asks:
            if not isinstance(item, dict):
                continue
            opt = item.get("product_option", {})
            size = str(opt.get("key", "")).strip()
            price = self._to_int(item.get("price"))
            if size and price:
                if size not in size_map:
                    size_map[size] = {}
                # 같은 사이즈의 최저가만 유지 (즉시구매가)
                cur = size_map[size].get("buy_now_price")
                if cur is None or price < cur:
                    size_map[size]["buy_now_price"] = price

        # bids → 즉시판매가 (사이즈별 최고 구매 희망가 = 판매자가 바로 팔 수 있는 가격)
        bids = th.get("bids", {}).get("items", [])
        for item in bids:
            if not isinstance(item, dict):
                continue
            opt = item.get("product_option", {})
            size = str(opt.get("key", "")).strip()
            price = self._to_int(item.get("price"))
            if size and price:
                if size not in size_map:
                    size_map[size] = {}
                # 같은 사이즈의 최고가만 유지 (즉시판매가)
                cur = size_map[size].get("sell_now_price")
                if cur is None or price > cur:
                    size_map[size]["sell_now_price"] = price
                # 입찰 수량 합산
                qty = item.get("quantity", 1)
                size_map[size]["bid_count"] = size_map[size].get("bid_count", 0) + qty

        # sales → 최근 체결가
        sales = th.get("sales", {}).get("items", [])
        for item in sales:
            if not isinstance(item, dict):
                continue
            opt = item.get("product_option", {})
            size = str(opt.get("key", "")).strip()
            price = self._to_int(item.get("price"))
            date_str = item.get("date_created", "")
            if size and price:
                if size not in size_map:
                    size_map[size] = {}
                # 첫 번째(최신) 체결가만 유지
                if "last_sale_price" not in size_map[size]:
                    size_map[size]["last_sale_price"] = price
                    if date_str:
                        size_map[size]["last_sale_date"] = self._parse_date(str(date_str))

        if not size_map:
            return []

        # KreamSizePrice 생성
        prices = []
        for size in sorted(size_map.keys(), key=lambda s: int(s) if s.isdigit() else 0):
            info = size_map[size]
            prices.append(KreamSizePrice(
                size=size,
                buy_now_price=info.get("buy_now_price"),
                sell_now_price=info.get("sell_now_price"),
                bid_count=info.get("bid_count", 0),
                last_sale_price=info.get("last_sale_price"),
                last_sale_date=info.get("last_sale_date"),
            ))

        logger.info("사이즈별 시세: %d건 (%s) [pinia]", len(prices), product_id)
        return prices

    def _parse_size_list(self, items: list) -> list[KreamSizePrice]:
        """사이즈 목록 (list of dict) 파싱."""
        prices = []
        for item in items:
            if not isinstance(item, dict):
                continue
            size = str(
                item.get("size") or item.get("name")
                or item.get("option") or item.get("optionValue") or ""
            ).strip()
            if not size:
                continue

            buy_price = self._to_int(
                item.get("buy_now_price") or item.get("buyNowPrice")
                or item.get("lowest_ask") or item.get("lowestAsk") or item.get("ask")
                or item.get("buyPrice") or item.get("buy_price")
                or item.get("price")
            )
            sell_price = self._to_int(
                item.get("sell_now_price") or item.get("sellNowPrice")
                or item.get("highest_bid") or item.get("highestBid") or item.get("bid")
                or item.get("sellPrice") or item.get("sell_price")
            )
            last_sale = self._to_int(
                item.get("last_sale_price") or item.get("lastSalePrice")
                or item.get("lastPrice") or item.get("last_price")
            )

            if buy_price or sell_price or last_sale:
                prices.append(KreamSizePrice(
                    size=size,
                    buy_now_price=buy_price,
                    sell_now_price=sell_price,
                    last_sale_price=last_sale,
                ))

        return prices

    def _parse_size_map(self, data: dict) -> list[KreamSizePrice]:
        """사이즈 맵 ({size: {price_data}}) 파싱."""
        prices = []
        for size_key, val in data.items():
            if not isinstance(val, dict):
                continue
            buy_price = self._to_int(
                val.get("buy_now_price") or val.get("buyNowPrice")
                or val.get("lowest_ask") or val.get("lowestAsk")
                or val.get("buyPrice") or val.get("price")
            )
            sell_price = self._to_int(
                val.get("sell_now_price") or val.get("sellNowPrice")
                or val.get("highest_bid") or val.get("highestBid")
                or val.get("sellPrice")
            )
            if buy_price or sell_price:
                prices.append(KreamSizePrice(
                    size=str(size_key),
                    buy_now_price=buy_price,
                    sell_now_price=sell_price,
                ))
        return prices

    def _parse_market_prices_from_dict(self, data: dict, product_id: str) -> list[KreamSizePrice]:
        """API 응답 dict에서 시세 파싱."""
        items = self._unwrap_list(data)
        if items is None:
            return []

        prices = self._parse_size_list(items)
        if prices:
            logger.info("사이즈별 시세: %d건 (%s) [API]", len(prices), product_id)
        return prices

    # ─── 거래 내역 ──────────────────────────────────────

    async def get_trade_history(self, product_id: str) -> dict:
        """체결 내역에서 거래량/추세 수집.

        Returns:
            {"volume_7d": int, "volume_30d": int, "last_trade_date": datetime|None, "price_trend": str}
        """
        empty = {"volume_7d": 0, "volume_30d": 0, "last_trade_date": None, "price_trend": ""}
        await _random_delay()

        # 1차: __NUXT_DATA__에서 거래내역 추출
        nuxt_result = await self._trades_from_nuxt(product_id)

        # pinia sales.items는 최근 5건 미리보기 → volume_7d 캡 발생
        # volume이 pinia 아이템 수와 같으면(캡 의심) screens API로 보충
        pinia_count = nuxt_result.get("_pinia_items_count", 0) if nuxt_result else 0
        pinia_capped = (
            nuxt_result
            and pinia_count > 0
            and nuxt_result["volume_7d"] >= pinia_count
        )

        if nuxt_result and not pinia_capped:
            nuxt_result.pop("_pinia_items_count", None)
            if nuxt_result["volume_7d"] > 0 or nuxt_result["volume_30d"] > 0 or nuxt_result["last_trade_date"]:
                return nuxt_result

        # 2차: screens API로 거래 내역 + bids/asks 수 확인
        screens_result = await self._trades_from_screens_api(product_id)
        if screens_result and (screens_result["volume_7d"] > 0 or screens_result["volume_30d"] > 0):
            return screens_result

        # 모든 API 실패 시 pinia 결과 + 볼륨 추정
        if nuxt_result:
            if pinia_capped and nuxt_result["volume_7d"] > 0:
                nuxt_result["volume_7d"] = self._estimate_volume(
                    nuxt_result["volume_7d"], pinia_count,
                )
            nuxt_result.pop("_pinia_items_count", None)
            if nuxt_result["volume_7d"] > 0 or nuxt_result["volume_30d"] > 0 or nuxt_result["last_trade_date"]:
                return nuxt_result

        return empty

    @staticmethod
    def _estimate_volume(volume_7d: int, pinia_count: int) -> int:
        """pinia 캡 도달 시 볼륨 추정.

        5건 모두 7일 내 → 최소 5건이지만 실제로는 더 많을 가능성 높음.
        보수적으로 캡의 2배 추정 (hot tier 진입 가능하도록).
        """
        if volume_7d >= pinia_count >= 4:
            return max(volume_7d, pinia_count * 2)
        return volume_7d

    async def _trades_from_screens_api(self, product_id: str) -> dict | None:
        """screens API에서 거래 내역 추출.

        /api/screens/products/{id} 의 transaction_history 섹션에서
        sales/asks/bids 아이템을 가져온다.
        """
        await _random_delay()
        data = await self._request(
            "GET", f"/api/screens/products/{product_id}",
            max_retries=2,
        )
        if not data or not isinstance(data, dict):
            return None

        # 재귀적으로 transaction_history 찾기
        th = self._find_transaction_history_in_screens(data)
        if not th:
            return None

        sales = th.get("sales", {}).get("items", [])
        if not sales:
            return None

        trades_for_stats = []
        for item in sales:
            if not isinstance(item, dict):
                continue
            trades_for_stats.append({
                "price": item.get("price"),
                "date": item.get("date_created", ""),
            })

        if not trades_for_stats:
            return None

        result = self._calculate_trade_stats(trades_for_stats, product_id)

        # bids/asks 수로 활성도 보강 — 5건 캡이라도 asks 5건이면 유동성 높음
        asks_count = len(th.get("asks", {}).get("items", []))
        bids_count = len(th.get("bids", {}).get("items", []))
        result["_screens_asks"] = asks_count
        result["_screens_bids"] = bids_count

        # 캡 감지: 5건 모두 7일 내이고 bids+asks 충분 → 볼륨 추정
        if result["volume_7d"] >= len(trades_for_stats) and len(trades_for_stats) >= 4:
            if asks_count + bids_count >= 6:
                result["volume_7d"] = max(result["volume_7d"], len(trades_for_stats) * 3)
            else:
                result["volume_7d"] = max(result["volume_7d"], len(trades_for_stats) * 2)

        result.pop("_screens_asks", None)
        result.pop("_screens_bids", None)

        logger.info(
            "거래내역 (%s): [screens] 7일=%d, 30일=%d (sales=%d, asks=%d, bids=%d)",
            product_id, result["volume_7d"], result["volume_30d"],
            len(trades_for_stats), asks_count, bids_count,
        )
        return result

    @staticmethod
    def _find_transaction_history_in_screens(data) -> dict | None:
        """screens API 응답에서 transaction_history 딕셔너리 찾기."""
        if isinstance(data, dict):
            if "transaction_history" in data:
                th = data["transaction_history"]
                if isinstance(th, dict) and "sales" in th:
                    return th
            for v in data.values():
                found = KreamCrawler._find_transaction_history_in_screens(v)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = KreamCrawler._find_transaction_history_in_screens(item)
                if found:
                    return found
        return None

    async def _trades_from_nuxt(self, product_id: str) -> dict | None:
        """상품 페이지 __NUXT_DATA__에서 거래 내역 추출."""
        url = f"{KREAM_BASE}/products/{product_id}"
        html = await self._request("GET", url, parse_json=False)
        if not html:
            return None

        data = self._extract_page_data(html)
        if not data:
            return None

        return self._find_trades_in_data(data, product_id)

    def _find_trades_in_data(self, data, product_id: str) -> dict | None:
        """역직렬화된 데이터에서 거래 내역 탐색."""
        # 1차: pinia 스토어에서 직접 추출 (SDUI 구조)
        pinia_trades = self._find_trades_in_pinia(data, product_id)
        if pinia_trades and (pinia_trades["volume_7d"] > 0 or pinia_trades["volume_30d"] > 0):
            return pinia_trades

        # 2차: deep_find 폴백 (레거시)
        for key in ("sales", "trades", "tradeHistory", "market_sales", "recentSales"):
            found_lists = self._deep_find(data, key)
            for found in found_lists:
                if isinstance(found, list) and found:
                    result = self._calculate_trade_stats(found, product_id)
                    if result["volume_7d"] > 0 or result["volume_30d"] > 0:
                        return result
                elif isinstance(found, dict):
                    inner = found.get("items") or found.get("data") or found.get("list")
                    if isinstance(inner, list) and inner:
                        result = self._calculate_trade_stats(inner, product_id)
                        if result["volume_7d"] > 0 or result["volume_30d"] > 0:
                            return result

        trade_candidates = self._deep_find_dict(data, {"price"}, max_depth=6)
        dated_trades = [c for c in trade_candidates if any(
            k in c for k in ("date", "created_at", "createdAt", "trade_date", "tradeDate",
                              "date_created", "dateCreated")
        )]
        if dated_trades:
            result = self._calculate_trade_stats(dated_trades, product_id)
            if result["volume_7d"] > 0 or result["volume_30d"] > 0:
                return result

        return pinia_trades  # 거래량 0이라도 pinia에서 가져온 것 반환

    def _find_trades_in_pinia(self, data, product_id: str) -> dict | None:
        """pinia의 transactionHistorySummary에서 체결 내역 추출."""
        if not isinstance(data, dict):
            return None
        pinia = data.get("pinia")
        if not isinstance(pinia, dict):
            return None

        th = (
            pinia.get("transactionHistorySummary", {})
            .get("previousItem", {})
            .get("meta", {})
            .get("transaction_history")
        )
        if not isinstance(th, dict):
            return None

        sales = th.get("sales", {}).get("items", [])
        if not isinstance(sales, list) or not sales:
            return None

        # date_created 필드를 _calculate_trade_stats 가 인식할 수 있도록 매핑
        trades_for_stats = []
        for item in sales:
            if not isinstance(item, dict):
                continue
            trades_for_stats.append({
                "price": item.get("price"),
                "date": item.get("date_created", ""),
            })

        if not trades_for_stats:
            return None

        result = self._calculate_trade_stats(trades_for_stats, product_id)
        # pinia 아이템 수 전달 — 캡 감지용 (get_trade_history에서 사용)
        result["_pinia_items_count"] = len(trades_for_stats)
        logger.info(
            "거래내역 (%s): [pinia] 7일=%d, 30일=%d (items=%d)",
            product_id, result["volume_7d"], result["volume_30d"], len(trades_for_stats),
        )
        return result

    def _parse_trade_data(self, data: dict, product_id: str) -> dict:
        """API 응답에서 거래 내역 파싱."""
        empty = {"volume_7d": 0, "volume_30d": 0, "last_trade_date": None, "price_trend": ""}
        items = self._unwrap_list(data)
        if items is None:
            return empty
        return self._calculate_trade_stats(items, product_id)

    def _calculate_trade_stats(self, items: list, product_id: str) -> dict:
        """거래 목록에서 통계 계산."""
        empty = {"volume_7d": 0, "volume_30d": 0, "last_trade_date": None, "price_trend": ""}

        trades = []
        for item in items[:200]:
            if not isinstance(item, dict):
                continue
            price = self._to_int(
                item.get("price") or item.get("amount")
                or item.get("trade_price") or item.get("tradePrice")
            )
            date_str = str(
                item.get("date") or item.get("created_at") or item.get("createdAt")
                or item.get("trade_date") or item.get("tradeDate")
                or item.get("date_created") or item.get("dateCreated") or ""
            )
            trade_date = self._parse_date(date_str)
            if price and trade_date:
                trades.append({"price": price, "date": trade_date})

        if not trades:
            return empty

        now = datetime.now()
        volume_7d = sum(1 for t in trades if (now - t["date"]).days <= 7)
        volume_30d = sum(1 for t in trades if (now - t["date"]).days <= 30)
        last_trade_date = trades[0]["date"]

        price_trend = ""
        if len(trades) >= 10:
            recent_avg = sum(t["price"] for t in trades[:5]) / 5
            older_avg = sum(t["price"] for t in trades[5:10]) / 5
            if recent_avg > older_avg * 1.03:
                price_trend = "상승"
            elif recent_avg < older_avg * 0.97:
                price_trend = "하락"
            else:
                price_trend = "보합"

        result = {
            "volume_7d": volume_7d,
            "volume_30d": volume_30d,
            "last_trade_date": last_trade_date,
            "price_trend": price_trend,
        }
        logger.info("거래내역 (%s): 7일=%d, 30일=%d, 추세=%s", product_id, volume_7d, volume_30d, price_trend)
        return result

    # ─── 전체 수집 ──────────────────────────────────────

    async def get_full_product_info(self, product_id: str) -> KreamProduct | None:
        """상품 전체 정보 수집 (상세 + 시세 + 거래내역).

        전략:
        1단계: HTML → __NUXT_DATA__ (pinia) 파싱으로 기본 정보 + 일부 가격
        2단계: 인증 API /api/p/options/display 로 전체 사이즈별 가격 보충
        3단계: 레거시 API 폴백 (비인증)
        """
        # 1단계: HTML 한 번으로 모든 데이터 추출 시도
        url = f"{KREAM_BASE}/products/{product_id}"
        html = await self._request("GET", url, parse_json=False)

        product = None
        size_prices = []
        trade_result = None

        if html:
            data = self._extract_page_data(html)
            if data:
                product = self._find_product_in_data(data, product_id)
                size_prices = self._find_prices_in_data(data, product_id)
                trade_result = self._find_trades_in_data(data, product_id)

            if not product:
                product = self._parse_product_from_meta(html, product_id)

        if not product:
            await _random_delay()
            product = await self._detail_via_meta(product_id)
            if not product:
                return None

        # 2단계: options/display API로 전체 사이즈별 가격 보충
        api_prices = await self._fetch_options_display(product_id)
        if api_prices:
            # API 결과가 더 완전하면 교체, 아니면 병합
            if len(api_prices) > len(size_prices):
                api_map = {p.size: p for p in api_prices}
                for p in size_prices:
                    if p.size in api_map:
                        ap = api_map[p.size]
                        if not ap.last_sale_price and p.last_sale_price:
                            ap.last_sale_price = p.last_sale_price
                            ap.last_sale_date = p.last_sale_date
                    else:
                        api_prices.append(p)
                api_prices.sort(key=lambda p: int(p.size) if p.size.isdigit() else 0)
                size_prices = api_prices
                logger.info("사이즈별 시세 보충: %d건 (API+pinia 병합)", len(size_prices))
            elif not size_prices:
                size_prices = api_prices

        # 거래내역 보강: pinia 캡 감지 시 screens API로 볼륨 추정
        pinia_count = trade_result.get("_pinia_items_count", 0) if trade_result else 0
        pinia_capped = (
            trade_result
            and pinia_count > 0
            and trade_result["volume_7d"] >= pinia_count
        )
        if pinia_capped or not trade_result or (
            trade_result["volume_7d"] == 0
            and trade_result["volume_30d"] == 0
            and not trade_result.get("last_trade_date")
        ):
            screens_trades = await self._trades_from_screens_api(product_id)
            if screens_trades and (
                screens_trades["volume_7d"] > trade_result.get("volume_7d", 0)
                if trade_result else screens_trades["volume_7d"] > 0
            ):
                trade_result = screens_trades
            elif pinia_capped and trade_result:
                trade_result["volume_7d"] = self._estimate_volume(
                    trade_result["volume_7d"], pinia_count,
                )

        # 최종 조합
        product.size_prices = size_prices

        if trade_result:
            trade_result.pop("_pinia_items_count", None)
            product.volume_7d = trade_result["volume_7d"]
            product.volume_30d = trade_result["volume_30d"]
            product.last_trade_date = trade_result["last_trade_date"]
            product.price_trend = trade_result["price_trend"]

        product.fetched_at = datetime.now()

        logger.info(
            "전체 수집 완료: %s | 사이즈 %d개 | 7일 거래량 %d | 로그인=%s",
            product.name, len(product.size_prices), product.volume_7d,
            "Y" if self._logged_in else "N",
        )
        return product

    async def _fetch_options_display(self, product_id: str) -> list[KreamSizePrice]:
        """API /api/p/options/display 로 전체 사이즈별 가격 조회.

        buying + selling 양쪽을 호출하여 즉시구매가/즉시판매가를 모두 수집.
        api.kream.co.kr 도메인 + X-KREAM 인증 헤더로 호출.
        """
        await _random_delay()

        size_map: dict[str, dict] = {}

        # buying: 즉시구매가 (판매 입찰 = asks)
        buy_data = await self._request(
            "GET", "/api/p/options/display",
            params={"product_id": product_id, "picker_type": "buying"},
            max_retries=2,
        )
        if buy_data:
            self._merge_options_into_map(buy_data, size_map, "buy")
            logger.info("options/display (buying) 응답 수신 (%s)", product_id)

        await _random_delay()

        # selling: 즉시판매가 (구매 입찰 = bids)
        sell_data = await self._request(
            "GET", "/api/p/options/display",
            params={"product_id": product_id, "picker_type": "selling"},
            max_retries=2,
        )
        if sell_data:
            self._merge_options_into_map(sell_data, size_map, "sell")
            logger.info("options/display (selling) 응답 수신 (%s)", product_id)

        if not size_map:
            return []

        prices = []
        for size in sorted(size_map.keys(), key=lambda s: int(s) if s.isdigit() else 0):
            info = size_map[size]
            prices.append(KreamSizePrice(
                size=size,
                buy_now_price=info.get("buy_now_price"),
                sell_now_price=info.get("sell_now_price"),
                bid_count=info.get("bid_count", 0),
                last_sale_price=info.get("last_sale_price"),
            ))

        logger.info("사이즈별 시세: %d건 (%s) [options/display API]", len(prices), product_id)
        return prices

    def _merge_options_into_map(self, data, size_map: dict, mode: str) -> None:
        """options/display API 응답을 size_map에 병합.

        SDUI 형식 응답에서 action parameters의 option_key + price 추출.
        레거시 리스트 형식도 폴백으로 지원.
        """
        # 1차: SDUI 형식 — action parameters에서 option_key/price 추출
        sdui_pairs = self._extract_sdui_option_prices(data)
        if sdui_pairs:
            for pair in sdui_pairs:
                size = pair["option_key"]
                price = pair["price"]
                if size not in size_map:
                    size_map[size] = {}
                if price:
                    if mode == "buy":
                        cur = size_map[size].get("buy_now_price")
                        if cur is None or price < cur:
                            size_map[size]["buy_now_price"] = price
                    elif mode == "sell":
                        cur = size_map[size].get("sell_now_price")
                        if cur is None or price > cur:
                            size_map[size]["sell_now_price"] = price
            return

        # 2차: 레거시 리스트 형식 폴백
        items = self._unwrap_list(data)
        if items is None and isinstance(data, dict):
            for key in ("options", "sizes", "sales_options", "buying_options",
                        "selling_options", "product_options"):
                val = data.get(key)
                if isinstance(val, list):
                    items = val
                    break

        if items is None:
            return

        for item in items:
            if not isinstance(item, dict):
                continue

            size = ""
            opt = item.get("product_option") or item.get("option")
            if isinstance(opt, dict):
                size = str(opt.get("key") or opt.get("size") or opt.get("name") or "").strip()
            if not size:
                size = str(
                    item.get("size") or item.get("option") or item.get("key")
                    or item.get("name") or item.get("optionValue") or ""
                ).strip()
            if not size:
                continue

            if size not in size_map:
                size_map[size] = {}

            price = self._to_int(
                item.get("price") or item.get("amount")
                or item.get("display_price") or item.get("displayPrice")
            )

            if price:
                if mode == "buy":
                    cur = size_map[size].get("buy_now_price")
                    if cur is None or price < cur:
                        size_map[size]["buy_now_price"] = price
                elif mode == "sell":
                    cur = size_map[size].get("sell_now_price")
                    if cur is None or price > cur:
                        size_map[size]["sell_now_price"] = price

            last_sale = self._to_int(
                item.get("last_sale_price") or item.get("lastSalePrice")
                or item.get("last_price")
            )
            if last_sale and "last_sale_price" not in size_map[size]:
                size_map[size]["last_sale_price"] = last_sale

            qty = item.get("quantity") or item.get("bid_count") or 0
            if isinstance(qty, int) and qty > 0 and mode == "sell":
                size_map[size]["bid_count"] = size_map[size].get("bid_count", 0) + qty

    @staticmethod
    def _extract_sdui_option_prices(data) -> list[dict]:
        """SDUI 응답에서 option_key + price 쌍 추출 (재귀)."""
        results = []
        seen = set()

        def _walk(obj):
            if isinstance(obj, dict):
                params = obj.get("parameters")
                if isinstance(params, dict):
                    option_key = params.get("option_key")
                    price = params.get("price")
                    if option_key and price:
                        ok = option_key[0] if isinstance(option_key, list) else option_key
                        pr = price[0] if isinstance(price, list) else price
                        if ok and pr and ok not in seen:
                            seen.add(ok)
                            try:
                                results.append({"option_key": str(ok), "price": int(pr)})
                            except (ValueError, TypeError):
                                pass
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(data)
        return results

    # ─── 유틸리티 ────────────────────────────────────────

    @staticmethod
    def _unwrap_list(data) -> list | None:
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return None
        for key in ("items", "sales", "sizes", "options", "data", "trades", "list",
                     "sizeOptions", "size_prices"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                for inner_key in ("items", "data", "list", "sizes"):
                    inner = val.get(inner_key)
                    if isinstance(inner, list):
                        return inner
        return None

    @staticmethod
    def _to_int(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, float):
            return int(value) if value > 0 else None
        if isinstance(value, str):
            digits = re.sub(r"[^\d]", "", value)
            return int(digits) if digits else None
        return None

    @staticmethod
    def _parse_date(text: str) -> datetime | None:
        if not text or not text.strip():
            return None
        text = text.strip()
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%y/%m/%d",
            "%Y/%m/%d",
            "%y.%m.%d",
        ):
            try:
                return datetime.strptime(text[:26], fmt)
            except (ValueError, IndexError):
                continue
        try:
            ts = int(text)
            if ts > 1e12:
                ts = ts // 1000
            if 1e9 < ts < 2e10:
                return datetime.fromtimestamp(ts)
        except (ValueError, OverflowError, OSError):
            pass
        return None

    # ─── 인기 상품 수집 (자동스캔용) ─────────────────────

    async def get_popular_products(
        self,
        category: str = "sneakers",
        sort: str = "popular",
        limit: int = 50,
    ) -> list[dict]:
        """크림 인기/급상승/거래량 많은 상품 수집.

        Args:
            category: 카테고리 (sneakers, clothing, bags 등)
            sort: 정렬 기준 (popular, sales, premium, release_date)
            limit: 최대 수집 수

        Returns:
            [{"product_id", "name", "brand", "url", "model_number"}, ...]
        """
        all_results: list[dict] = []
        seen_ids: set[str] = set()

        # 카테고리별 브랜드 키워드 (크림 검색 페이지는 키워드 필수)
        category_brand_keywords = {
            "sneakers": ["nike", "jordan", "adidas", "new balance", "asics"],
            "shoes": ["nike", "jordan", "adidas", "new balance"],
            "clothing": ["nike", "adidas", "stussy", "supreme"],
            "apparel": ["nike", "adidas", "stussy"],
            "bags": ["nike", "supreme"],
            "accessories": ["nike", "supreme"],
        }
        brand_keywords = category_brand_keywords.get(category, ["nike", "jordan", "adidas"])

        # 정렬 매핑
        sort_params = {
            "popular": "popular",
            "sales": "sales",
            "premium": "premium",
            "release_date": "release",
        }
        sort_value = sort_params.get(sort, sort)

        # 각 브랜드 키워드로 검색 → 인기순으로 수집
        per_brand = max(limit // len(brand_keywords), 10)
        for keyword in brand_keywords:
            if len(all_results) >= limit:
                break
            await _random_delay()
            search_url = (
                f"{KREAM_BASE}/search?keyword={keyword}&tab=products&sort={sort_value}"
            )
            html = await self._request("GET", search_url, parse_json=False)
            if html:
                data = self._extract_page_data(html)
                if data:
                    products = self._extract_listing_products(data)
                    added = 0
                    for p in products:
                        if added >= per_brand:
                            break
                        pid = p["product_id"]
                        if pid not in seen_ids:
                            seen_ids.add(pid)
                            all_results.append(p)
                            added += 1

        logger.info(
            "인기 상품 수집: %d건 (카테고리=%s, 정렬=%s)",
            len(all_results), category, sort,
        )
        return all_results[:limit]

    async def get_trending_products(self, limit: int = 30) -> list[dict]:
        """크림 급상승 / 트렌딩 상품 수집.

        급상승 페이지 또는 메인 페이지의 인기 섹션에서 수집.
        """
        all_results: list[dict] = []
        seen_ids: set[str] = set()

        await _random_delay()

        # 1차: 브랜드 키워드로 거래량순 검색 (급상승에 가까운 정렬)
        trending_keywords = ["nike", "jordan", "adidas", "new balance"]
        for keyword in trending_keywords:
            if len(all_results) >= limit:
                break
            search_url = f"{KREAM_BASE}/search?keyword={keyword}&tab=products&sort=sales"
            html = await self._request("GET", search_url, parse_json=False, max_retries=2)
            if html:
                data = self._extract_page_data(html)
                if data:
                    products = self._extract_listing_products(data)
                    added = 0
                    for p in products:
                        if added >= 10 or len(all_results) >= limit:
                            break
                        if p["product_id"] not in seen_ids:
                            seen_ids.add(p["product_id"])
                            all_results.append(p)
                            added += 1
            await _random_delay()

        # 2차: 메인 페이지에서 추출
        if not all_results:
            html = await self._request("GET", KREAM_BASE, parse_json=False)
            if html:
                data = self._extract_page_data(html)
                if data:
                    products = self._extract_listing_products(data)
                    for p in products:
                        if p["product_id"] not in seen_ids:
                            seen_ids.add(p["product_id"])
                            all_results.append(p)

        logger.info("급상승 상품 수집: %d건", len(all_results))
        return all_results[:limit]

    def _extract_listing_products(self, data) -> list[dict]:
        """리스팅/검색 페이지 데이터에서 상품 목록 추출.

        SDUI 구조 (product_card display_type) 우선 처리:
        - items[].display_type == "product_card"
        - items[].actions[].parameters.properties JSON에 상품 데이터 존재
        - items[].actions[0].value에 URL (products/{id})
        """
        results = []

        # 1차: SDUI product_card 추출 (Nuxt 3 최신 구조)
        items_lists = self._deep_find(data, "items")
        for found in items_lists:
            if not isinstance(found, list) or not found:
                continue
            for item in found[:80]:
                if not isinstance(item, dict):
                    continue
                if item.get("display_type") != "product_card":
                    continue
                parsed = self._extract_sdui_product_card(item)
                if parsed:
                    results.append(parsed)
            if results:
                return results

        # 2차: 기존 구조 (products/items 키에서 직접 추출)
        for key in ("products", "list", "cards", "contents"):
            found_lists = self._deep_find(data, key)
            for found in found_lists:
                if isinstance(found, list) and found:
                    for item in found[:60]:
                        parsed = self._extract_product_summary(item)
                        if parsed:
                            if isinstance(item, dict):
                                model = (
                                    item.get("style_code")
                                    or item.get("styleCode")
                                    or item.get("model_number")
                                    or ""
                                )
                                parsed["model_number"] = str(model).strip()
                            results.append(parsed)
                    if results:
                        return results

        # 3차: id+name을 가진 dict 탐색
        if not results:
            candidates = self._deep_find_dict(data, {"id", "name"}, max_depth=8)
            for c in candidates[:60]:
                parsed = self._extract_product_summary(c)
                if parsed:
                    if isinstance(c, dict):
                        model = (
                            c.get("style_code")
                            or c.get("styleCode")
                            or c.get("model_number")
                            or ""
                        )
                        parsed["model_number"] = str(model).strip()
                    results.append(parsed)

        return results

    def _extract_sdui_product_card(self, item: dict) -> dict | None:
        """SDUI product_card 아이템에서 상품 정보 추출.

        actions의 event_log properties JSON에서 상품 데이터를 파싱한다.
        """
        actions = item.get("actions")
        if not isinstance(actions, list):
            return None

        product_data = None
        product_url = ""

        for action in actions:
            if not isinstance(action, dict):
                continue

            # URL 추출 (click 액션)
            if action.get("type") == "url" and action.get("trigger") == "click":
                product_url = action.get("value", "")

            # event_log에서 상품 데이터 추출
            if action.get("type") == "event_log" and action.get("value") in (
                "click_product", "impression_product",
            ):
                params = action.get("parameters", {})
                if not isinstance(params, dict):
                    continue
                props_list = params.get("properties", [])
                if isinstance(props_list, list) and props_list:
                    props_str = props_list[0] if isinstance(props_list[0], str) else ""
                    if props_str:
                        try:
                            product_data = json.loads(props_str)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if product_data:
                        break

        if not product_data or not isinstance(product_data, dict):
            # URL에서라도 product_id 추출
            if product_url:
                match = re.search(r"/products/(\d+)", product_url)
                if match:
                    return {
                        "product_id": match.group(1),
                        "name": "",
                        "brand": "",
                        "url": product_url,
                        "model_number": "",
                        "volume_7d": 0,
                    }
            return None

        product_id = str(product_data.get("product_id", ""))
        if not product_id:
            return None

        name = (
            product_data.get("product_name_ko")
            or product_data.get("product_name_en")
            or ""
        )
        brand = product_data.get("brand_name", "")
        model_number = product_data.get("product_style_code", "")
        url = product_url or f"{KREAM_BASE}/products/{product_id}"

        return {
            "product_id": product_id,
            "name": str(name).strip(),
            "brand": str(brand).strip(),
            "url": url,
            "model_number": str(model_number).strip(),
            "volume_7d": 0,
        }


# 싱글톤 인스턴스
kream_crawler = KreamCrawler()
