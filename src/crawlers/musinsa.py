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

    async def search_products(self, keyword: str) -> list[dict]:
        """무신사에서 키워드로 상품 검색.

        Returns:
            [{"product_id": str, "name": str, "brand": str, "url": str, "price": int}, ...]
        """
        ctx = await self.connect()
        page = await ctx.new_page()

        try:
            search_url = f"{MUSINSA_BASE}/search/goods?keyword={keyword}&category=shoes"
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
                if "/options?" in url and "optKindCd" in url and resp.ok:
                    options_data = await resp.json()
                elif "prioritized-inventories" in url and resp.ok:
                    body = await resp.json()
                    inventory_data = body.get("data", [])
            except Exception:
                pass

        page.on("response", on_response)

        try:
            url = f"{MUSINSA_BASE}/products/{product_id}"
            await page.goto(url, wait_until="networkidle", timeout=20000)
            await asyncio.sleep(1)

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

            # 사이즈별 재고 — API 데이터 우선, 폴백으로 DOM 파싱
            sizes = self._parse_sizes_from_api(
                options_data, inventory_data,
                sale_price, original_price, discount_type, discount_rate,
            )
            if not sizes:
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

        data = options_data.get("data", {})
        basic_options = data.get("basic", [])

        # 사이즈 옵션 찾기
        size_option = None
        for opt in basic_options:
            if opt.get("name") in ("사이즈", "size"):
                size_option = opt
                break

        if not size_option:
            return []

        option_values = size_option.get("optionValues", [])

        # 재고 정보 맵 (variantId -> outOfStock)
        stock_map: dict[int, bool] = {}
        if inventory_data:
            for inv in inventory_data:
                vid = inv.get("productVariantId")
                out = inv.get("outOfStock", False)
                if vid is not None:
                    stock_map[vid] = not out

        sizes: list[RetailSizeInfo] = []
        for ov in option_values:
            size_name = ov.get("name", "")
            if not size_name:
                continue

            # 재고 여부: inventory 데이터가 있으면 그것으로, 없으면 재고 있다고 가정
            in_stock = True
            if stock_map:
                # variant와 option value 매핑은 순서 기반
                ov_no = ov.get("no")
                # inventory에서 해당 옵션 찾기
                for inv in (inventory_data or []):
                    related = inv.get("relatedOption")
                    if related and related.get("optionValueNo") == ov_no:
                        in_stock = not inv.get("outOfStock", False)
                        break
                else:
                    # 매핑 못 찾으면 deleted 여부로 판단
                    in_stock = not ov.get("isDeleted", False)

            sizes.append(RetailSizeInfo(
                size=size_name,
                price=sale_price,
                original_price=original_price,
                in_stock=in_stock,
                discount_type=discount_type,
                discount_rate=discount_rate,
            ))

        return sizes

    async def _extract_model_number(self, page: Page) -> str:
        """상세 페이지에서 모델번호 추출."""
        # 1. 상품 상세 정보 테이블에서 찾기
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
                        return text
            except Exception:
                continue

        # 2. data attribute에서 찾기
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
                        return val.strip()
            except Exception:
                continue

        # 3. 상품명에서 모델번호 패턴 추출 (무신사는 상품명에 모델번호를 포함시킴)
        # 예: "덩크 로우 W - 세일:화이트:메탈릭 골드:펄 핑크 / IO4244-100"
        try:
            name_el = await page.query_selector("[class*='GoodsName']")
            if name_el:
                name_text = (await name_el.inner_text()).strip()
                # "/" 뒤의 모델번호 패턴
                m = re.search(r"/\s*([A-Za-z0-9][-A-Za-z0-9]+)", name_text)
                if m:
                    candidate = m.group(1).strip()
                    if re.match(r"[A-Z]{1,3}\d{3,5}[-\s]?\d{2,4}", candidate.upper()):
                        return candidate.upper()
        except Exception:
            pass

        # 4. 페이지 HTML에서 정규식으로 추출
        try:
            content = await page.content()
            patterns = [
                r"[A-Z]{1,3}\d{3,5}[-\s]?\d{2,4}",  # DQ8423-100, FJ4188-100
                r"\d{6}[-\s]?\d{3}",  # adidas: 123456-001
                r"[A-Z]{2}\d{4}",  # NB: BB550
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

                in_stock = disabled is None and "품절" not in text and "sold" not in text.lower()
                size_text = re.sub(r"\s*\(.*?\)", "", text).strip()

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
                    )

                    size_text = re.sub(r"\s*\(.*?\)", "", text).strip()
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
