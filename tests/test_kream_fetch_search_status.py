"""KreamCrawler.fetch_search_status() 공개 API 계약 테스트 (2-2r F1).

`drain_collect_queue.py`(2-2)가 더 이상 `kream_crawler._request(max_retries=1)`
경로에 의존하지 않도록 — `_request()`는 403 시 쿠키 재초기화용 **미계측 GET
1회**를 추가로 보내고 429 는 최대 10초 지수 백오프 sleep 을 반복하며, 결국
403/429/5xx/timeout 전부를 상태코드 없이 None 으로 뭉갠다(2-0 "차단 1회 =
즉시 전면 중단" 계약 위반 + 차단 후 추가 실호출). `fetch_screens_status()`
(2-1r F3)와 동일한 정신으로, 상태코드 + 리스팅 결과를 그대로 돌려주는 단일
시도 공개 메서드를 추가했다. 전부 mock — 실 크림 호출 0.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers.kream import kream_crawler

pytestmark = pytest.mark.asyncio


class _FakeResp:
    """`_request()`의 `resp.text`(속성, 메서드 아님) 관례를 그대로 따른다."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeSession:
    """`resp` 가 Exception 이면 그대로 raise, 아니면 고정 응답 반환."""

    def __init__(self, resp):
        self._resp = resp
        self.calls: list[tuple] = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self._resp, BaseException):
            raise self._resp
        return self._resp


def _search_html(items: list[dict]) -> str:
    """`_extract_page_data`/`_extract_listing_products` 가 실제로 파싱 가능한
    최소 __NUXT_DATA__ 구조 — `products` 키 아래 리스트(2차 폴백 경로)."""
    import json

    payload = {"products": items}
    return (
        "<html><body><script id=\"__NUXT_DATA__\" type=\"application/json\">"
        f"{json.dumps([payload])}"
        "</script></body></html>"
    )


@pytest.fixture(autouse=True)
def _patch_crawler_internals(monkeypatch):
    """세션/인증헤더/예산 체크를 mock — 계약(상태코드·재시도횟수)만 검증."""
    monkeypatch.setattr(kream_crawler, "_build_api_auth_headers", lambda: {})
    monkeypatch.setattr("src.crawlers.kream.check_budget", AsyncMock(return_value=None))
    monkeypatch.setattr("src.crawlers.kream.record_call", AsyncMock(return_value=None))


def _patch_session(monkeypatch, session: _FakeSession):
    monkeypatch.setattr(kream_crawler, "_get_session", AsyncMock(return_value=session))


async def test_success_with_matching_item_returns_status_and_list(monkeypatch):
    items = [{"id": "P1", "model_number": "DQ8423-100", "name": "x"}]
    monkeypatch.setattr(kream_crawler, "_extract_page_data", lambda html: {"products": items})
    monkeypatch.setattr(
        kream_crawler, "_extract_listing_products", lambda data: data["products"]
    )
    session = _FakeSession(_FakeResp(200, "<html>fake</html>"))
    _patch_session(monkeypatch, session)

    status, results = await kream_crawler.fetch_search_status("DQ8423-100")

    assert status == 200
    assert results == items
    assert len(session.calls) == 1  # 재시도 없음 = 호출 1회


async def test_success_but_no_listing_parsed_returns_status_and_none(monkeypatch):
    """2xx 인데 `_extract_page_data` 가 아무 구조도 못 찾음 — None."""
    monkeypatch.setattr(kream_crawler, "_extract_page_data", lambda html: None)
    session = _FakeSession(_FakeResp(200, "<html>empty</html>"))
    _patch_session(monkeypatch, session)

    status, results = await kream_crawler.fetch_search_status("ZZ0000")

    assert status == 200
    assert results is None


async def test_404_returns_status_and_none_single_attempt(monkeypatch):
    session = _FakeSession(_FakeResp(404))
    _patch_session(monkeypatch, session)

    status, results = await kream_crawler.fetch_search_status("X")

    assert status == 404
    assert results is None
    assert len(session.calls) == 1


async def test_403_returns_status_and_none_no_cookie_reinit_retry(monkeypatch):
    """`_request()` 는 403 시 쿠키 재초기화 후 재시도하지만, 이 메서드는 안 한다."""
    session = _FakeSession(_FakeResp(403))
    _patch_session(monkeypatch, session)

    status, results = await kream_crawler.fetch_search_status("X")

    assert status == 403
    assert results is None
    assert len(session.calls) == 1  # 쿠키 재초기화 재시도용 추가 GET 없음


async def test_429_returns_status_and_none_no_backoff_sleep(monkeypatch):
    """`_request()` 는 429 시 최대 10초 지수 백오프로 재시도하지만, 이 메서드는 즉시 반환한다."""
    session = _FakeSession(_FakeResp(429))
    _patch_session(monkeypatch, session)

    status, results = await kream_crawler.fetch_search_status("X")

    assert status == 429
    assert results is None
    assert len(session.calls) == 1


async def test_5xx_single_attempt_no_backoff_retry(monkeypatch):
    session = _FakeSession(_FakeResp(503))
    _patch_session(monkeypatch, session)

    status, results = await kream_crawler.fetch_search_status("X")

    assert status == 503
    assert results is None
    assert len(session.calls) == 1


async def test_connection_exception_returns_status_zero(monkeypatch):
    session = _FakeSession(ConnectionError("boom"))
    _patch_session(monkeypatch, session)

    status, results = await kream_crawler.fetch_search_status("X")

    assert status == 0
    assert results is None
    assert len(session.calls) == 1


async def test_calls_check_budget_and_record_call(monkeypatch):
    monkeypatch.setattr(kream_crawler, "_extract_page_data", lambda html: None)
    session = _FakeSession(_FakeResp(200, "<html></html>"))
    _patch_session(monkeypatch, session)

    with patch("src.crawlers.kream.check_budget", new=AsyncMock()) as budget_mock, patch(
        "src.crawlers.kream.record_call", new=AsyncMock()
    ) as record_mock:
        await kream_crawler.fetch_search_status("X", purpose="collect_queue_drain")

    budget_mock.assert_awaited_once()
    record_mock.assert_awaited_once()
    args = record_mock.await_args.args
    assert args[0] == "/search"
    assert args[1] == "GET"
    assert args[2] == 200
    assert args[4] == "collect_queue_drain"
