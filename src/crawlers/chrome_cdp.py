"""Chrome CDP 연결 매니저.

Windows에 설치된 Chrome을 원격 디버깅 모드로 실행하고,
Playwright가 CDP로 연결하여 조종하는 방식.
크롬 인스턴스는 하나만 유지하고 탭을 재사용한다.
"""

import asyncio
import subprocess

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.config import settings
from src.utils.logging import setup_logger

logger = setup_logger("chrome_cdp")


class ChromeCDPManager:
    """Chrome CDP 연결 관리."""

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._chrome_process: subprocess.Popen | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._browser is not None

    async def start_chrome(self) -> None:
        """Windows Chrome을 디버그 모드로 실행.

        이미 실행 중인 디버그 크롬이 있으면 재사용한다.
        일반 크롬 브라우저와 분리하기 위해 별도 user-data-dir을 사용한다.
        """
        # 이미 디버그 포트가 열려있는지 확인
        if await self._is_debug_port_open():
            logger.info("디버그 크롬이 이미 실행 중 (포트 %d)", settings.chrome_debug_port)
            return

        chrome_path = settings.chrome_path
        user_data_dir = settings.chrome_user_data_dir
        port = settings.chrome_debug_port

        # WSL에서 Windows Chrome 실행
        cmd = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--window-position=-2400,-2400",  # 화면 밖에 숨김
            "--no-first-run",
            "--no-default-browser-check",
        ]

        logger.info("Chrome 디버그 모드 시작: 포트 %d", port)
        try:
            self._chrome_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Chrome이 완전히 시작될 때까지 대기
            for attempt in range(15):
                await asyncio.sleep(1)
                if await self._is_debug_port_open():
                    logger.info("Chrome 시작 완료 (%.0f초)", attempt + 1)
                    return
            raise TimeoutError("Chrome 시작 시간 초과 (15초)")
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Chrome을 찾을 수 없습니다: {chrome_path}\n"
                ".env 파일의 CHROME_PATH를 확인하세요."
            )

    async def _is_debug_port_open(self) -> bool:
        """디버그 포트가 열려있는지 확인."""
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", settings.chrome_debug_port
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (ConnectionRefusedError, OSError):
            return False

    async def connect(self) -> BrowserContext:
        """Playwright를 통해 CDP로 Chrome에 연결."""
        if self._connected and self._context:
            return self._context

        # Chrome이 실행 중인지 확인하고, 아니면 시작
        if not await self._is_debug_port_open():
            await self.start_chrome()

        cdp_url = f"http://127.0.0.1:{settings.chrome_debug_port}"
        logger.info("CDP 연결 중: %s", cdp_url)

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)

        # 기본 컨텍스트 사용 (로그인 세션 유지를 위해)
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
        else:
            self._context = await self._browser.new_context()

        self._connected = True
        logger.info(
            "CDP 연결 성공 (컨텍스트: %d개, 페이지: %d개)",
            len(self._browser.contexts),
            len(self._context.pages),
        )
        return self._context

    async def get_page(self, url: str | None = None) -> Page:
        """페이지를 가져오거나 새로 생성. 탭 재사용."""
        ctx = await self.connect()

        # 빈 탭이 있으면 재사용
        for page in ctx.pages:
            if page.url in ("about:blank", "chrome://newtab/"):
                if url:
                    await page.goto(url, wait_until="domcontentloaded")
                return page

        # 없으면 새 탭
        page = await ctx.new_page()
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        return page

    async def close_extra_tabs(self, keep: int = 2) -> None:
        """불필요한 탭 정리. keep개만 유지."""
        if not self._context:
            return
        pages = self._context.pages
        if len(pages) > keep:
            for page in pages[keep:]:
                await page.close()
            logger.info("탭 정리: %d → %d", len(pages), keep)

    async def disconnect(self) -> None:
        """Playwright 연결 해제 (Chrome은 계속 실행)."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._context = None
        self._connected = False
        logger.info("CDP 연결 해제")

    async def restart_chrome(self) -> BrowserContext:
        """Chrome 프로세스 재시작 (크롬이 완전히 죽었을 때)."""
        logger.warning("Chrome 재시작 시도")
        await self.disconnect()
        await self._kill_debug_chrome()
        await asyncio.sleep(2)
        await self.start_chrome()
        return await self.connect()

    async def _kill_debug_chrome(self) -> None:
        """디버그 모드 Chrome만 종료 (일반 Chrome은 건드리지 않음)."""
        try:
            # Windows의 디버그 Chrome 프로세스만 종료
            subprocess.run(
                [
                    "powershell.exe",
                    "-Command",
                    f"Get-Process chrome | Where-Object {{$_.CommandLine -like '*--remote-debugging-port={settings.chrome_debug_port}*'}} | Stop-Process -Force",
                ],
                capture_output=True,
                timeout=10,
            )
            logger.info("디버그 Chrome 프로세스 종료 완료")
        except Exception as e:
            logger.warning("디버그 Chrome 종료 실패: %s", e)

    async def check_login_status(self, site: str = "kream") -> bool:
        """로그인 상태 확인."""
        page = await self.get_page()
        try:
            if site == "kream":
                await page.goto(
                    "https://kream.co.kr/my", wait_until="domcontentloaded", timeout=15000
                )
                # 로그인되어 있으면 마이페이지가 뜸, 아니면 로그인 페이지로 리다이렉트
                await page.wait_for_load_state("networkidle", timeout=5000)
                current_url = page.url
                is_logged_in = "/login" not in current_url
                logger.info("크림 로그인 상태: %s", "로그인됨" if is_logged_in else "로그아웃")
                return is_logged_in
        except Exception as e:
            logger.error("로그인 상태 확인 실패: %s", e)
            return False

    async def kream_login(self, email: str, password: str) -> bool:
        """CDP 브라우저로 크림 로그인 자동화.

        이미 로그인된 상태면 바로 True 반환.
        로그인 성공 시 브라우저 컨텍스트에 쿠키가 유지된다.
        """
        # 1. 기존 로그인 확인
        if await self.check_login_status("kream"):
            return True

        if not email or not password:
            logger.error("크림 로그인 정보 없음 (KREAM_EMAIL / KREAM_PASSWORD)")
            return False

        logger.info("크림 로그인 시도: %s", email)
        page = await self.get_page()

        try:
            # 2. 로그인 페이지 이동
            await page.goto(
                "https://kream.co.kr/login", wait_until="domcontentloaded", timeout=20000
            )
            await page.wait_for_load_state("networkidle", timeout=10000)

            # 3. 이메일 입력
            email_input = page.locator('input[type="email"], input[placeholder*="이메일"]')
            if await email_input.count() == 0:
                # 대체 선택자
                email_input = page.locator('input[name="email"], input[autocomplete="email"]')
            if await email_input.count() == 0:
                # 더 일반적: 첫 번째 텍스트 입력
                email_input = page.locator('input[type="text"]').first
            await email_input.click()
            await email_input.fill(email)
            await asyncio.sleep(0.5)

            # 4. 비밀번호 입력
            pw_input = page.locator('input[type="password"]')
            await pw_input.click()
            await pw_input.fill(password)
            await asyncio.sleep(0.5)

            # 5. 로그인 버튼 클릭
            login_btn = page.locator(
                'button:has-text("로그인"), '
                'button[type="submit"], '
                'a:has-text("로그인")'
            ).first
            await login_btn.click()

            # 6. 로그인 결과 대기 (URL 변경 또는 에러 메시지)
            try:
                await page.wait_for_url(
                    lambda url: "/login" not in url, timeout=15000
                )
            except Exception:
                # URL이 안 바뀌면 에러 메시지 확인
                error_el = page.locator('.error_text, .login_error, [class*="error"]')
                if await error_el.count() > 0:
                    error_text = await error_el.first.text_content()
                    logger.error("크림 로그인 실패: %s", error_text)
                    return False
                logger.warning("크림 로그인 URL 변경 대기 타임아웃")

            await asyncio.sleep(1)

            # 7. 최종 로그인 확인
            current_url = page.url
            if "/login" in current_url:
                logger.error("크림 로그인 실패: 여전히 로그인 페이지")
                return False

            logger.info("크림 로그인 성공: %s", email)
            return True

        except Exception as e:
            logger.error("크림 로그인 자동화 실패: %s", e)
            return False

    async def get_kream_cookies(self) -> list[dict]:
        """크림 도메인의 브라우저 쿠키를 dict 리스트로 반환.

        Returns:
            [{"name": "...", "value": "...", "domain": "...", ...}, ...]
        """
        if not self._context:
            return []
        try:
            all_cookies = await self._context.cookies("https://kream.co.kr")
            kream_cookies = [
                c for c in all_cookies
                if "kream" in (c.get("domain", "") or "")
            ]
            logger.info("크림 쿠키 %d개 추출", len(kream_cookies))
            return kream_cookies
        except Exception as e:
            logger.error("크림 쿠키 추출 실패: %s", e)
            return []


# 싱글톤 인스턴스
cdp_manager = ChromeCDPManager()
