"""무신사(Musinsa) 크롤러.

무신사에서 상품 검색, 등급 할인가 수집, 사이즈별 재고 확인.
로그인 상태에서 크롤링해야 회원 등급 할인가를 가져올 수 있다.

무신사는 Playwright 직접 접속이 가능하므로 별도 브라우저 컨텍스트를 사용한다.
로그인 세션은 storage state 파일로 저장/복원한다.
"""

import asyncio
import json
import random
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Response, async_playwright

from src.config import DATA_DIR, settings
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger

logger = setup_logger("musinsa_crawler")

MUSINSA_BASE = "https://www.musinsa.com"
SESSION_FILE = DATA_DIR / "musinsa_session.json"


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


async def _random_delay() -> None:
    delay = random.uniform(settings.request_delay_min, settings.request_delay_max)
    await asyncio.sleep(delay)


class MusinsaCrawler:
    """무신사 크롤러."""

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._connected = False

    async def connect(self) -> BrowserContext:
        """Playwright 브라우저 시작 및 세션 복원."""
        if self._connected and self._context:
            return self._context

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )

        # 저장된 세션이 있으면 복원
        if SESSION_FILE.exists():
            try:
                self._context = await self._browser.new_context(
                    storage_state=str(SESSION_FILE),
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                logger.info("무신사 세션 복원 완료")
            except Exception as e:
                logger.warning("세션 복원 실패, 새 컨텍스트 생성: %s", e)
                self._context = await self._create_fresh_context()
        else:
            self._context = await self._create_fresh_context()

        self._connected = True
        return self._context

    async def _create_fresh_context(self) -> BrowserContext:
        return await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

    async def save_session(self) -> None:
        """현재 세션(쿠키 등)을 파일로 저장."""
        if self._context:
            state = await self._context.storage_state()
            SESSION_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            logger.info("무신사 세션 저장 완료: %s", SESSION_FILE)

    async def check_login_status(self) -> bool:
        """로그인 상태 확인."""
        ctx = await self.connect()
        page = await ctx.new_page()
        try:
            await page.goto(
                f"{MUSINSA_BASE}/my-page", wait_until="domcontentloaded", timeout=15000
            )
            await page.wait_for_load_state("networkidle", timeout=5000)
            # 로그인 안 되어 있으면 로그인 페이지로 리다이렉트
            is_logged_in = "/login" not in page.url and "/member/login" not in page.url
            logger.info("무신사 로그인 상태: %s", "로그인됨" if is_logged_in else "로그아웃")
            return is_logged_in
        except Exception as e:
            logger.error("무신사 로그인 확인 실패: %s", e)
            return False
        finally:
            await page.close()

    async def login_manual(self) -> bool:
        """수동 로그인을 위한 브라우저 창 열기.

        headful 브라우저를 띄워서 사용자가 직접 로그인.
        로그인 후 세션을 저장한다.
        """
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        # headful 모드로 새 브라우저 열기
        browser = await self._playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(f"{MUSINSA_BASE}/member/login", wait_until="domcontentloaded")
        logger.info("무신사 로그인 페이지가 열렸습니다. 브라우저에서 로그인해주세요.")

        # 로그인 완료 대기 (마이페이지로 이동하거나 URL이 바뀔 때까지)
        try:
            await page.wait_for_url(
                lambda url: "/login" not in url and "/member/login" not in url,
                timeout=120000,  # 2분 대기
            )
            logger.info("로그인 감지됨. 세션 저장 중...")

            # 세션 저장
            state = await context.storage_state()
            SESSION_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

            # 기존 컨텍스트 교체
            if self._context:
                await self._context.close()
            self._context = await self._browser.new_context(
                storage_state=str(SESSION_FILE),
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )

            logger.info("무신사 로그인 및 세션 저장 완료")
            return True
        except Exception as e:
            logger.error("무신사 로그인 시간 초과 또는 실패: %s", e)
            return False
        finally:
            await browser.close()

    async def search_products(self, keyword: str, category: str = "") -> list[dict]:
        """무신사에서 키워드로 상품 검색.

        Args:
            keyword: 검색 키워드
            category: 카테고리 필터. 빈 문자열이면 전체 검색.
                예: "shoes", "top", "pants"

        Returns:
            [{"product_id": str, "name": str, "brand": str, "url": str, "price": int}, ...]
        """
        ctx = await self.connect()
        page = await ctx.new_page()

        try:
            search_url = f"{MUSINSA_BASE}/search/goods?keyword={keyword}"
            if category:
                search_url += f"&category={category}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            await _random_delay()

            results = []
            seen_ids: set[str] = set()

            # 상품 링크가 로드될 때까지 대기
            await page.wait_for_selector(
                "a[href*='/products/']",
                timeout=10000,
            )

            # 상품 링크에서 직접 정보 추출
            links = await page.query_selector_all("a[href*='/products/']")

            for link in links:
                try:
                    href = await link.get_attribute("href") or ""
                    match = re.search(r"/products/(\d+)", href)
                    if not match:
                        continue

                    product_id = match.group(1)
                    if product_id in seen_ids:
                        continue

                    # 텍스트가 있는 링크만 (상품명 링크)
                    name = (await link.inner_text()).strip()
                    if not name or len(name) < 3:
                        continue

                    seen_ids.add(product_id)
                    full_url = href if href.startswith("http") else f"{MUSINSA_BASE}{href}"
                    results.append({
                        "product_id": product_id,
                        "name": name,
                        "brand": "",
                        "url": full_url,
                        "price": 0,
                    })
                except Exception:
                    continue

            logger.info("무신사 검색 '%s': %d건", keyword, len(results))
            return results[:30]

        except Exception as e:
            logger.warning("무신사 검색 실패 (%s): %s", keyword, e)
            return []
        finally:
            await page.close()

    # 카테고리 코드 (실제 무신사 사이트에서 확인)
    CATEGORY_CODES: dict[str, str] = {
        "신발": "103",         # 무신사 킥스 (신발 전체)
        "스니커즈": "103",     # 신발 = 103
        "스포츠": "017",       # 스포츠/레저
        "바지": "003",
        "상의": "001",
        "아우터": "002",
        "가방": "004",
    }

    async def fetch_category_listing(
        self,
        category: str,
        max_pages: int = 1,
    ) -> list[dict]:
        """무신사 카테고리 리스팅 — 스크롤+인터셉트 방식.

        무신사 API는 페이지 2+에 hmacId(클라이언트 생성)가 필요하므로
        직접 fetch 호출이 불가능. 실제 브라우저 스크롤로 무한스크롤을
        트리거하고 API 응답을 인터셉트하여 데이터를 수집한다.

        Args:
            category: 카테고리 코드 (예: "103", "017")
            max_pages: 수집할 페이지 수 (1페이지 = 60건)

        Returns:
            [{"goodsNo": str, "goodsName": str, "brand": str,
              "brandName": str, "price": int, "saleRate": int,
              "isSoldOut": bool}, ...]
        """
        ctx = await self.connect()
        page = await ctx.new_page()

        all_items: list[dict] = []
        seen_goods: set[str] = set()
        pages_collected = 0

        def _parse_goods(goods_list: list) -> list[dict]:
            """API 응답의 goods 목록을 파싱."""
            parsed = []
            for item in goods_list:
                if not isinstance(item, dict):
                    continue
                goods_no = str(item.get("goodsNo") or "")
                if not goods_no or goods_no in seen_goods:
                    continue
                seen_goods.add(goods_no)
                parsed.append({
                    "goodsNo": goods_no,
                    "goodsName": item.get("goodsName") or "",
                    "brand": item.get("brand") or "",
                    "brandName": item.get("brandName") or "",
                    "price": item.get("price") or item.get("normalPrice") or 0,
                    "saleRate": item.get("saleRate") or 0,
                    "isSoldOut": bool(item.get("isSoldOut", False)),
                })
            return parsed

        async def on_response(response: Response) -> None:
            nonlocal pages_collected
            url = response.url
            if "plp/goods" not in url or "count" in url or "label" in url:
                return
            try:
                data = await response.json()
                goods_list = (data.get("data") or {}).get("list") or []
                if goods_list:
                    parsed = _parse_goods(goods_list)
                    all_items.extend(parsed)
                    pages_collected += 1
                    logger.debug(
                        "카테고리 리스팅 인터셉트: page=%d, +%d건 (총 %d건)",
                        pages_collected, len(parsed), len(all_items),
                    )
            except Exception:
                pass

        try:
            page.on("response", on_response)

            await page.goto(
                f"{MUSINSA_BASE}/categories/item/{category}",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            # 첫 페이지 로드 대기
            await asyncio.sleep(2)

            # 스크롤로 추가 페이지 로드
            scroll_attempts = 0
            max_scroll_attempts = max_pages * 3  # 안전장치
            while pages_collected < max_pages and scroll_attempts < max_scroll_attempts:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.2)
                scroll_attempts += 1

            logger.info(
                "카테고리 리스팅 완료: category=%s, %d페이지, %d건",
                category, pages_collected, len(all_items),
            )
            return all_items

        except Exception as e:
            logger.error("카테고리 리스팅 실패: category=%s error=%s", category, e)
            return all_items  # 이미 수집된 것이라도 반환
        finally:
            await page.close()

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 페이지에서 정보 수집 (모델번호, 할인가, 사이즈 재고).

        API 인터셉트를 사용하여 사이즈/재고 정보를 정확히 수집한다.
        """
        ctx = await self.connect()
        page = await ctx.new_page()

        # API 응답 캡처용 변수
        options_data: dict | None = None
        inventory_data: list | None = None

        async def on_response(resp: Response) -> None:
            nonlocal options_data, inventory_data
            try:
                url = resp.url
                # 순서 중요: inventory를 먼저 체크 (URL에 /options도 포함됨)
                if "prioritized-inventories" in url and resp.ok:
                    body = await resp.json()
                    if isinstance(body, dict):
                        inventory_data = body.get("data", [])
                    elif isinstance(body, list):
                        inventory_data = body
                    logger.debug("인터셉트 재고 캡처: url=%s", url[:100])
                elif (
                    re.search(r"/api2?/goods/\d+/options\b", url)
                    and "inventories" not in url
                    and resp.ok
                ):
                    body = await resp.json()
                    # 유효한 구조일 때만 저장
                    if isinstance(body, dict):
                        d = body.get("data")
                        if isinstance(d, dict) and isinstance(
                            d.get("basic"), list
                        ):
                            options_data = body
                            logger.debug(
                                "인터셉트 옵션 캡처: url=%s", url[:100],
                            )
            except Exception:
                pass

        page.on("response", on_response)

        try:
            url = f"{MUSINSA_BASE}/products/{product_id}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1)

            # 오프라인 전용 / 발매예정 상품 필터링
            page_text = await page.inner_text("body")
            # 1) "판매예정" / "출시예정" → 무조건 스킵
            if "판매예정" in page_text or "출시예정" in page_text:
                logger.info("발매예정 스킵: pid=%s", product_id)
                return None
            # 2) "오프라인 전용 상품" 정확한 문구 + 구매버튼 없음 → 스킵
            if "오프라인 전용 상품" in page_text:
                buy_btn = await page.query_selector(
                    "button:has-text('구매하기'), button:has-text('바로구매'), "
                    "button:has-text('장바구니')"
                )
                if not buy_btn:
                    logger.info("오프라인전용 스킵: pid=%s", product_id)
                    return None

            # 상품명 — GoodsName 컴포넌트 또는 전통적 셀렉터
            name = ""
            for sel in [
                "[class*='GoodsName']",
                "h2[class*='product_title']", ".product_title",
                "h3.product_item_name", "[class*='ProductName']",
            ]:
                el = await page.query_selector(sel)
                if el:
                    name = (await el.inner_text()).strip()
                    if name:
                        break

            if not name:
                name = await page.title()

            # 브랜드
            brand = ""
            for sel in [
                "[class*='BrandName']",
                "[class*='product_brand'] a", ".product_brand a",
            ]:
                el = await page.query_selector(sel)
                if el:
                    brand = (await el.inner_text()).strip()
                    if brand:
                        break

            # 모델번호 추출 — 핵심!
            model_number = await self._extract_model_number(page)

            # 가격 정보 — PriceWrap 컴포넌트 또는 전통적 셀렉터
            original_price, sale_price, discount_type, discount_rate = (
                await self._extract_prices(page)
            )

            # 이미지
            image_url = ""
            for sel in [
                ".product_img img", "[class*='ProductImage'] img",
                ".product-img img", "img[class*='swiper']",
            ]:
                img = await page.query_selector(sel)
                if img:
                    image_url = await img.get_attribute("src") or ""
                    if image_url:
                        break

            # ---- 사이즈별 재고: 3단계 폴백 ----
            sizes: list[RetailSizeInfo] = []

            # 직접 재고 API 호출 (인터셉트보다 신뢰성 높음)
            if not inventory_data:
                inventory_data = await self._fetch_inventories_api(
                    page, product_id,
                )

            # Tier 1: 직접 API 호출 (가장 신뢰성 높음)
            direct_options = await self._fetch_options_api(page, product_id)
            if direct_options:
                sizes = self._parse_sizes_from_api(
                    direct_options, inventory_data,
                    sale_price, original_price, discount_type, discount_rate,
                )
                if sizes:
                    logger.debug(
                        "Tier1(직접API) 사이즈 파싱 성공: pid=%s %d개",
                        product_id, len(sizes),
                    )

            # Tier 2: 브라우저 인터셉트 데이터
            if not sizes and options_data:
                sizes = self._parse_sizes_from_api(
                    options_data, inventory_data,
                    sale_price, original_price, discount_type, discount_rate,
                )
                if sizes:
                    logger.debug(
                        "Tier2(인터셉트) 사이즈 파싱 성공: pid=%s %d개",
                        product_id, len(sizes),
                    )

            # Tier 3: DOM 폴백 + 원인별 진단 로깅
            if not sizes:
                self._log_size_parse_failure(
                    product_id, direct_options, options_data,
                )
                sizes = await self._extract_sizes(
                    page, sale_price, original_price, discount_type, discount_rate,
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
                name, model_number, f"{sale_price:,}" if sale_price else "?", len(sizes),
            )
            return product

        except Exception as e:
            logger.error("무신사 상품 조회 실패 (%s): %s", product_id, e)
            return None
        finally:
            await page.close()

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

        # API 응답이 list로 오는 경우 처리
        if isinstance(options_data, list):
            if not options_data:
                return []
            options_data = options_data[0] if isinstance(options_data[0], dict) else {}

        data = options_data.get("data") if isinstance(options_data, dict) else None
        if not isinstance(data, dict):
            logger.debug("options_data.data가 dict가 아님: type=%s", type(data).__name__)
            return []
        basic_options = data.get("basic", [])
        if not isinstance(basic_options, list):
            logger.debug("basic_options가 list가 아님: type=%s", type(basic_options).__name__)
            return []

        # 사이즈 옵션 찾기 — standardOptionNo(3,4,6) 또는 이름 매칭
        _COLOR_NAMES = {"c", "color", "색상", "컬러"}
        _SIZE_NAMES = {"s", "size", "사이즈", "신발", "shoes"}
        _SKIP_NAMES = {"none", ""}

        size_option = None
        for opt in basic_options:
            if not isinstance(opt, dict):
                continue
            name_lower = (opt.get("name") or "").strip().lower()
            std_no = opt.get("standardOptionNo")

            # standardOptionNo 기반 판별 (3,4,6 = 사이즈)
            if std_no in (3, 4, 6):
                size_option = opt
                break
            # 이름 기반 판별
            if name_lower in _SIZE_NAMES:
                size_option = opt
                break

        # 옵션이 1개뿐이고 컬러/NONE이 아니면 사이즈로 간주
        if not size_option and len(basic_options) == 1:
            name_lower = (basic_options[0].get("name") or "").strip().lower()
            if name_lower not in _COLOR_NAMES and name_lower not in _SKIP_NAMES:
                size_option = basic_options[0]

        if not size_option:
            opt_names = [o.get("name") for o in basic_options]
            logger.debug("사이즈 옵션 매칭 실패: options=%s", opt_names)
            return []

        option_values = size_option.get("optionValues", [])
        if not isinstance(option_values, list):
            return []

        # optionItems에서 비활성(activated=False) 매핑 구축
        # 주의: activated=True는 재고 보장 아님! 실제 재고는 inventory에서 확인
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

        # optionItem.no → optionValueNos 역매핑 (productVariantId 폴백용)
        variant_to_values: dict[int, list[int]] = {}
        for item in option_items:
            if isinstance(item, dict):
                variant_to_values[item["no"]] = item.get("optionValueNos", [])

        # inventory_data에서 품절 매핑 구축
        inventory_stock: dict[int, bool] = {}
        if inventory_data and isinstance(inventory_data, list):
            for inv in inventory_data:
                if not isinstance(inv, dict):
                    continue
                in_stock = not inv.get("outOfStock", False)
                # 방법 1: relatedOption.optionValueNo로 매핑
                related = inv.get("relatedOption")
                if isinstance(related, dict):
                    val_no = related.get("optionValueNo")
                    if val_no is not None:
                        inventory_stock[val_no] = in_stock
                        continue
                # 방법 2: productVariantId → optionItem.no → optionValueNos
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

            # 재고 여부 판단:
            # 1) activated=False → 확정 품절
            # 2) inventory 데이터 있으면 → outOfStock 기준
            # 3) 둘 다 없으면 → isDeleted 폴백
            if ov_no in deactivated:
                in_stock = False
            elif inventory_stock:
                in_stock = inventory_stock.get(ov_no, False)
            elif inventory_data is not None:
                # inventory API 응답 왔지만 매핑 실패 or 빈 리스트 → 품절
                in_stock = False
            else:
                in_stock = not ov.get("isDeleted", False)

            # "재입고 알림" 상태인 옵션은 품절 처리
            if ov.get("isRestock") or "재입고" in size_name:
                in_stock = False

            if not in_stock:
                logger.debug("무신사 품절 사이즈 스킵: %s", size_name)
                continue

            sizes.append(RetailSizeInfo(
                size=size_name,
                price=sale_price,
                original_price=original_price,
                in_stock=in_stock,
                discount_type=discount_type,
                discount_rate=discount_rate,
            ))

        return sizes

    def _log_size_parse_failure(
        self,
        product_id: str,
        direct_options: dict | None,
        intercepted_options: dict | None,
    ) -> None:
        """사이즈 파싱 실패 시 원인별 진단 로그."""
        # 직접 API 데이터가 있으나 사이즈 0 → 전체 품절 가능성
        if direct_options:
            data = direct_options.get("data", {})
            basic = data.get("basic", []) if isinstance(data, dict) else []
            total_values = 0
            for opt in basic:
                if isinstance(opt, dict):
                    vals = opt.get("optionValues", [])
                    total_values += len(vals) if isinstance(vals, list) else 0
            if total_values > 0:
                logger.info(
                    "사이즈 전체 품절 (DOM 폴백): pid=%s (총 %d개 옵션 모두 품절/비활성)",
                    product_id, total_values,
                )
            else:
                logger.info(
                    "사이즈 옵션값 없음 (DOM 폴백): pid=%s", product_id,
                )
            return

        # 인터셉트 데이터만 있는 경우
        if intercepted_options:
            diag = _diagnose_options_data(intercepted_options)
            logger.info(
                "API 사이즈 파싱 0개 (DOM 폴백): pid=%s diag=%s",
                product_id, diag,
            )
            return

        # 두 소스 모두 데이터 없음
        logger.info(
            "API 옵션 데이터 미수신 (직접+인터셉트 모두 실패, DOM 폴백): pid=%s",
            product_id,
        )

    async def _fetch_options_api(
        self, page: Page, product_id: str,
    ) -> dict | None:
        """직접 API 호출로 옵션 데이터 가져오기.

        page.evaluate(fetch)를 사용하면 브라우저 쿠키/세션이 자동 포함된다.
        CORS 차단 시 Playwright APIRequestContext로 폴백.
        """
        api_url = (
            f"https://goods-detail.musinsa.com/api2/goods/{product_id}/options"
        )
        try:
            result = await page.evaluate(
                """async (url) => {
                    try {
                        const resp = await fetch(url, {
                            method: 'GET',
                            credentials: 'include',
                            headers: { 'Accept': 'application/json' }
                        });
                        if (!resp.ok) return { __error: resp.status };
                        return await resp.json();
                    } catch (e) {
                        return { __error: e.message };
                    }
                }""",
                api_url,
            )
        except Exception as e:
            logger.debug("옵션 API evaluate 실패: pid=%s %s", product_id, e)
            result = None

        # evaluate 실패 또는 CORS → Playwright APIRequestContext 폴백
        if result is None or (isinstance(result, dict) and "__error" in result):
            if isinstance(result, dict):
                logger.debug(
                    "옵션 API fetch 에러 (APIRequest 폴백): pid=%s error=%s",
                    product_id, result.get("__error"),
                )
            try:
                ctx = page.context
                resp = await ctx.request.get(api_url)
                if resp.ok:
                    result = await resp.json()
                else:
                    logger.debug(
                        "옵션 APIRequest 실패: pid=%s status=%s",
                        product_id, resp.status,
                    )
                    return None
            except Exception as e:
                logger.debug("옵션 APIRequest 예외: pid=%s %s", product_id, e)
                return None

        # 구조 검증
        if not isinstance(result, dict):
            return None
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("basic"), list):
            logger.debug(
                "옵션 직접 API 성공: pid=%s basic=%d개",
                product_id, len(data["basic"]),
            )
            return result
        logger.debug(
            "옵션 직접 API 구조 비정상: pid=%s keys=%s",
            product_id, list(result.keys())[:5],
        )
        return None

    async def _fetch_inventories_api(
        self, page: Page, product_id: str,
    ) -> list | None:
        """직접 API 호출로 재고 데이터 가져오기.

        prioritized-inventories 엔드포인트는 인증 필요 → 브라우저 컨텍스트 사용.
        """
        # v2 URL 우선, 실패 시 구 URL 폴백
        urls = [
            f"https://goods-detail.musinsa.com/api2/goods/"
            f"{product_id}/options/v2/prioritized-inventories",
            f"https://goods-detail.musinsa.com/api2/goods/"
            f"{product_id}/prioritized-inventories",
        ]
        try:
            ctx = page.context
            resp = None
            for api_url in urls:
                resp = await ctx.request.get(api_url)
                if resp.ok:
                    break
                logger.debug(
                    "재고 API 시도 실패: pid=%s url=%s status=%s",
                    product_id, api_url.split("/")[-1], resp.status,
                )
            if not resp or not resp.ok:
                return None
            body = await resp.json()
            if isinstance(body, dict):
                data = body.get("data", [])
                if isinstance(data, list) and data:
                    logger.debug(
                        "재고 직접 API 성공: pid=%s %d개", product_id, len(data),
                    )
                    return data
            elif isinstance(body, list):
                return body
        except Exception as e:
            logger.debug("재고 API 예외: pid=%s %s", product_id, e)
        return None

    @staticmethod
    def _is_musinsa_sku(model: str) -> bool:
        """무신사 자체 SKU인지 판별.

        무신사 SKU 패턴: 브랜드접두사 + 언더스코어/숫자 조합
        예: NBPDGS111G_15, SXCR2405_BK, APA5FS14_BK55
        크림 모델번호와 달리 언더스코어를 포함하거나, 영문4자리+숫자 형태가 아님.
        """
        if not model:
            return False
        # 언더스코어 포함 → 무신사 SKU일 가능성 높음
        if "_" in model:
            return True
        # 전형적 크림 모델번호 패턴이면 SKU가 아님
        upper = model.upper()
        # Nike/Jordan: XX1234-123, Adidas: AB1234, NB: M990XX / U7408PL
        if re.match(r"^[A-Z]{1,3}\d{3,5}[-]\d{2,4}$", upper):
            return False
        if re.match(r"^[A-Z]{1,2}\d{3,5}[A-Z]{0,3}$", upper):
            return False
        # 6자리 이상 영문+숫자 혼합이면서 하이픈 없으면 SKU 의심
        if len(model) >= 8 and not "-" in model and re.match(r"^[A-Z]{2,}", upper):
            return True
        return False

    async def _extract_model_number(self, page: Page) -> str:
        """상세 페이지에서 모델번호 추출."""
        # 1. 상품 상세 정보 테이블에서 찾기
        table_model = ""
        for sel in [
            "//th[contains(text(), '품번')]/following-sibling::td",
            "//th[contains(text(), '모델')]/following-sibling::td",
            "//dt[contains(text(), '품번')]/following-sibling::dd",
            "//dt[contains(text(), '모델')]/following-sibling::dd",
        ]:
            try:
                el = await page.query_selector(f"xpath={sel}")
                if el:
                    text = (await el.inner_text()).strip()
                    if text and text != "-":
                        table_model = text
                        break
            except Exception:
                continue

        # 2. data attribute에서 찾기
        if not table_model:
            for sel in [
                "[data-model-number]", "[data-product-code]",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        val = await el.get_attribute("data-model-number")
                        if not val:
                            val = await el.get_attribute("data-product-code")
                        if val:
                            table_model = val.strip()
                            break
                except Exception:
                    continue

        # 3. 상품명에서 모델번호 패턴 추출
        # 예: "덩크 로우 W - 세일:화이트 / IO4244-100"
        # 예: "뉴발란스 574 레거시 U7408PL"
        name_model = ""
        try:
            name_el = await page.query_selector("[class*='GoodsName']")
            if name_el:
                name_text = (await name_el.inner_text()).strip()
                # "/" 뒤의 모델번호
                m = re.search(r"/\s*([A-Za-z0-9][-A-Za-z0-9]+)", name_text)
                if m:
                    candidate = m.group(1).strip().upper()
                    if re.match(r"[A-Z]{1,3}\d{3,5}[-\s]?\d{2,4}", candidate):
                        name_model = candidate
                # 상품명 내 품번 패턴 (공백/하이픈 구분)
                if not name_model:
                    # Nike/Jordan: XX1234-123
                    m = re.search(r"\b([A-Z]{1,3}\d{3,5}-\d{2,4})\b", name_text.upper())
                    if m:
                        name_model = m.group(1)
                if not name_model:
                    # NB/기타: 영문1~2자+숫자3~4자+영문0~3자 (U7408PL, MT410GC5, BB550)
                    m = re.search(
                        r"\b([A-Z]{1,2}\d{3,5}[A-Z]{0,3}\d{0,2})\b", name_text.upper(),
                    )
                    if m:
                        candidate = m.group(1)
                        # 너무 짧은(4자 이하) 것은 제외
                        if len(candidate) >= 5:
                            name_model = candidate
        except Exception:
            pass

        # table_model이 무신사 SKU면 name_model 우선, 아니면 table_model 우선
        if table_model and not self._is_musinsa_sku(table_model):
            return table_model
        if name_model:
            if table_model:
                logger.debug(
                    "무신사 SKU→상품명 품번 대체: %s → %s", table_model, name_model,
                )
            return name_model
        if table_model:
            # SKU라도 없는 것보다 낫다
            return table_model

        # 4. 페이지 HTML에서 정규식으로 추출
        try:
            content = await page.content()
            patterns = [
                r"[A-Z]{1,3}\d{3,5}[-\s]?\d{2,4}",  # DQ8423-100, FJ4188-100
                r"\d{6}[-\s]?\d{3}",  # adidas: 123456-001
                r"[A-Z]{1,2}\d{3,5}[A-Z]{0,3}",  # NB: BB550, U7408PL
            ]
            for pattern in patterns:
                m = re.search(pattern, content)
                if m:
                    return m.group(0).strip()
        except Exception:
            pass

        # 5. 페이지 전체에서 "품번" 근처 텍스트 찾기
        try:
            all_text = await page.inner_text("body")
            m = re.search(r"품번[:\s]*([A-Za-z0-9\-]+)", all_text)
            if m:
                return m.group(1).strip()
        except Exception:
            pass

        return ""

    async def _extract_prices(self, page: Page) -> tuple[int, int, str, float]:
        """가격 정보 추출.

        Returns:
            (original_price, sale_price, discount_type, discount_rate)
        """
        original_price = 0
        sale_price = 0
        discount_type = ""
        discount_rate = 0.0

        # 새 무신사 UI: PriceWrap/PriceTitle 컴포넌트
        price_wrap = await page.query_selector("[class*='PriceWrap'], [class*='PriceTotalWrap']")
        if price_wrap:
            text = (await price_wrap.inner_text()).strip()
            # 여러 가격이 있을 수 있음 (정가, 할인율, 할인가)
            prices = re.findall(r"(\d[\d,]+)원", text)
            if prices:
                parsed = [int(p.replace(",", "")) for p in prices]
                if len(parsed) >= 2:
                    original_price = parsed[0]
                    sale_price = parsed[-1]
                elif len(parsed) == 1:
                    sale_price = parsed[0]
                    original_price = sale_price

            # 할인율 추출
            rate_match = re.search(r"(\d+)%", text)
            if rate_match and original_price and sale_price and original_price != sale_price:
                discount_rate = int(rate_match.group(1)) / 100
                discount_type = "할인"

            if original_price and sale_price and not discount_rate and original_price > sale_price:
                discount_rate = round(1 - sale_price / original_price, 3)
                discount_type = "할인"

            if sale_price:
                return original_price, sale_price, discount_type, discount_rate

        # 레거시 셀렉터 폴백
        for sel in [
            "[class*='originalPrice']", "[class*='origin-price']",
            ".product_article_price .discount_price .original_price",
            "del", "s",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                parsed = self._parse_price(text)
                if parsed:
                    original_price = parsed
                    break

        for sel in [
            "[class*='memberPrice']", "[class*='member-price']",
            "[class*='salePrice']", "[class*='sale-price']",
            ".product_article_price .txt_price_area",
            "[class*='FinalPrice']", "[class*='final-price']",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                parsed = self._parse_price(text)
                if parsed:
                    sale_price = parsed
                    break

        if not discount_type and sale_price and original_price and sale_price < original_price:
            discount_type = "할인"

        if original_price and sale_price and original_price > 0:
            discount_rate = round(1 - sale_price / original_price, 3)

        if not sale_price and original_price:
            sale_price = original_price

        return original_price, sale_price, discount_type, discount_rate

    async def _extract_sizes(
        self,
        page: Page,
        sale_price: int,
        original_price: int,
        discount_type: str,
        discount_rate: float,
    ) -> list[RetailSizeInfo]:
        """사이즈 옵션 및 재고 추출 (DOM 파싱 폴백)."""
        sizes: list[RetailSizeInfo] = []

        # 방법 0: 현재 무신사 React UI — 옵션 드롭다운 트리거 후 항목 수집
        try:
            for trigger_sel in [
                "[class*='OptionItem']", "[class*='option_select']",
                "[class*='FilterOption']", "button:has-text('사이즈')",
                "[class*='ProductOption']",
            ]:
                trigger = await page.query_selector(trigger_sel)
                if trigger:
                    await trigger.click()
                    await asyncio.sleep(0.5)
                    break

            for item_sel in [
                "[role='option']", "[role='listbox'] li",
                "[class*='OptionValue']", "[class*='option_value']",
                "[class*='FilterValue']", "li[class*='option']",
            ]:
                items = await page.query_selector_all(item_sel)
                if not items or len(items) < 2:
                    continue
                for item in items:
                    text = (await item.inner_text()).strip()
                    if not text or len(text) > 15:
                        continue
                    if text in ("사이즈 선택", "선택", ""):
                        continue
                    classes = await item.get_attribute("class") or ""
                    aria_disabled = await item.get_attribute("aria-disabled")
                    in_stock = (
                        "sold" not in classes.lower()
                        and "disabled" not in classes.lower()
                        and aria_disabled != "true"
                        and "품절" not in text
                        and "재입고" not in text
                    )
                    size_text = re.sub(r"\s*[\(\[].*?[\)\]]", "", text).strip()
                    if not in_stock:
                        continue
                    sizes.append(RetailSizeInfo(
                        size=size_text,
                        price=sale_price,
                        original_price=original_price,
                        in_stock=True,
                        discount_type=discount_type,
                        discount_rate=discount_rate,
                    ))
                if sizes:
                    logger.debug("DOM 방법0(React UI) 사이즈 %d개", len(sizes))
                    return sizes
        except Exception:
            pass

        # 방법 1: select 태그
        select = await page.query_selector(
            "select[name*='size'], select[id*='size'], select.option_size"
        )
        if select:
            options = await select.query_selector_all("option")
            for opt in options:
                value = await opt.get_attribute("value")
                text = (await opt.inner_text()).strip()
                disabled = await opt.get_attribute("disabled")

                if not value or value == "" or text in ("사이즈 선택", "선택", ""):
                    continue

                in_stock = (
                    disabled is None
                    and "품절" not in text
                    and "재입고" not in text
                    and "sold" not in text.lower()
                )
                size_text = re.sub(r"\s*\(.*?\)", "", text).strip()

                if not in_stock:
                    logger.debug("무신사 품절 사이즈 스킵 (select): %s", size_text)
                    continue

                sizes.append(RetailSizeInfo(
                    size=size_text,
                    price=sale_price,
                    original_price=original_price,
                    in_stock=in_stock,
                    discount_type=discount_type,
                    discount_rate=discount_rate,
                ))
            if sizes:
                return sizes

        # 방법 2: 사이즈 버튼
        for btn_sel in [
            "button[class*='size']", "[class*='SizeItem']",
            ".option_size button", ".size_list button",
            "[class*='chip']",
        ]:
            buttons = await page.query_selector_all(btn_sel)
            if not buttons:
                continue

            for btn in buttons:
                try:
                    text = (await btn.inner_text()).strip()
                    if not text or len(text) > 10:
                        continue

                    classes = await btn.get_attribute("class") or ""
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    in_stock = (
                        disabled is None
                        and aria_disabled != "true"
                        and "sold" not in classes.lower()
                        and "disabled" not in classes.lower()
                        and "품절" not in text
                        and "재입고" not in text
                    )

                    size_text = re.sub(r"\s*\(.*?\)", "", text).strip()

                    if not in_stock:
                        logger.debug("무신사 품절 사이즈 스킵 (button): %s", size_text)
                        continue

                    sizes.append(RetailSizeInfo(
                        size=size_text,
                        price=sale_price,
                        original_price=original_price,
                        in_stock=in_stock,
                        discount_type=discount_type,
                        discount_rate=discount_rate,
                    ))
                except Exception:
                    continue

            if sizes:
                break

        return sizes

    async def disconnect(self) -> None:
        """브라우저 종료."""
        if self._context:
            await self.save_session()
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._connected = False
        logger.info("무신사 크롤러 연결 해제")

    @staticmethod
    def _parse_price(text: str) -> int | None:
        if not text:
            return None
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else None


# 싱글톤
musinsa_crawler = MusinsaCrawler()
