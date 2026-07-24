"""KreamCrawler.fetch_screens_status() 공개 API 계약 테스트 (2-1r F3).

`bootstrap_cold_volumes_light.py` 가 더 이상 kream.py 의 private 속성
(`_get_session`/`_build_api_auth_headers`/모듈 상수 등)에 의존하지 않도록,
상태코드 + 거래 파싱 결과를 그대로 돌려주는 공개 메서드 하나로 대체했다.
이 메서드는 재시도·백오프가 없다(1회 시도) — `_request()`의 기존 재시도
경로는 이 테스트에서 건드리지 않는다. 전부 mock — 실 크림 호출 0.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.crawlers.kream import kream_crawler

pytestmark = pytest.mark.asyncio


class _FakeResp:
    def __init__(self, status_code: int, json_data=None, *, json_raises: bool = False):
        self.status_code = status_code
        self._json_data = json_data
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("bad json")
        return self._json_data


class _FakeSession:
    """`resp` 가 리스트면 순차 소비, 아니면 고정 응답. Exception 이면 그대로 raise."""

    def __init__(self, resp):
        self._resp = resp
        self.calls: list[tuple] = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self._resp, BaseException):
            raise self._resp
        if isinstance(self._resp, list):
            return self._resp.pop(0)
        return self._resp


def _transaction_history_payload(sales: list[dict]) -> dict:
    return {"transaction_history": {"sales": sales, "asks": [], "bids": []}}


@pytest.fixture(autouse=True)
def _patch_crawler_internals(monkeypatch):
    """세션/인증헤더/예산 체크를 mock — 계약(상태코드·재시도횟수)만 검증."""
    monkeypatch.setattr(kream_crawler, "_build_api_auth_headers", lambda: {})
    monkeypatch.setattr("src.crawlers.kream.check_budget", AsyncMock(return_value=None))
    monkeypatch.setattr("src.crawlers.kream.record_call", AsyncMock(return_value=None))


def _patch_session(monkeypatch, session: _FakeSession):
    monkeypatch.setattr(kream_crawler, "_get_session", AsyncMock(return_value=session))


async def test_success_with_sales_returns_status_and_positive_stats(monkeypatch):
    payload = _transaction_history_payload(
        [{"price": 100000, "date_created": "2026-07-01T00:00:00"}] * 3
    )
    session = _FakeSession(_FakeResp(200, payload))
    _patch_session(monkeypatch, session)

    status, stats = await kream_crawler.fetch_screens_status("P1")

    assert status == 200
    assert isinstance(stats, dict)
    assert "volume_7d" in stats and "volume_30d" in stats
    assert len(session.calls) == 1  # 재시도 없음 = 호출 1회


async def test_success_structure_found_but_empty_sales_returns_confirmed_zero(monkeypatch):
    payload = _transaction_history_payload([])
    session = _FakeSession(_FakeResp(200, payload))
    _patch_session(monkeypatch, session)

    status, stats = await kream_crawler.fetch_screens_status("P1")

    assert status == 200
    assert stats == {
        "volume_7d": 0, "volume_30d": 0, "last_trade_date": None, "price_trend": "",
    }


async def test_success_but_unexpected_shape_returns_none(monkeypatch):
    """transaction_history 구조 자체를 못 찾음 — dict=None (success_zero 와 구분)."""
    session = _FakeSession(_FakeResp(200, {"unexpected": "shape"}))
    _patch_session(monkeypatch, session)

    status, stats = await kream_crawler.fetch_screens_status("P1")

    assert status == 200
    assert stats is None


async def test_json_parse_failure_returns_status_and_none(monkeypatch):
    session = _FakeSession(_FakeResp(200, json_raises=True))
    _patch_session(monkeypatch, session)

    status, stats = await kream_crawler.fetch_screens_status("P1")

    assert status == 200
    assert stats is None


async def test_404_returns_status_and_none_single_attempt(monkeypatch):
    session = _FakeSession(_FakeResp(404))
    _patch_session(monkeypatch, session)

    status, stats = await kream_crawler.fetch_screens_status("P1")

    assert status == 404
    assert stats is None
    assert len(session.calls) == 1


async def test_403_returns_status_and_none_no_cookie_reinit_retry(monkeypatch):
    """`_request()` 는 403 시 쿠키 재초기화 후 재시도하지만, 이 메서드는 안 한다."""
    session = _FakeSession(_FakeResp(403))
    _patch_session(monkeypatch, session)

    status, stats = await kream_crawler.fetch_screens_status("P1")

    assert status == 403
    assert stats is None
    assert len(session.calls) == 1


async def test_5xx_single_attempt_no_backoff_retry(monkeypatch):
    """`_request()` 는 5xx 를 재시도하지만, 이 메서드는 1회 시도로 끝낸다."""
    session = _FakeSession(_FakeResp(503))
    _patch_session(monkeypatch, session)

    status, stats = await kream_crawler.fetch_screens_status("P1")

    assert status == 503
    assert stats is None
    assert len(session.calls) == 1


async def test_connection_exception_returns_status_zero(monkeypatch):
    session = _FakeSession(ConnectionError("boom"))
    _patch_session(monkeypatch, session)

    status, stats = await kream_crawler.fetch_screens_status("P1")

    assert status == 0
    assert stats is None
    assert len(session.calls) == 1


async def test_calls_check_budget_and_record_call(monkeypatch):
    session = _FakeSession(_FakeResp(200, _transaction_history_payload([])))
    _patch_session(monkeypatch, session)

    with patch("src.crawlers.kream.check_budget", new=AsyncMock()) as budget_mock, patch(
        "src.crawlers.kream.record_call", new=AsyncMock()
    ) as record_mock:
        await kream_crawler.fetch_screens_status("P1", purpose="bootstrap_light")

    budget_mock.assert_awaited_once()
    record_mock.assert_awaited_once()
    args = record_mock.await_args.args
    assert args[0] == "/api/screens/products/P1"
    assert args[1] == "GET"
    assert args[2] == 200
    assert args[4] == "bootstrap_light"
