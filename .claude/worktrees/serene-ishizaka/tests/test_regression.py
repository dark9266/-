"""회귀 테스트 — tests/fixtures/false_positives.json 기반.

새 버그 발견 시 fixture에 케이스만 추가하면 자동 회귀 테스트 확장.
"""

import json
from pathlib import Path

import pytest

from src.matcher import model_numbers_match
from src.models.product import Signal
from src.profit_calculator import determine_signal

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "false_positives.json"


@pytest.fixture
def cases():
    data = json.loads(FIXTURES_PATH.read_text())
    return data["cases"]


def _get_case(cases, case_id):
    """fixture에서 특정 케이스 조회."""
    return next(c for c in cases if c["id"] == case_id)


class TestRegression:
    def test_fixture_file_exists(self):
        assert FIXTURES_PATH.exists(), f"{FIXTURES_PATH} 파일 없음"

    def test_fixture_not_empty(self, cases):
        assert len(cases) > 0, "fixture 케이스가 비어있음"

    # --- 시그널 회귀 테스트 (type=signal) ---

    def test_low_profit_alert_blocked(self, cases):
        """+954₩ ROI 0.7% → 비추천, 알림 차단."""
        case = _get_case(cases, "low_profit_alert_sent")
        inp = case["input"]
        signal = determine_signal(inp["net_profit"], inp["volume_7d"])
        assert signal.value == case["expected_signal"]
        assert (signal in (Signal.STRONG_BUY, Signal.BUY)) == case["expected_alert"]

    def test_zero_volume_alert_blocked(self, cases):
        """거래량 0건 → 비추천, 알림 차단."""
        case = _get_case(cases, "zero_volume_alert")
        inp = case["input"]
        signal = determine_signal(inp["net_profit"], inp["volume_7d"])
        assert signal.value == case["expected_signal"]
        assert (signal in (Signal.STRONG_BUY, Signal.BUY)) == case["expected_alert"]

    # --- 모델번호 회귀 테스트 (type=model_match) ---

    def test_like_short_model_exact_match_blocked(self, cases):
        """25-002 ≠ CU9225-002: model_numbers_match는 정확 비교로 올바르게 차단."""
        case = _get_case(cases, "like_short_model_match")
        inp = case["input"]
        assert model_numbers_match(inp["retail_model"], inp["kream_model"]) is False

    # --- 전체 signal 케이스 자동 순회 ---

    def test_all_signal_cases(self, cases):
        """fixture 내 모든 signal 타입 케이스를 자동 검증."""
        signal_cases = [c for c in cases if c["type"] == "signal"]
        for case in signal_cases:
            inp = case["input"]
            signal = determine_signal(inp["net_profit"], inp["volume_7d"])
            assert signal.value == case["expected_signal"], (
                f"[{case['id']}] {case['description']}: "
                f"expected={case['expected_signal']}, got={signal.value}"
            )
            assert (signal in (Signal.STRONG_BUY, Signal.BUY)) == case["expected_alert"], (
                f"[{case['id']}] alert expected={case['expected_alert']}"
            )
