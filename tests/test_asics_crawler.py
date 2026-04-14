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
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.get_calls: list[str] = []

    async def get(self, url: str):
        self.get_calls.append(url)
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
    crawler = AsicsCrawler()
    crawler._rate_limiter = _NoopLimiter()  # type: ignore
    fake_client = _FakeClient(_FakeResp(SKELETON_HTML))
    crawler._client = fake_client  # 이미 세팅 — lazy 초기화 스킵

    hits = {"n": 0}

    async def cb() -> None:
        hits["n"] += 1

    crawler.set_skeleton_callback(cb)
    result = await crawler._fetch("/c/0001")

    assert result == ""
    assert hits["n"] == 1
    assert fake_client.get_calls == ["https://www.asics.co.kr/c/0001"]


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
