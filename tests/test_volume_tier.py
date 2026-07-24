"""거래량 → 티어 중앙 함수 경계값 테스트 (조각 2-1) + cold 재검사 정책 (조각 2-3)."""

from datetime import datetime, timedelta

from src.core.volume_tier import (
    bump_check_at,
    compute_next_volume_check_at,
    jitter_offset_seconds,
    next_check_interval,
    retry_backoff,
    tier_for_volume,
)


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


# ---------------------------------------------------------------------------
# 조각 2-3: TTL 매트릭스 (next_check_interval) 경계값
# ---------------------------------------------------------------------------


def test_next_check_interval_none_is_immediate():
    """미확인(None) — 즉시(부트스트랩 대상), 지터 없이 timedelta(0)."""
    assert next_check_interval(None, has_retail_match=True) == timedelta(0)


def test_next_check_interval_zero_with_retail_match_is_30_days():
    assert next_check_interval(0, has_retail_match=True) == timedelta(days=30)


def test_next_check_interval_zero_without_retail_match_is_90_days():
    assert next_check_interval(0, has_retail_match=False) == timedelta(days=90)


def test_next_check_interval_one_is_seven_days():
    assert next_check_interval(1, has_retail_match=True) == timedelta(days=7)


def test_next_check_interval_four_is_seven_days():
    """1~4 는 zero 와 같은 cold 로 묶지 않는다 — 숨은 보석 정책과 동일한 정신."""
    assert next_check_interval(4, has_retail_match=True) == timedelta(days=7)


def test_next_check_interval_five_is_three_days():
    assert next_check_interval(5, has_retail_match=True) == timedelta(days=3)


def test_next_check_interval_large_volume_is_three_days():
    assert next_check_interval(500, has_retail_match=True) == timedelta(days=3)


# ---------------------------------------------------------------------------
# 조각 2-3: 결정적 지터 (랜덤 금지 — product_id 해시 기반, 재현 가능)
# ---------------------------------------------------------------------------


def test_jitter_offset_deterministic_same_product_id():
    base = 7 * 86400
    a = jitter_offset_seconds("P123", base)
    b = jitter_offset_seconds("P123", base)
    assert a == b


def test_jitter_offset_within_ten_percent_range():
    base = 7 * 86400
    offset = jitter_offset_seconds("P123", base)
    assert abs(offset) <= base * 0.10


def test_jitter_offset_differs_across_product_ids():
    base = 7 * 86400
    a = jitter_offset_seconds("P1", base)
    b = jitter_offset_seconds("P999999", base)
    assert a != b


def test_jitter_offset_zero_base_is_zero():
    assert jitter_offset_seconds("P1", 0) == 0


# ---------------------------------------------------------------------------
# 조각 2-3: compute_next_volume_check_at — 간격 + 지터 결합, 결정적
# ---------------------------------------------------------------------------


def test_compute_next_volume_check_at_applies_interval_within_jitter_band():
    now = datetime(2026, 1, 1)
    due = compute_next_volume_check_at("P1", 0, has_retail_match=True, now=now)
    delta_days = (due - now).total_seconds() / 86400
    assert 27 <= delta_days <= 33  # 30일 ±10%


def test_compute_next_volume_check_at_none_volume_is_immediate_now():
    now = datetime(2026, 1, 1)
    due = compute_next_volume_check_at("P1", None, has_retail_match=True, now=now)
    assert due == now


def test_compute_next_volume_check_at_deterministic_for_same_product():
    now = datetime(2026, 1, 1)
    due1 = compute_next_volume_check_at("P1", 1, has_retail_match=True, now=now)
    due2 = compute_next_volume_check_at("P1", 1, has_retail_match=True, now=now)
    assert due1 == due2


# ---------------------------------------------------------------------------
# 조각 2-3: retry_backoff — retryable 재시도 간격 매트릭스 (2-1 임시 6h 고정 대체)
# ---------------------------------------------------------------------------


def test_retry_backoff_first_attempt_is_six_hours():
    assert retry_backoff(1) == timedelta(hours=6)


def test_retry_backoff_second_attempt_is_24_hours():
    assert retry_backoff(2) == timedelta(hours=24)


def test_retry_backoff_third_attempt_is_seven_days():
    assert retry_backoff(3) == timedelta(days=7)


def test_retry_backoff_beyond_third_stays_seven_days():
    assert retry_backoff(10) == timedelta(days=7)


def test_retry_backoff_zero_or_negative_defaults_to_first():
    assert retry_backoff(0) == timedelta(hours=6)
    assert retry_backoff(-1) == timedelta(hours=6)


# ---------------------------------------------------------------------------
# 조각 2-3: bump_check_at — 이벤트 앞당김 인터페이스 (배선은 스코프 밖)
# ---------------------------------------------------------------------------


def test_bump_check_at_none_current_uses_candidate():
    candidate = datetime(2026, 2, 1)
    assert bump_check_at(None, candidate) == candidate


def test_bump_check_at_earlier_candidate_wins():
    current = datetime(2026, 3, 1)
    candidate = datetime(2026, 2, 1)
    assert bump_check_at(current, candidate) == candidate


def test_bump_check_at_does_not_push_later():
    """이벤트가 더 늦은 날짜를 제안해도 뒤로 미루지 않는다 — 앞당김 전용."""
    current = datetime(2026, 2, 1)
    candidate = datetime(2026, 3, 1)
    assert bump_check_at(current, candidate) == current
