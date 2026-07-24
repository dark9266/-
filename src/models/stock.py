"""소싱처 재고 상태 모델.

`available_sizes=()` 의 4중 의미(전품절/조회실패/미지원/하위호환)를 명시 상태로
분리하기 위한 모델. `CandidateMatched.source_stock` 에서 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# 소싱처 관측→알림 사이 허용 신선도(초). in-flight 이벤트는 수 초 내 소비되므로
# 사실상 재생/지연 방어용 보수값 (1c-2, runtime 중앙 게이트가 참조).
DEFAULT_SOURCE_STOCK_TTL_SEC = 1800  # 30분


class StockState(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SourceStockSnapshot:
    """소싱처 재고 관측 스냅샷.

    `expires_at` 경과 시 자동 UNKNOWN 취급(체크포인트 재생 시 오래된 재고 방지).
    `reason_code` 는 UNKNOWN 사유(api_400/parse_fail/unsupported 등)를 남긴다.
    """

    state: StockState
    available_sizes: tuple[str, ...] = ()
    observed_at: float = 0.0  # epoch — 체크포인트 재생 시 오래된 재고 방지
    expires_at: float = 0.0  # 경과 시 자동 UNKNOWN 취급
    evidence_method: str = ""
    reason_code: str = ""

    def is_fresh(self, now: float) -> bool:
        return self.expires_at > now

    def usable(self, now: float) -> bool:
        # 알림 자격: 신선한 IN_STOCK + 사이즈 있음
        return (
            self.state is StockState.IN_STOCK
            and bool(self.available_sizes)
            and self.is_fresh(now)
        )


__all__ = ["DEFAULT_SOURCE_STOCK_TTL_SEC", "SourceStockSnapshot", "StockState"]
