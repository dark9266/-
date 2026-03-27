"""상품 데이터 모델."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Signal(Enum):
    STRONG_BUY = "강력매수"
    BUY = "매수"
    WATCH = "관망"
    NOT_RECOMMENDED = "비추천"


@dataclass
class KreamSizePrice:
    """크림 사이즈별 가격 정보."""

    size: str
    sell_now_price: int | None = None  # 즉시판매가 (내가 팔 수 있는 가격)
    buy_now_price: int | None = None  # 즉시구매가
    bid_count: int = 0  # 구매 입찰 수
    last_sale_price: int | None = None
    last_sale_date: datetime | None = None


@dataclass
class KreamProduct:
    """크림 상품 정보."""

    product_id: str
    name: str
    model_number: str
    brand: str = ""
    image_url: str = ""
    category: str = "sneakers"
    url: str = ""
    size_prices: list[KreamSizePrice] = field(default_factory=list)
    volume_7d: int = 0
    volume_30d: int = 0
    last_trade_date: datetime | None = None
    price_trend: str = ""  # "상승", "하락", "보합"
    fetched_at: datetime = field(default_factory=datetime.now)


@dataclass
class RetailSizeInfo:
    """리테일 사이트 사이즈별 정보."""

    size: str
    price: int  # 할인 적용된 실구매가
    original_price: int = 0  # 정가
    in_stock: bool = True
    discount_type: str = ""  # "회원등급", "쿠폰", "세일" 등
    discount_rate: float = 0.0  # 할인율 (0~1)


@dataclass
class RetailProduct:
    """리테일(무신사 등) 상품 정보."""

    source: str  # "musinsa", "nike", "kasina" 등
    product_id: str
    name: str
    model_number: str
    brand: str = ""
    url: str = ""
    image_url: str = ""
    sizes: list[RetailSizeInfo] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)


@dataclass
class SizeProfitResult:
    """사이즈별 수익 계산 결과."""

    size: str
    retail_price: int  # 구매가
    kream_sell_price: int  # 크림 즉시판매가
    sell_fee: int  # 크림 판매 수수료 (부가세 포함)
    inspection_fee: int  # 검수비
    kream_shipping_fee: int  # 크림 배송비
    seller_shipping_fee: int  # 사업자 택배비
    total_cost: int  # 총 비용
    net_profit: int  # 순수익
    roi: float  # 수익률 (%)
    signal: Signal = Signal.NOT_RECOMMENDED
    in_stock: bool = True
    bid_count: int = 0


@dataclass
class ProfitOpportunity:
    """수익 기회 (알림 단위)."""

    kream_product: KreamProduct
    retail_products: list[RetailProduct]  # 같은 상품을 파는 여러 사이트
    size_profits: list[SizeProfitResult]
    best_profit: int = 0  # 최고 순수익
    best_roi: float = 0.0  # 최고 ROI
    signal: Signal = Signal.NOT_RECOMMENDED
    detected_at: datetime = field(default_factory=datetime.now)


@dataclass
class AutoScanSizeProfit:
    """자동스캔 사이즈별 2단계 수익 분석."""

    size: str
    musinsa_price: int  # 무신사 매입가
    # 1순위: 확정 수익 (즉시판매 = bid 기반)
    kream_bid_price: int | None = None  # 크림 즉시구매가 (= 내가 받는 가격)
    confirmed_profit: int = 0  # 확정 순수익
    confirmed_roi: float = 0.0  # 확정 ROI
    # 2순위: 예상 수익 (시세 기반 = 최근 체결가)
    kream_recent_price: int | None = None  # 최근 7일 평균 체결가
    estimated_profit: int = 0  # 예상 순수익
    estimated_roi: float = 0.0  # 예상 ROI
    # 보조 지표
    bid_count: int = 0  # 구매 입찰 수
    in_stock: bool = True


@dataclass
class AutoScanOpportunity:
    """자동스캔 수익 기회 (크림 기준 → 무신사 매입)."""

    kream_product: KreamProduct
    musinsa_url: str = ""
    musinsa_name: str = ""
    musinsa_product_id: str = ""
    size_profits: list[AutoScanSizeProfit] = field(default_factory=list)
    # 최고 확정 수익
    best_confirmed_profit: int = 0
    best_confirmed_roi: float = 0.0
    # 최고 예상 수익
    best_estimated_profit: int = 0
    best_estimated_roi: float = 0.0
    # 리스크 지표
    volume_7d: int = 0
    price_trend: str = ""  # 상승/하락/횡보
    bid_ask_spread: int = 0  # 즉시구매가-최근거래가 차이 (클수록 불안정)
    detected_at: datetime = field(default_factory=datetime.now)
