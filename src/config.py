"""프로젝트 설정 관리."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


class KreamFees(BaseSettings):
    """크림 수수료 설정."""

    model_config = SettingsConfigDict(
        env_prefix="KREAM_FEE_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_fee: int = 2500  # 기본료 (원)
    sell_fee_rate: float = 0.06  # 등급 수수료율 6%
    vat_rate: float = 0.1  # 부가세 10%
    inspection_fee: int = 0  # 검수비 (무료)
    kream_shipping_fee: int = 0  # 크림 배송비 (무료)


class SignalThresholds(BaseSettings):
    """시그널 판정 기준."""

    strong_buy_profit: int = 30000  # 강력매수 순수익 기준
    strong_buy_volume_7d: int = 10  # 강력매수 7일 거래량 기준
    buy_profit: int = 15000  # 매수 순수익 기준
    buy_volume_7d: int = 5  # 매수 7일 거래량 기준
    watch_profit: int = 5000  # 관망 순수익 기준
    min_volume_7d: int = 3  # 최소 거래량 (미만이면 비추천)


class Settings(BaseSettings):
    """전체 설정."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Discord
    discord_token: str = ""
    channel_profit_alert: int = 0
    channel_price_change: int = 0
    channel_daily_report: int = 0
    channel_manual_search: int = 0
    channel_settings: int = 0
    channel_log: int = 0
    channel_match_review: int = 0  # 매칭 애매한 경우 검토용 채널

    # Chrome CDP
    chrome_path: str = "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
    chrome_debug_port: int = 9222
    chrome_user_data_dir: str = (
        ""
    )

    # 크림 로그인
    kream_email: str = ""
    kream_password: str = ""

    # 배송비
    shipping_cost_to_kream: int = 3000  # 사업자 택배비

    # 크롤링
    use_cdp_login: bool = False  # CDP 브라우저 로그인 사용 여부 (False면 pinia 전용 모드)
    request_delay_min: float = 1.5  # 최소 딜레이 (초)
    request_delay_max: float = 4.0  # 최대 딜레이 (초)
    scan_interval_minutes: int = 30  # 기본 스캔 주기
    fast_scan_interval_minutes: int = 10  # 수익 상품 집중 추적 주기

    # 수수료
    fees: KreamFees = Field(default_factory=KreamFees)

    # 시그널
    signals: SignalThresholds = Field(default_factory=SignalThresholds)

    # DB
    db_path: str = str(DATA_DIR / "kream_bot.db")

    # 자동스캔 설정
    auto_scan_interval_minutes: int = 30  # 자동스캔 주기
    auto_scan_confirmed_roi: float = 5.0  # 확정 수익 ROI 기준 (%)
    auto_scan_estimated_roi: float = 10.0  # 예상 수익 ROI 기준 (%)
    auto_scan_max_products: int = 100  # 1회 스캔 최대 상품 수
    auto_scan_concurrency: int = 3  # 동시 요청 수
    auto_scan_cache_minutes: int = 60  # 캐시 유효 시간 (분)


settings = Settings()
