"""NetFunnel 쿠키 획득 헬퍼 — asics.co.kr 대기열 우회.

배경
----
아식스 한국몰 ``www.asics.co.kr`` 은 NetFunnel 트래픽 대기열을 입구에 건
SSR 기반 사이트로, 첫 방문은 3~5KB 스켈레톤 HTML + ``NetFunnel_Action``
JS 블록만 반환한다. 해당 JS 가 브라우저에서 실행되어 큐를 통과하면 쿠키
``NetFunnel_ID_act_1`` 가 세팅되고, 이후 요청이 정상 SSR 페이지로 해석된다.

``AsicsCrawler`` 는 쿠키 수급을 스스로 하지 않고 외부 헬퍼 주입을 받는다
(``attach_netfunnel_cookie``). 본 모듈은 Playwright headless 로 홈 페이지에
진입해 NetFunnel JS 를 실행시킨 뒤, 발급된 쿠키 값을 회수해 캐시한다.

설계 포인트
-----------
* 쿠키 1개만 필요 — ``NetFunnel_ID_act_1`` 값(텍스트). 다른 쿠키 무시.
* TTL 기반 캐시 — 한 번 획득한 값은 ``ttl_seconds`` 동안 재사용. 실운영에서는
  크롤러가 스켈레톤 감지 시 ``invalidate()`` 호출 → 다음 호출에서 재획득.
* 적응형 TTL(EMA) — 실효 TTL 을 관측하면 지수이동평균으로 보정 (upper clamp).
* cooldown — 연속 실패 시 최소 간격 보장 (차단 리스크 축소).
* Playwright 종속성은 함수 레벨 지연 import — 테스트에서 mock 주입 가능.

Read-only · 홈 진입 GET only · POST 금지.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ---- 설정 기본값 ---------------------------------------------------------

DEFAULT_HOME_URL = "https://www.asics.co.kr/main/index"
DEFAULT_COOKIE_NAME = "NetFunnel_ID_act_1"
# NetFunnel 서버측 토큰 TTL 은 쿠키 값 내부에 ``ttl=15`` 로 고정되어 있고
# 실측(2026-04-14)상 단일 사용에 가까운 one-shot 에 가깝다. 초기 TTL 은
# 보수적으로 10초로 짧게 잡고, EMA 관측으로 서버가 더 관대하면 점진 상향.
DEFAULT_TTL_SECONDS = 10.0
DEFAULT_TTL_MIN = 5.0
DEFAULT_TTL_MAX = 60.0
DEFAULT_COOLDOWN_SECONDS = 15.0      # 연속 실패 사이 최소 간격
DEFAULT_TIMEOUT_MS = 20_000
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)


# Playwright 의존성 주입용 타입 — 실Playwright 대신 테스트 mock 주입 가능.
# 시그니처: (home_url, cookie_name, user_agent, timeout_ms) -> Optional[str]
CookieFetcher = Callable[[str, str, str, int], Awaitable[Optional[str]]]


async def _default_playwright_fetch(
    home_url: str,
    cookie_name: str,
    user_agent: str,
    timeout_ms: int,
) -> Optional[str]:
    """Playwright headless 로 NetFunnel 쿠키 획득 — 기본 구현.

    실패 시 ``None`` 을 반환한다(예외 throw 하지 않음 — 호출자가 cooldown 등
    정책을 일관되게 처리하도록).
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("playwright import 실패: %s", exc)
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=user_agent)
                page = await ctx.new_page()
                # 도메인 진입 — NetFunnel JS 가 큐를 통과시키도록 대기
                await page.goto(home_url, timeout=timeout_ms, wait_until="networkidle")
                # 일부 경우 1~2초 사이 쿠키 세팅 — 짧게 더 대기
                await page.wait_for_timeout(1500)
                cookies = await ctx.cookies()
                for c in cookies:
                    if c.get("name") == cookie_name and c.get("value"):
                        return str(c["value"])
                logger.info("netfunnel 쿠키 미발견 (진입 OK, 쿠키 set 안됨)")
                return None
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning("netfunnel playwright 획득 실패: %s", exc)
        return None


