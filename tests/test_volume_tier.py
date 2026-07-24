"""거래량 → 티어 중앙 함수 경계값 테스트 (조각 2-1)."""

from src.core.volume_tier import tier_for_volume


def test_none_is_unknown():
    """한 번도 조회 못 한 상품 — cold 로 단정 금지."""
    assert tier_for_volume(None) == "unknown"


def test_zero_is_cold():
    assert tier_for_volume(0) == "cold"


def test_one_is_warm():
    assert tier_for_volume(1) == "warm"


def test_four_is_warm():
    """숨은 보석 정책 — 1~4 는 zero 와 같은 cold 로 묶지 않는다."""
    assert tier_for_volume(4) == "warm"


def test_five_is_hot():
    assert tier_for_volume(5) == "hot"


def test_large_volume_is_hot():
    assert tier_for_volume(500) == "hot"


def test_negative_treated_as_unknown():
    """방어적 — 음수는 있을 수 없는 값이니 unknown 으로 취급 (cold 오판정 금지)."""
    assert tier_for_volume(-1) == "unknown"


def test_custom_hot_min_override():
    assert tier_for_volume(3, hot_min=3) == "hot"
    assert tier_for_volume(2, hot_min=3) == "warm"
