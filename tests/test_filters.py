"""경계값 단위 테스트 — 시그널/수수료/알림 임계값."""

import os
import tempfile

import pytest

from src.models.database import Database
from src.models.product import Signal
from src.profit_calculator import calculate_kream_fees, determine_signal


# === A. 시그널 경계값 ===


class TestSignalBoundary:
    """determine_signal() 경계값 — config.py SignalThresholds 기준."""

    # --- 거래량 게이트 (min_volume_7d = 3) ---

    def test_volume_0_always_not_recommended(self):
        """거래량 0 → 수익 무관 NOT_RECOMMENDED."""
        assert determine_signal(50_000, 0) == Signal.NOT_RECOMMENDED

    def test_volume_1_below_gate(self):
        assert determine_signal(50_000, 1) == Signal.NOT_RECOMMENDED

    def test_volume_2_below_gate(self):
        assert determine_signal(50_000, 2) == Signal.NOT_RECOMMENDED

    def test_volume_3_passes_gate(self):
        """거래량 3 = 게이트 통과, vol < 5 이므로 BUY는 불가."""
        assert determine_signal(50_000, 3) == Signal.WATCH

    # --- WATCH 경계 (watch_profit = 5,000) ---

    def test_profit_4999_not_recommended(self):
        assert determine_signal(4_999, 5) == Signal.NOT_RECOMMENDED

    def test_profit_5000_watch(self):
        """정확히 5,000₩ + vol 4 → WATCH (vol < 5이므로 BUY 불가)."""
        assert determine_signal(5_000, 4) == Signal.WATCH

    def test_profit_5001_watch(self):
        assert determine_signal(5_001, 4) == Signal.WATCH

    # --- BUY 경계 (buy_profit = 15,000, buy_volume_7d = 5) ---

    def test_profit_14999_watch(self):
        """14,999₩ + vol 5 → WATCH (수익 부족)."""
        assert determine_signal(14_999, 5) == Signal.WATCH

    def test_profit_15000_vol5_buy(self):
        """정확히 경계: 15,000₩ + vol 5 → BUY."""
        assert determine_signal(15_000, 5) == Signal.BUY

    def test_profit_15001_vol5_buy(self):
        assert determine_signal(15_001, 5) == Signal.BUY

    def test_profit_15000_vol4_watch(self):
        """수익 충분하나 거래량 부족 → BUY 아닌 WATCH."""
        assert determine_signal(15_000, 4) == Signal.WATCH

    # --- STRONG_BUY 경계 (strong_buy_profit = 30,000, volume = 10) ---

    def test_profit_29999_buy(self):
        """29,999₩ + vol 10 → BUY (수익 1원 부족)."""
        assert determine_signal(29_999, 10) == Signal.BUY

    def test_profit_30000_vol10_strong_buy(self):
        """정확히 경계: 30,000₩ + vol 10 → STRONG_BUY."""
        assert determine_signal(30_000, 10) == Signal.STRONG_BUY

    def test_profit_30001_vol10_strong_buy(self):
        assert determine_signal(30_001, 10) == Signal.STRONG_BUY

    def test_profit_30000_vol9_strong_buy(self):
        """수익 충분 + 거래량 ≥ 5 → STRONG_BUY."""
        assert determine_signal(30_000, 9) == Signal.STRONG_BUY

    def test_profit_30000_vol4_watch(self):
        """수익 충분하나 거래량 부족(< 5) → BUY 미달, WATCH."""
        assert determine_signal(30_000, 4) == Signal.WATCH


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
