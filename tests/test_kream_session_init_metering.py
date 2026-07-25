"""세션 초기화 GET 계측 + 차단 시 본요청 차단 계약 테스트 (2-v F2).

코덱스 적대검증 실결함: `_init_cookies()` 의 크림 루트 GET 이
① `record_call` 계측 없이 나가 하드캡/서킷 판정에서 통째로 빠졌고,
② 응답이 403/429 여도 그 사실을 감춘 채 이후 본 요청이 그대로 전송돼
   "차단 1회 = 즉시 전면 중단"(2-0) 계약이 무력화됐다.

이 파일은 (1) 초기화 GET 이 `{purpose}:session_init` 로 계측되는지,
(2) 초기화가 차단되면 raw 배치 경로가 본 요청 없이 그 상태코드를 그대로
돌려주는지, (3) 정상/예외 경로의 기존 fail-open 동작이 보존되는지를 고정한다.

전부 mock — 실 크림 호출 0. 모듈 전역 싱글턴(`kream_crawler`) 오염을 피하려고
매 테스트가 `KreamCrawler()` 인스턴스를 새로 만든다.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import src.core.kream_budget as kb
from src.crawlers.kream import KreamCrawler

pytestmark = pytest.mark.asyncio


class _FakeCookies:
    def __init__(self, values: dict | None = None):
        self._values = values or {}

    def get(self, key, default=""):
        return self._values.get(key, default)


class _FakeResp:
    def __init__(self, status_code: int, *, text: str = "", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


class _FakeSession:
    """`get` = 초기화 루트 방문, `request` = 본 요청. 각각 호출을 기록한다."""

    def __init__(self, init_resp, main_resp=None):
        self._init_resp = init_resp
        self._main_resp = main_resp
        self.cookies = _FakeCookies({"webDid": "did-123"})
        self.get_calls: list[tuple] = []
        self.request_calls: list[tuple] = []

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if isinstance(self._init_resp, BaseException):
            raise self._init_resp
        return self._init_resp

    async def request(self, method, url, **kwargs):
        self.request_calls.append((method, url, kwargs))
        if isinstance(self._main_resp, BaseException):
            raise self._main_resp
        return self._main_resp


@pytest.fixture
def crawler(monkeypatch):
    """예산 체크/계측은 스파이로 — 계약(계측 인자·요청 횟수)만 본다."""
    monkeypatch.setattr("src.crawlers.kream.check_budget", AsyncMock(return_value=None))
    monkeypatch.setattr("src.crawlers.kream.record_call", AsyncMock(return_value=None))
    return KreamCrawler()


def _attach(crawler: KreamCrawler, session: _FakeSession) -> None:
    """AsyncSession 생성 없이 세션만 주입 — `_initialized=False` 라 이번 호출에서
    `_init_cookies()` 가 실행된다(실제 배치 첫 호출과 동일 상태)."""
    crawler._session = session
    crawler._initialized = False


def _record_calls():
    from src.crawlers import kream as kream_mod

    return kream_mod.record_call.await_args_list


def _screens_payload() -> dict:
    return {
        "transaction_history": {
            "sales": [{"price": 100000, "date_created": "2026-07-01T00:00:00"}],
            "asks": [],
            "bids": [],
        }
    }


# ---------------------------------------------------------------------------
# 1. 초기화 GET 계측
# ---------------------------------------------------------------------------


async def test_init_cookies_records_call_with_session_init_purpose(crawler):
    session = _FakeSession(_FakeResp(200, text="<html></html>"))
    _attach(crawler, session)

    with kb.kream_purpose("bootstrap_light"):
        await crawler._init_cookies()

    calls = _record_calls()
    assert len(calls) == 1
    endpoint, method, status, _latency, purpose = calls[0].args
    assert (endpoint, method, status) == ("/", "GET", 200)
    assert purpose == "bootstrap_light:session_init"


async def test_init_cookies_records_block_status(crawler):
    session = _FakeSession(_FakeResp(403))
    _attach(crawler, session)

    with kb.kream_purpose("collect_queue_drain"):
        await crawler._init_cookies()

    endpoint, method, status, _latency, purpose = _record_calls()[0].args
    assert (endpoint, method, status) == ("/", "GET", 403)
    assert purpose == "collect_queue_drain:session_init"
    assert crawler._last_init_status == 403


async def test_init_cookies_exception_records_none_status_and_keeps_going(crawler):
    """연결 실패는 기존과 동일하게 fail-open — 상태는 None(차단 아님)으로 남는다."""
    session = _FakeSession(TimeoutError("timeout"))
    _attach(crawler, session)

    await crawler._init_cookies()

    _endpoint, _method, status, _latency, _purpose = _record_calls()[0].args
    assert status is None
    assert crawler._last_init_status is None
    assert crawler._initialized is True


# ---------------------------------------------------------------------------
# 2. 초기화가 차단되면 본 요청을 보내지 않는다 (screens)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("block_status", [403, 429])
async def test_fetch_screens_status_short_circuits_when_init_blocked(crawler, block_status):
    session = _FakeSession(_FakeResp(block_status), _FakeResp(200, json_data=_screens_payload()))
    _attach(crawler, session)

    status, data = await crawler.fetch_screens_status("P1")

    assert status == block_status
    assert data is None
    assert len(session.get_calls) == 1  # 초기화 GET 1회
    assert session.request_calls == []  # 본 요청 0회 — 추가 실호출 없음


@pytest.mark.parametrize("block_status", [403, 429])
async def test_fetch_search_status_short_circuits_when_init_blocked(crawler, block_status):
    session = _FakeSession(_FakeResp(block_status), _FakeResp(200, text="<html></html>"))
    _attach(crawler, session)

    status, products = await crawler.fetch_search_status("나이키")

    assert status == block_status
    assert products is None
    assert len(session.get_calls) == 1
    assert session.request_calls == []


# ---------------------------------------------------------------------------
# 3. 정상 초기화 경로는 그대로 진행한다 (과잉 차단 방어)
# ---------------------------------------------------------------------------


async def test_fetch_screens_status_proceeds_when_init_ok(crawler):
    session = _FakeSession(
        _FakeResp(200, text="<html></html>"), _FakeResp(200, json_data=_screens_payload())
    )
    _attach(crawler, session)

    status, data = await crawler.fetch_screens_status("P1")

    assert status == 200
    assert data is not None and data["volume_7d"] >= 0
    assert len(session.request_calls) == 1


async def test_fetch_screens_status_proceeds_when_init_failed_with_exception(crawler):
    """초기화 연결 실패(=차단 아님)는 기존처럼 본 요청을 시도한다."""
    session = _FakeSession(TimeoutError("timeout"), _FakeResp(200, json_data=_screens_payload()))
    _attach(crawler, session)

    status, _data = await crawler.fetch_screens_status("P1")

    assert status == 200
    assert len(session.request_calls) == 1


async def test_short_circuit_only_applies_to_the_call_that_ran_init(crawler):
    """이미 초기화된 세션이면(과거 런의 잔여 상태) 본 요청을 막지 않는다.

    `_last_init_status` 는 "이번 호출에서 초기화가 났고 그게 차단이었나" 판별용
    이다 — 초기화를 유발하지 않은 후속 호출까지 영구 차단하면 안 된다.
    """
    session = _FakeSession(_FakeResp(403), _FakeResp(200, json_data=_screens_payload()))
    crawler._session = session
    crawler._initialized = True  # 이번 호출은 초기화를 유발하지 않음
    crawler._last_init_status = 403  # 과거 런의 잔여 상태

    status, _data = await crawler.fetch_screens_status("P1")

    assert status == 200
    assert session.get_calls == []  # 초기화 GET 재발생 없음
    assert len(session.request_calls) == 1
