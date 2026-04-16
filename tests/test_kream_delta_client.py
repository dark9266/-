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
    build_snapshot_fn,
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
    """options/display mock 응답 — 레거시 sell_now_price 필드 사용.

    SDUI parameters.price 는 구매입찰 추천 시작가이므로
    _extract_best_sell_now 가 무시. 레거시 필드로 가격 전달.
    """
    return {
        "sections": [
            {
                "items": [
                    {
                        "sell_now_price": price,
                        "size": option_key,
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
    # MIN 기준 보수 대표가 (260=450k, 270=500k, 280=None) → 450000
    assert out["sell_now_price"] == 450000
    assert out["top_size"] == "260"
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


def test_extract_best_sell_now_sdui_parameters_ignored() -> None:
    """SDUI parameters.price 는 구매입찰 추천가이므로 무시해야 함."""
    data = {
        "items": [
            {"parameters": {"option_key": ["260"], "price": [100000]}},
            {"parameters": {"option_key": ["270"], "price": [150000]}},
            {"parameters": {"option_key": ["280"], "price": [120000]}},
        ]
    }
    assert _extract_best_sell_now(data) is None


def test_extract_best_sell_now_legacy_field() -> None:
    data = {"sell_now_price": 99000}
    assert _extract_best_sell_now(data) == 99000


def test_extract_best_sell_now_none_on_empty() -> None:
    assert _extract_best_sell_now({}) is None
    assert _extract_best_sell_now([]) is None


def test_pick_best_sell_ignores_none_and_zero() -> None:
    """MIN 기준 — 260 사이즈 450000 이 최저, 280=None 은 스킵."""
    product = _sample_product()
    size, price = _pick_best_sell(product)
    assert size == "260"
    assert price == 450000


# ─── build_snapshot_fn (V3Runtime 어댑터) ────────────────


async def test_build_snapshot_fn_exact_size_match() -> None:
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=_FakeSnapshotFn({"111": _sample_product("111")}),
        sleep_fn=_NoSleep(),
    )
    fn = build_snapshot_fn(client)
    out = await fn(111, "260")
    assert out["sell_now_price"] == 450000
    assert out["volume_7d"] == 12
    assert out["size_prices"] == [
        {"size": "260", "sell_now_price": 450000, "buy_now_price": 460000}
    ]


async def test_build_snapshot_fn_size_miss_returns_none() -> None:
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=_FakeSnapshotFn({"111": _sample_product("111")}),
        sleep_fn=_NoSleep(),
    )
    fn = build_snapshot_fn(client)
    assert await fn(111, "999") is None


async def test_build_snapshot_fn_size_none_price_returns_none() -> None:
    """해당 사이즈 sell_now_price=None 이면 drop (280 사이즈)."""
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=_FakeSnapshotFn({"111": _sample_product("111")}),
        sleep_fn=_NoSleep(),
    )
    fn = build_snapshot_fn(client)
    assert await fn(111, "280") is None


async def test_build_snapshot_fn_empty_snapshot_returns_none() -> None:
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=_FakeSnapshotFn({"111": None}),
        sleep_fn=_NoSleep(),
    )
    fn = build_snapshot_fn(client)
    assert await fn(111, "260") is None


async def test_build_snapshot_fn_all_size_uses_min() -> None:
    """size='' 또는 'ALL' 은 MIN sell_now 를 보수 기준으로 사용.

    과거: MAX → 사이즈 전량에 걸쳐 최고가 기준 → 거짓양성 314건 (2026-04-14).
    현재: MIN → 최저 사이즈로도 수익 나면 진짜 기회. 거짓양성 차단.
    """
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=_FakeSnapshotFn({"111": _sample_product("111")}),
        sleep_fn=_NoSleep(),
    )
    fn = build_snapshot_fn(client)
    # _sample_product: 260=450000, 270=500000, 280=None → min=450000
    out = await fn(111, "ALL")
    assert out["sell_now_price"] == 450000
    assert out["volume_7d"] == 12
    assert len(out["size_prices"]) == 2  # 280 sell_now=None 은 제외

    out_empty = await fn(111, "")
    assert out_empty["sell_now_price"] == 450000
    assert out_empty["volume_7d"] == 12


async def test_build_snapshot_fn_cache_hit_no_extra_call() -> None:
    """동일 product_id 재조회 시 캐시 히트 → snapshot 호출 0."""
    product = _sample_product("111")
    snapshot = _FakeSnapshotFn({"111": product})
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=snapshot,
        sleep_fn=_NoSleep(),
    )
    fn = build_snapshot_fn(client, cache_ttl_sec=60.0)

    out1 = await fn(111, "260")
    assert out1["sell_now_price"] == 450000
    assert len(snapshot.calls) == 1

    out2 = await fn(111, "270")
    assert out2["sell_now_price"] == 500000
    assert len(snapshot.calls) == 1  # 캐시 히트 — 추가 호출 없음


async def test_build_snapshot_fn_cache_different_pid() -> None:
    """다른 product_id 는 캐시 미스 → 별도 호출."""
    product = _sample_product("111")
    snapshot = _FakeSnapshotFn({"111": product, "222": product})
    client = KreamDeltaClient(
        request_fn=_FakeRequestFn([]),
        snapshot_fn=snapshot,
        sleep_fn=_NoSleep(),
    )
    fn = build_snapshot_fn(client, cache_ttl_sec=60.0)

    await fn(111, "260")
    await fn(222, "260")
    assert len(snapshot.calls) == 2  # 각각 1회씩


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
