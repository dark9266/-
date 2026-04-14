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
    strong_buy_volume_7d: int = 5  # 강력매수 7일 거래량 기준
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
    channel_progress: int = 0  # 스캔 진행상황 알림 채널
    channel_match_review: int = 0  # 매칭 애매한 경우 검토용 채널

    # Discord 웹훅 — 커밋/작업 알림 (사람이 보는 작업 로그 채널)
    discord_notify_webhook: str = ""
    # Discord 웹훅 — v3 수익 알림 전용 (브릿지 v3_discord_publisher).
    # 미설정 시 publisher no-op → 파일 로그만. 작업 채널과 분리 필수.
    discord_profit_webhook: str = ""

    # 배송비
    shipping_cost_to_kream: int = 3000  # 사업자 택배비

    # 크롤링
    request_delay_min: float = 1.0  # 최소 딜레이 (초)
    request_delay_max: float = 2.0  # 최대 딜레이 (초)
    httpx_concurrency: int = 10  # httpx 동시 요청 수
    musinsa_min_interval: float = 0.5  # 무신사 요청 간 최소 간격 (초)
    hoka_proxy_url: str = ""  # 호카 DataDome 우회용 프록시 (예: http://user:pass@host:port)
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
    auto_scan_concurrency: int = 10  # 동시 요청 수
    auto_scan_cache_minutes: int = 60  # 캐시 유효 시간 (분)

    # 2티어 실시간 아키텍처
    tier1_interval_minutes: int = 30  # 워치리스트 빌더 주기
    tier2_interval_seconds: int = 60  # 실시간 폴링 주기
    watchlist_gap_threshold: int = -20_000  # 워치리스트 추가 기준 (gap > 이 값)
    watchlist_max_age_hours: int = 48  # 워치리스트 항목 최대 유지 시간

    # 실시간 DB
    realtime_collect_interval_hours: int = 6       # 신규 상품 수집 주기
    realtime_hot_refresh_minutes: int = 30         # hot 상품 시세 갱신 주기
    realtime_cold_refresh_hours: int = 6           # cold 상품 시세 갱신 주기
    realtime_volume_check_minutes: int = 60        # 거래량 체크 주기
    realtime_spike_threshold: float = 2.0          # 거래량 급등 판정 배율 (이전 대비)
    realtime_hot_volume_min: int = 5               # hot tier 최소 7일 거래량
    realtime_collect_pages_per_keyword: int = 5    # 수집 시 키워드당 페이지 수
    realtime_refresh_batch_size: int = 50          # 1회 시세 갱신 배치 크기

    # v2 연속 배치 스캔
    continuous_scan_interval_minutes: int = 5      # 배치 주기
    continuous_scan_batch_size: int = 20           # 1회 배치 크기 (크림 API 부담 최소화)
    continuous_hot_ttl_hours: int = 2              # hot 재스캔 주기
    continuous_warm_ttl_hours: int = 8             # warm 재스캔 주기
    continuous_cold_ttl_hours: int = 48            # cold 재스캔 주기

    # 알림 최소 기준 (하드 플로어)
    alert_min_profit: int = 10_000  # 최소 순수익 (원)
    alert_min_roi: float = 5.0  # 최소 ROI (%)
    alert_min_volume_7d: int = 1  # 최소 7일 거래량

    # v2 reverse_scanner 비활성 (정확도 사고 격리 — 2026-04-13)
    # Nike MFS 미작동 / LAUNCH 필터 reverse 누락 / 웍스아웃 이름 오매칭 수정 전까지 OFF.
    v2_reverse_disabled: bool = False

    # v3 런타임 (Phase 2.6) — 기본 OFF. 기존 v2 루프는 그대로 유지.
    # 실사용자가 `.env` 에 `V3_RUNTIME_ENABLED=true` 로 수동 ON 해야 기동된다.
    v3_runtime_enabled: bool = False
    v3_musinsa_interval_sec: int = 1800  # 무신사 어댑터 사이클 간격 (30분)
    v3_adapter_interval_sec: int = 1800  # v3 어댑터 공통 사이클 간격 (30분)
    v3_adapter_stagger_sec: int = 30  # 어댑터 기동 stagger (21개 × 30s = 10.5분)
    v3_hot_poll_interval_sec: int = 300  # 크림 hot/delta 감시 폴링 간격
    # 일일 캡(10,000) 대비 안전 범위:
    # candidate: 3/min × 60 × 24 = 4,320
    # delta_light: 180/h × 24 ≈ 4,320 (300s 폴링, 호출 ~15건/poll)
    # 합계 ≈ 8,640/일, 여유 1,360 으로 recover/수동 호출 흡수.
    v3_throttle_rate_per_min: float = 3.0
    v3_throttle_burst: int = 6
    v3_alert_log_path: str = str(LOGS_DIR / "v3_alerts.jsonl")
    # 드라이런: 크림 호출 0건. snapshot_fn / delta_client 미주입 →
    # 어댑터 덤프·매칭·이벤트버스만 검증. 캡 회복 전 안전 검증용.
    v3_kream_dry_run: bool = False


settings = Settings()
