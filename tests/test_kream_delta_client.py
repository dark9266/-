"""KreamDeltaClient 단위 테스트 (Phase 3 파일럿).

실호출 금지 — 모든 의존성 mock.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.kream_budget import KreamBudgetExceeded
from src.crawlers.kream_delta_client import (
    KreamDeltaClient,
    _extract_best_sell_now,
    _pick_best_sell,
)
from src.models.product import KreamProduct, KreamSizePrice

# ─── Fake 의존성 ──────────────────────────────────────────


class _FakeRequestFn:
    """crawler._request 호환 mock.

    queue: 호출 순서대로 반환할 값들. 예외를 넣으면 raise.
    """

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        parse_json: bool = True,
        purpose: str = "manual",
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "purpose": purpose,
            }
        )
        if not self._responses:
            return None
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeSnapshotFn:
    def __init__(self, responses: dict[str, Any]):
        self._responses = responses
        self.calls: list[str] = []

    async def __call__(self, product_id: str) -> Any:
        self.calls.append(product_id)
        item = self._responses.get(product_id)
        if isinstance(item, BaseException):
            raise item
        return item


class _NoSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.waits.append(sec)


# ─── Helpers ──────────────────────────────────────────────


def _sdui_response(price: int, option_key: str = "260") -> dict:
    """options/display SDUI 형식 mock 응답."""
    return {
        "sections": [
            {
                "items": [
                    {
                        "action": {
                            "parameters": {
                                "option_key": [option_key],
                                "price": [price],
                            }
                        }
                    }
                ]
            }
        ]
    }


# ─── fetch_light ──────────────────────────────────────────


async def test_fetch_light_happy_path_multiple_ids() -> None:
    request = _FakeRequestFn(
        [_sdui_response(450000), _sdui_response(320000), _sdui_response(780000)]
    )
    snapshot = _FakeSnapshotFn({})
    sleep = _NoSleep()
    client = KreamDeltaClient(
        request_fn=request,
        snapshot_fn=snapshot,
        rate_limit_sec=1.5,
        sleep_fn=sleep,
    )

    results = await client.fetch_light([111, 222, 333])

    assert len(results) == 3
    assert results[0] == {"product_id": 111, "sell_now_price": 450000}
    assert results[1] == {"product_id": 222, "sell_now_price": 320000}
    assert results[2] == {"product_id": 333, "sell_now_price": 780000}
    # rate limit: 첫 호출 전엔 대기 X, 이후 2회 대기
    assert sleep.waits == [1.5, 1.5]
    # 호출 횟수 = ids 개수 (배치 API 없음 → 개별 루프)
    assert len(request.calls) == 3
    for call in request.calls:
        assert call["method"] == "GET"
        assert call["url"] == "/api/p/options/display"
        assert call["params"]["picker_type"] == "selling"
        assert call["purpose"] == "delta_light"


async def test_fetch_light_empty_ids_no_call() -> None:
    request = _FakeRequestFn([])
    client = KreamDeltaClient(
        request_fn=request,
        snapshot_fn=_FakeSnapshotFn({}),
        sleep_fn=_NoSleep(),
    )
    assert await client.fetch_light([]) == []
    assert request.calls == []


async def test_fetch_light_404_returns_empty_price_not_exception() -> None:
    """크림 404 = 거래량 0 신규. None 응답으로 위장된 404 → sell_now_price=None."""
    request = _FakeRequestFn([None, _sdui_response(500000)])
    client = KreamDeltaClient(
        request_fn=request,
        snapshot_fn=_FakeSnapshotFn({}),
        rate_limit_sec=0.0,
        sleep_fn=_NoSleep(),
    )
    results = await client.fetch_light([111, 222])
    assert results[0] == {"product_id": 111, "sell_now_price": None}
    assert results[1] == {"product_id": 222, "sell_now_price": 500000}


async def test_fetch_light_budget_exceeded_propagates() -> None:
    request = _FakeRequestFn([_sdui_response(100000), KreamBudgetExceeded("cap hit")])
    client = KreamDeltaClient(
        request_fn=request,
        snapshot_fn=_FakeSnapshotFn({}),
        rate_limit_sec=0.0,
        sleep_fn=_NoSleep(),
    )
    with pytest.raises(KreamBudgetExceeded):
        await client.fetch_light([111, 222])
    # 첫 상품은 처리됨을 확인 (위치 기반 호출)
    assert request.calls[0]["params"]["product_id"] == "111"


async def test_fetch_light_generic_exception_isolated() -> None:
    """개별 예외는 로그만, 다음 상품 진행."""
    request = _FakeRequestFn([RuntimeError("boom"), _sdui_response(200000)])
    client = KreamDeltaClient(
        request_fn=request,
        snapshot_fn=_FakeSnapshotFn({}),
        rate_limit_sec=0.0,
        sleep_fn=_NoSleep(),
    )
    results = await client.fetch_light([111, 222])
    # 예외난 것은 결과에 포함 X, 정상 건만 반환
    assert len(results) == 1
    assert results[0] == {"product_id": 222, "sell_now_price": 200000}


async def test_fetch_light_non_int_id_skipped() -> None:
    request = _FakeRequestFn([_sdui_response(99000)])
    client = KreamDeltaClient(
        request_fn=request,
        snapshot_fn=_FakeSnapshotFn({}),
        rate_limit_sec=0.0,
        sleep_fn=_NoSleep(),
    )
    results = await client.fetch_light(["abc", 777])  # type: ignore[list-item]
    assert len(results) == 1
    assert results[0]["product_id"] == 777


# ─── get_snapshot ─────────────────────────────────────────


def _sample_product(pid: str = "111") -> KreamProduct:
    return KreamProduct(
        product_id=pid,
        name="Jordan 1 Retro",
        model_number="DZ5485-612",
        brand="Jordan",
        size_prices=[
            KreamSizePrice(size="260", sell_now_price=450000, buy_now_price=460000),
            KreamSizePrice(size="270", sell_now_price=500000, buy_now_price=510000),
            KreamSizePrice(size="280", sell_now_price=None, buy_now_price=490000),
        ],
        volume_7d=12,
        volume_30d=48,
    )


async def test_get_snapshot_happy_path() -> None:
    product = _sample_product("111")
    snapshot = _FakeSnapshotFn({"111": product})
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=snapshot,
        sleep_fn=_NoSleep(),
    )

    out = await client.get_snapshot(111)

    assert out["product_id"] == 111
    assert out["model_number"] == "DZ5485-612"
    assert out["brand"] == "Jordan"
    assert out["sell_now_price"] == 500000  # 최고가 사이즈 픽
    assert out["top_size"] == "270"
    assert out["volume_7d"] == 12
    assert len(out["size_prices"]) == 3
    assert snapshot.calls == ["111"]


async def test_get_snapshot_not_found_returns_empty_dict() -> None:
    snapshot = _FakeSnapshotFn({"111": None})
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=snapshot,
        sleep_fn=_NoSleep(),
    )
    assert await client.get_snapshot(111) == {}


async def test_get_snapshot_exception_isolated() -> None:
    snapshot = _FakeSnapshotFn({"111": RuntimeError("boom")})
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=snapshot,
        sleep_fn=_NoSleep(),
    )
    assert await client.get_snapshot(111) == {}


async def test_get_snapshot_budget_exceeded_propagates() -> None:
    snapshot = _FakeSnapshotFn({"111": KreamBudgetExceeded("cap hit")})
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=snapshot,
        sleep_fn=_NoSleep(),
    )
    with pytest.raises(KreamBudgetExceeded):
        await client.get_snapshot(111)


async def test_get_snapshot_non_int_returns_empty() -> None:
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=_FakeSnapshotFn({}),
        sleep_fn=_NoSleep(),
    )
    assert await client.get_snapshot("abc") == {}  # type: ignore[arg-type]


# ─── 생성자/helper ────────────────────────────────────────


def test_constructor_requires_crawler_or_fns() -> None:
    with pytest.raises(ValueError):
        KreamDeltaClient()


def test_extract_best_sell_now_sdui() -> None:
    data = {
        "items": [
            {"parameters": {"option_key": ["260"], "price": [100000]}},
            {"parameters": {"option_key": ["270"], "price": [150000]}},
            {"parameters": {"option_key": ["280"], "price": [120000]}},
        ]
    }
    assert _extract_best_sell_now(data) == 150000


def test_extract_best_sell_now_legacy_field() -> None:
    data = {"sell_now_price": 99000}
    assert _extract_best_sell_now(data) == 99000


def test_extract_best_sell_now_none_on_empty() -> None:
    assert _extract_best_sell_now({}) is None
    assert _extract_best_sell_now([]) is None


def test_pick_best_sell_ignores_none_and_zero() -> None:
    product = _sample_product()
    size, price = _pick_best_sell(product)
    assert size == "270"
    assert price == 500000


def test_pick_best_sell_all_none() -> None:
    product = KreamProduct(
        product_id="1",
        name="x",
        model_number="x",
        size_prices=[KreamSizePrice(size="260", sell_now_price=None)],
    )
    size, price = _pick_best_sell(product)
    assert size == ""
    assert price is None
