"""AsicsCrawler 스켈레톤 콜백 단위 테스트.

``_fetch`` 가 NetFunnel 스켈레톤을 감지했을 때 등록된 콜백을 호출하고
빈 문자열을 반환하는지 검증한다. 실HTTP 호출 없음 — ``_fetch`` 내부의
rate_limiter/client get 을 monkeypatch.
"""

from __future__ import annotations

import pytest

from src.crawlers.asics import AsicsCrawler


class _FakeResp:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status


class _FakeClient:
    def __init__(self, resp: _FakeResp | list[_FakeResp]) -> None:
        # 단일 응답이면 고정 반환, 리스트면 호출마다 순차 소비(retry 검증용).
        self._responses = resp if isinstance(resp, list) else None
        self._resp = resp if not isinstance(resp, list) else None
        self.get_calls: list[str] = []

    async def get(self, url: str):
        self.get_calls.append(url)
        if self._responses is not None:
            idx = min(len(self.get_calls) - 1, len(self._responses) - 1)
            return self._responses[idx]
        return self._resp


class _NoopLimiter:
    def acquire(self):
        limiter = self

        class _Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False

        return _Ctx()


SKELETON_HTML = "<html>" + "x" * 500 + "NetFunnel_Action();" + "y" * 500 + "</html>"
REAL_HTML = "<html>" + "a" * 7000 + "</html>"


@pytest.mark.asyncio
async def test_skeleton_invokes_callback_and_returns_empty():
    """연속 스켈레톤 — 재시도 후에도 스켈레톤이면 빈 문자열 반환.

    ``_fetch_full`` 은 스켈레톤 감지 시 콜백을 부른 후 1회 자동 재시도한다.
    따라서 연속 실패 시나리오에서는 콜백이 2번 호출되고, 결과는 여전히 ``""``.
    """
    crawler = AsicsCrawler()
    crawler._rate_limiter = _NoopLimiter()  # type: ignore
    fake_client = _FakeClient([_FakeResp(SKELETON_HTML), _FakeResp(SKELETON_HTML)])
    crawler._client = fake_client  # 이미 세팅 — lazy 초기화 스킵

    hits = {"n": 0}

    async def cb() -> None:
        hits["n"] += 1

    crawler.set_skeleton_callback(cb)
    result = await crawler._fetch("/c/0001")

    assert result == ""
    assert hits["n"] == 2  # 원본 + 재시도 각 1회
    assert fake_client.get_calls == [
        "https://www.asics.co.kr/c/0001",
        "https://www.asics.co.kr/c/0001",
    ]


@pytest.mark.asyncio
async def test_skeleton_retry_recovers_after_callback():
    """콜백이 쿠키 재주입에 성공하면 재시도에서 실SSR 회복."""
    crawler = AsicsCrawler()
    crawler._rate_limiter = _NoopLimiter()  # type: ignore
    fake_client = _FakeClient([_FakeResp(SKELETON_HTML), _FakeResp(REAL_HTML)])
    crawler._client = fake_client

    hits = {"n": 0}

    async def cb() -> None:
        hits["n"] += 1

    crawler.set_skeleton_callback(cb)
    result = await crawler._fetch("/c/0001")

    assert result == REAL_HTML
    assert hits["n"] == 1  # 1차만 스켈레톤 → 콜백 1회
    assert len(fake_client.get_calls) == 2


@pytest.mark.asyncio
async def test_real_html_does_not_invoke_callback():
    crawler = AsicsCrawler()
    crawler._rate_limiter = _NoopLimiter()  # type: ignore
    crawler._client = _FakeClient(_FakeResp(REAL_HTML))

    hits = {"n": 0}

    async def cb() -> None:
        hits["n"] += 1

    crawler.set_skeleton_callback(cb)
    result = await crawler._fetch("/c/0001")

    assert result == REAL_HTML
    assert hits["n"] == 0
