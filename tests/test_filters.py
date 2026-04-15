"""경계값 단위 테스트 — 시그널/수수료/알림 임계값."""

import os
import tempfile

import pytest

from src.models.database import Database
from src.models.product import Signal
from src.profit_calculator import calculate_kream_fees, determine_signal


# === A. 시그널 경계값 ===


class TestSignalBoundary:
    """determine_signal() 경계값 — config.py SignalThresholds 기준.

    숨은 보석 정책 (2026-04-15): 거래량 게이트를 1로 낮춰 vol 1~4 저거래 상품도
    알림 후보에 포함. vol 0 만 제외(대기 매수자 0 → 판매 불가). 거짓 알림 방어는
    순수익/ROI 하드 플로어가 담당.
    """

    # --- 거래량 게이트 (min_volume_7d = 1) ---

    def test_volume_0_always_not_recommended(self):
        """거래량 0 → 대기 매수자 없어 판매 불가 → NOT_RECOMMENDED."""
        assert determine_signal(50_000, 0) == Signal.NOT_RECOMMENDED

    def test_volume_1_strong_buy(self):
        """거래량 1 + 수익 50k → STRONG_BUY (숨은 보석 허용)."""
        assert determine_signal(50_000, 1) == Signal.STRONG_BUY

    def test_volume_2_buy(self):
        """거래량 2 + 수익 20k → BUY."""
        assert determine_signal(20_000, 2) == Signal.BUY

    def test_volume_3_watch(self):
        """거래량 3 + 수익 10k → WATCH (수익 < 15k)."""
        assert determine_signal(10_000, 3) == Signal.WATCH

    # --- WATCH 경계 (watch_profit = 5,000) ---

    def test_profit_4999_not_recommended(self):
        assert determine_signal(4_999, 5) == Signal.NOT_RECOMMENDED

    def test_profit_5000_watch(self):
        """정확히 5,000원 + vol 1 → WATCH."""
        assert determine_signal(5_000, 1) == Signal.WATCH

    def test_profit_5001_watch(self):
        assert determine_signal(5_001, 2) == Signal.WATCH

    # --- BUY 경계 (buy_profit = 15,000, buy_volume_7d = 1) ---

    def test_profit_14999_watch(self):
        """14,999원 → BUY 경계 1원 미달 → WATCH."""
        assert determine_signal(14_999, 5) == Signal.WATCH

    def test_profit_15000_vol1_buy(self):
        """정확히 경계: 15,000원 + vol 1 → BUY (숨은 보석)."""
        assert determine_signal(15_000, 1) == Signal.BUY

    def test_profit_15001_vol1_buy(self):
        assert determine_signal(15_001, 1) == Signal.BUY

    def test_profit_15000_vol0_not_recommended(self):
        """거래량 0 → 수익 무관 NOT_RECOMMENDED."""
        assert determine_signal(15_000, 0) == Signal.NOT_RECOMMENDED

    # --- STRONG_BUY 경계 (strong_buy_profit = 30,000, strong_buy_volume = 1) ---

    def test_profit_29999_buy(self):
        """29,999원 → STRONG_BUY 경계 1원 미달 → BUY."""
        assert determine_signal(29_999, 10) == Signal.BUY

    def test_profit_30000_vol1_strong_buy(self):
        """정확히 경계: 30,000원 + vol 1 → STRONG_BUY (숨은 보석)."""
        assert determine_signal(30_000, 1) == Signal.STRONG_BUY

    def test_profit_30001_vol1_strong_buy(self):
        assert determine_signal(30_001, 1) == Signal.STRONG_BUY

    def test_profit_30000_vol10_strong_buy(self):
        """거래량 많은 메이저 상품 그대로 STRONG_BUY."""
        assert determine_signal(30_000, 10) == Signal.STRONG_BUY

    def test_profit_30000_vol0_not_recommended(self):
        """거래량 0 → 판매 불가 → NOT_RECOMMENDED."""
        assert determine_signal(30_000, 0) == Signal.NOT_RECOMMENDED


# === B. 수수료 계산 경계값 ===


