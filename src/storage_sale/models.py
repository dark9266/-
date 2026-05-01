"""Phase B-0 — 보관판매 슬롯 캐치 봇팅 데이터 모델."""
from dataclasses import dataclass, field


@dataclass
class SlotRequest:
    """1페이지 슬롯 시도 요청."""

    product_id: str
    product_option_key: str  # 사이즈 (예: "265")
    quantity: int = 1
    max_duration_minutes: int = 30
    interval_seconds: float = 1.0


@dataclass
class SlotResult:
    """1페이지 슬롯 시도 결과."""

    success: bool
    review_id: str | None = None
    error_code: str | None = None  # USER_STOP, TIMEOUT, BAN, AUTH_FAIL, SERVER_ERROR
    attempts: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class ConfirmRequest:
    """2페이지 등록 컨펌 요청."""

    review_id: str
    return_address_id: int
    return_shipping_memo: str = ""
    delivery_method: str = ""


@dataclass
class ConfirmResult:
    """2페이지 등록 컨펌 결과."""

    success: bool
    response_data: dict | None = None
    error_message: str | None = None
