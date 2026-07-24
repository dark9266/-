"""어댑터 stock capability 계약 테스트 (1c-4).

이번 조각은 `on_running`/`musinsa` 2곳만 `stock_capability` 를 선언한다.
나머지 어댑터는 후속 조각 몫 — 여기서는 "선언 안 한 어댑터가 깨지지 않는지"만
확인(속성 부재 = 정상, getattr 기본값 사용은 호출자 책임).
"""

from __future__ import annotations

import time

from src.adapters._stock_capability import StockCapability, unobservable_snapshot
from src.adapters.musinsa_adapter import MusinsaAdapter
from src.adapters.on_running_adapter import OnRunningAdapter
from src.adapters.stussy_adapter import StussyAdapter
from src.models.stock import DEFAULT_SOURCE_STOCK_TTL_SEC, StockState


class TestStockCapabilityEnum:
    def test_members(self):
        assert StockCapability.SIZE_STOCK_SUPPORTED.value == "SUPPORTED"
        assert StockCapability.SIZE_STOCK_RESOLVABLE.value == "RESOLVABLE"
        assert StockCapability.SIZE_STOCK_UNOBSERVABLE.value == "UNOBSERVABLE"

    def test_is_str_enum(self):
        # 로그/직렬화에서 그냥 문자열처럼 다룰 수 있어야 함.
        assert StockCapability.SIZE_STOCK_SUPPORTED == "SUPPORTED"


class TestUnobservableSnapshot:
    def test_unknown_reason_unsupported(self):
        now = time.time()
        snap = unobservable_snapshot(now)
        assert snap.state == StockState.UNKNOWN
        assert snap.reason_code == "unsupported"
        assert snap.available_sizes == ()
        assert snap.observed_at == now
        assert snap.expires_at == now + DEFAULT_SOURCE_STOCK_TTL_SEC

    def test_not_usable(self):
        now = time.time()
        snap = unobservable_snapshot(now)
        assert snap.usable(now) is False


class TestAdapterCapabilityDeclarations:
    """이번 조각에서 선언하는 건 on_running + musinsa 2곳뿐."""

    def test_on_running_supported(self):
        assert OnRunningAdapter.stock_capability == StockCapability.SIZE_STOCK_SUPPORTED

    def test_musinsa_supported(self):
        assert MusinsaAdapter.stock_capability == StockCapability.SIZE_STOCK_SUPPORTED

    def test_undeclared_adapter_has_no_attribute(self):
        """롤아웃 안 한 어댑터는 속성 자체가 없어야 함(과공학 금지 확인)."""
        assert not hasattr(StussyAdapter, "stock_capability")
        # getattr 기본값 사용은 호출자 책임 — 크래시 없이 조회 가능해야 함.
        assert getattr(StussyAdapter, "stock_capability", None) is None
