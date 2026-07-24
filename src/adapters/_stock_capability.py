"""어댑터별 사이즈 재고 관측 capability 계약 (1c-4).

`StockCapability` 는 소싱처가 사이즈별 실재고를 어떻게(혹은 못) 관측하는지
선언하는 **최소** 계약이다. 어댑터 클래스가 `stock_capability` 클래스 속성으로
선언하면, 이후 조각(전체 롤아웃/모니터링)이 소싱처별 재고 신뢰도를 일괄 조회할
수 있다. 이번 조각은 on_running/musinsa 2곳만 SUPPORTED 로 선언 — 나머지
어댑터 롤아웃은 후속 조각 몫이라 여기서 건드리지 않는다.

과공학 금지: enum + 헬퍼 1개만. 레지스트리/자동판정 로직 없음.
"""

from __future__ import annotations

from enum import Enum

from src.models.stock import DEFAULT_SOURCE_STOCK_TTL_SEC, SourceStockSnapshot, StockState


class StockCapability(str, Enum):
    """어댑터가 사이즈별 실재고를 관측할 수 있는 경로."""

    SIZE_STOCK_SUPPORTED = "SUPPORTED"  # 덤프/상세 응답에 사이즈 재고 있음
    SIZE_STOCK_RESOLVABLE = "RESOLVABLE"  # listing 엔 없지만 JIT 상세 GET 로 가능
    SIZE_STOCK_UNOBSERVABLE = "UNOBSERVABLE"  # 허용 GET 경로에 없음


def unobservable_snapshot(now: float) -> SourceStockSnapshot:
    """`SIZE_STOCK_UNOBSERVABLE` 어댑터용 UNKNOWN 스냅샷 헬퍼.

    허용된 GET 경로에 사이즈별 재고가 아예 없는 소싱처가, candidate 발행 시
    자체 판단으로 재고를 지어내지 않고(정확성 1순위) runtime 게이트에
    검증 보류를 명시적으로 알리기 위한 공통 생성자.
    """
    return SourceStockSnapshot(
        state=StockState.UNKNOWN,
        available_sizes=(),
        observed_at=now,
        expires_at=now + DEFAULT_SOURCE_STOCK_TTL_SEC,
        evidence_method="",
        reason_code="unsupported",
    )


__all__ = ["StockCapability", "unobservable_snapshot"]