@dataclass
class NetFunnelCookieCache:
    """TTL + cooldown + EMA 기반 NetFunnel 쿠키 캐시.

    - ``get()`` 이 유효 쿠키를 반환하면 그대로 사용
    - 없거나 TTL 초과면 ``_fetcher`` 로 재획득 시도
    - 실패 직후에는 ``cooldown_seconds`` 동안 재시도를 보류

    동시성 — 단일 asyncio.Lock 으로 재획득 싱글플라이트 보장.
    """

    home_url: str = DEFAULT_HOME_URL
    cookie_name: str = DEFAULT_COOKIE_NAME
    user_agent: str = DEFAULT_USER_AGENT
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    ttl_min: float = DEFAULT_TTL_MIN
    ttl_max: float = DEFAULT_TTL_MAX
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    fetcher: CookieFetcher = field(default=_default_playwright_fetch)
    _now: Callable[[], float] = field(default=time.monotonic)

    _value: Optional[str] = field(default=None, init=False)
    _acquired_at: float = field(default=0.0, init=False)
    _last_failure_at: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _ema_ttl: float = field(default=0.0, init=False)

    def _is_fresh(self) -> bool:
        if not self._value:
            return False
        return (self._now() - self._acquired_at) < self.ttl_seconds

    def invalidate(self) -> None:
        """현재 캐시된 쿠키를 무효화한다.

        크롤러가 스켈레톤 응답을 감지했을 때 호출하면 다음 ``get()`` 이
        재획득을 시도한다. 실효 TTL(이번 값이 방금 전까지 유효했던 시간)을
        EMA 로 반영해 ``ttl_seconds`` 를 점진 보정한다.
        """
        if self._value and self._acquired_at > 0:
            lived = max(0.0, self._now() - self._acquired_at)
            # α=0.3 EMA — 새 관측이 30% 반영
            if self._ema_ttl == 0.0:
                self._ema_ttl = lived
            else:
                self._ema_ttl = 0.7 * self._ema_ttl + 0.3 * lived
            # 관측치 기반 TTL 보정 — 안전 마진 90% + clamp
            candidate = max(self.ttl_min, min(self.ttl_max, self._ema_ttl * 0.9))
            self.ttl_seconds = candidate
        self._value = None
        self._acquired_at = 0.0

    async def get(self) -> Optional[str]:
        """유효한 NetFunnel 쿠키 값을 반환. 없거나 만료면 재획득 시도.

        쿨다운 중이면 바로 None 을 반환한다(즉시 스킵, 지수 백오프 없음).
        """
        if self._is_fresh():
            return self._value

        async with self._lock:
            # 더블 체크 — 락 대기 중 다른 호출이 값 세팅했을 수 있음
            if self._is_fresh():
                return self._value

            # 쿨다운 중이면 재시도 보류
            if self._last_failure_at > 0.0:
                elapsed = self._now() - self._last_failure_at
                if elapsed < self.cooldown_seconds:
                    return None

            value = await self.fetcher(
                self.home_url,
                self.cookie_name,
                self.user_agent,
                self.timeout_ms,
            )
            if value:
                self._value = value
                self._acquired_at = self._now()
                self._last_failure_at = 0.0
                logger.info(
                    "netfunnel 쿠키 획득 성공: ttl=%.0fs ema=%.0fs",
                    self.ttl_seconds,
                    self._ema_ttl,
                )
                return value

            self._last_failure_at = self._now()
            logger.info(
                "netfunnel 쿠키 획득 실패 — cooldown %.0fs 진입",
                self.cooldown_seconds,
            )
            return None


async def acquire_netfunnel_cookie(
    *,
    home_url: str = DEFAULT_HOME_URL,
    cookie_name: str = DEFAULT_COOKIE_NAME,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> Optional[str]:
    """단발 호출 — 캐시 없이 즉시 한 번 Playwright 로 쿠키 획득.

    스크립트/수동 테스트 편의용. 실운영은 ``NetFunnelCookieCache`` 사용.
    """
    return await _default_playwright_fetch(
        home_url, cookie_name, user_agent, timeout_ms
    )


__all__ = [
    "CookieFetcher",
    "DEFAULT_COOKIE_NAME",
    "DEFAULT_HOME_URL",
    "NetFunnelCookieCache",
    "acquire_netfunnel_cookie",
]