class TestFeeBoundary:
    def test_zero_price_fee(self):
        """0원 판매 → 기본료만 적용."""
        result = calculate_kream_fees(0)
        assert result["sell_fee"] == 2_750  # ceil(2500 * 1.1)
        assert result["total_fees"] == 2_750 + 3_000

    def test_1won_price(self):
        """1원 판매 → 거의 기본료."""
        result = calculate_kream_fees(1)
        # round((2500 + 1*0.06) * 1.1) = round(2750.066) = 2750
        assert result["sell_fee"] == 2_750


# === C. 알림 발송 게이트 (BUY/STRONG_BUY만 발송) ===


class TestAlertSendingGate:
    """scanner.py 알림은 STRONG_BUY/BUY만 발송."""

    def test_not_recommended_no_alert(self):
        signal = determine_signal(3_000, 10)
        assert signal == Signal.NOT_RECOMMENDED
        assert signal not in (Signal.STRONG_BUY, Signal.BUY)

    def test_watch_no_alert(self):
        signal = determine_signal(8_000, 4)
        assert signal == Signal.WATCH
        assert signal not in (Signal.STRONG_BUY, Signal.BUY)

    def test_buy_sends_alert(self):
        signal = determine_signal(15_000, 5)
        assert signal == Signal.BUY
        assert signal in (Signal.STRONG_BUY, Signal.BUY)

    def test_strong_buy_sends_alert(self):
        signal = determine_signal(30_000, 10)
        assert signal == Signal.STRONG_BUY
        assert signal in (Signal.STRONG_BUY, Signal.BUY)

    def test_profit_954_no_alert(self):
        """실제 버그 재현: +954₩ ROI 0.7% → 절대 알림 불가."""
        signal = determine_signal(954, 10)
        assert signal == Signal.NOT_RECOMMENDED
        assert signal not in (Signal.STRONG_BUY, Signal.BUY)


# === D. 알림 쿨다운/중복방지 ===


@pytest.fixture
async def db():
    """테스트용 임시 DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path=db_path)
    await database.connect()
    yield database
    await database.close()
    os.unlink(db_path)


class TestAlertDedup:
    """should_send_alert() 중복 방지 경계값."""

    async def test_first_alert_always_sends(self, db: Database):
        result = await db.should_send_alert("product1", "profit", "매수", 20_000)
        assert result is True

    async def test_same_signal_within_cooldown_blocked(self, db: Database):
        await db.save_alert("product1", "profit", best_profit=20_000, signal="매수")
        result = await db.should_send_alert("product1", "profit", "매수", 20_000)
        assert result is False

    async def test_signal_upgrade_sends(self, db: Database):
        await db.save_alert("product1", "profit", best_profit=8_000, signal="관망")
        result = await db.should_send_alert("product1", "profit", "매수", 20_000)
        assert result is True

    async def test_profit_20pct_increase_sends(self, db: Database):
        await db.save_alert("product1", "profit", best_profit=20_000, signal="매수")
        result = await db.should_send_alert("product1", "profit", "매수", 24_001)
        assert result is True

    async def test_profit_19pct_increase_blocked(self, db: Database):
        await db.save_alert("product1", "profit", best_profit=20_000, signal="매수")
        result = await db.should_send_alert("product1", "profit", "매수", 23_999)
        assert result is False


# === E. LIKE 검색 오매칭 ===


class TestLikeSearchBoundary:
    """search_kream_by_model_like 슬래시 전용 LIKE 검색."""

    async def test_short_model_no_false_positive(self, db: Database):
        """25-002가 CU9225-002를 매칭하지 않아야 함 (수정 완료)."""
        await db.upsert_kream_product(
            product_id="CU9225",
            name="나이키 에어포스 1",
            model_number="CU9225-002",
            brand="Nike",
        )
        row = await db.search_kream_by_model_like("25-002")
        assert row is None  # 슬래시 전용으로 차단됨

    async def test_slash_model_like_match(self, db: Database):
        """슬래시 구분 모델번호는 정상 매칭."""
        await db.upsert_kream_product(
            product_id="AF1",
            name="나이키 AF1",
            model_number="315122-111/CW2288-111",
            brand="Nike",
        )
        row = await db.search_kream_by_model_like("CW2288-111")
        assert row is not None
        assert row["product_id"] == "AF1"

    async def test_like_min_length_guard(self, db: Database):
        """5자 이하 모델번호는 LIKE 검색 차단."""
        row = await db.search_kream_by_model_like("AB-1")
        assert row is None
