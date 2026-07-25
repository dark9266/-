"""quarantine(404/410) 재시도 시각 namespace 결정적 지터 (2026-07-25).

## 왜
`datetime('now', '+90 days')` 고정이라 **지터가 없었다**. 같은 장애 파동에서 격리된
상품들이 90일 뒤 **같은 날 한꺼번에** 되살아나 하루짜리 재시도 절벽을 만든다.
정기 재검사(`next_volume_check_at`)에는 ±10% 지터가 있는데 여기만 빠져 있었다.

지금 고치는 이유: DB 의 quarantine 행이 **아직 0건**이다. 복구가 진행되면 404/410 이
쌓이기 시작하므로, 부채가 생기기 전에 넣어야 한다.

## 계약 (코덱스 P2 브리프)
- 기본 90일 ± 10% → 81~99일
- 랜덤 금지, 결정적
- 해시 namespace 를 정기 재검사와 **분리** (`quarantine:v1:{product_id}:{error_class}`)
- 기준시각은 실행시각이 아니라 `last_volume_attempt_at` → 재실행해도 예정일 불변
- `next_volume_attempt_at` 만 바꾼다. `next_volume_check_at`·`volume_7d`·tier 불변
- 404/410 외 retryable 백오프는 안 건드린다
- 크림 호출 0건
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.volume_tier import (
    QUARANTINE_BASE_DAYS,
    compute_next_volume_check_at,
    compute_quarantine_until,
    jitter_offset_seconds,
    retry_backoff,
)

BASE = datetime(2026, 7, 25, 5, 0, 0, tzinfo=UTC)


def _days(product_id: str, error_class: str = "http_404", at: datetime = BASE) -> float:
    return (compute_quarantine_until(product_id, error_class, at) - at).total_seconds() / 86400


# ---------------------------------------------------------------------------
# 1. 결정성 — 랜덤 금지
# ---------------------------------------------------------------------------


def test_same_input_is_identical_across_repeated_calls():
    first = compute_quarantine_until("P1", "http_404", BASE)
    for _ in range(100):
        assert compute_quarantine_until("P1", "http_404", BASE) == first


def test_recomputation_after_restart_gives_same_date():
    """기준시각이 실행시각이 아니라 attempted_at 이라 재실행해도 안 움직인다."""
    stored_attempt = BASE
    day1 = compute_quarantine_until("P1", "http_404", stored_attempt)
    # 며칠 뒤 다시 계산해도(=현재시각이 달라져도) 같은 값이어야 한다
    day2 = compute_quarantine_until("P1", "http_404", stored_attempt)
    assert day1 == day2


def test_shifting_attempted_at_shifts_result_by_same_amount():
    a = compute_quarantine_until("P1", "http_404", BASE)
    b = compute_quarantine_until("P1", "http_404", BASE + timedelta(days=1))
    assert b - a == timedelta(days=1)


# ---------------------------------------------------------------------------
# 2. 범위 — 81~99일
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "product_id",
    ["P1", "P2", "249897", "140265", "818996", "608430", "515349", "47058", "", "한글아이디"],
)
def test_result_stays_within_ten_percent_band(product_id):
    d = _days(product_id)
    assert 81.0 <= d <= 99.0, f"{product_id} -> {d}일"


def test_band_edges_are_reachable_but_not_exceeded():
    """1,000개 표본으로 상·하한 초과가 없는지 (해시 분포 확인)."""
    values = [_days(f"P{i}") for i in range(1000)]
    assert min(values) >= 81.0
    assert max(values) <= 99.0
    # 실제로 퍼지는지 — 전부 같은 값이면 지터가 죽은 것이다
    assert max(values) - min(values) > 10.0


# ---------------------------------------------------------------------------
# 3. 분산 — 같은 날 몰리지 않는다 (이 조각의 존재 이유)
# ---------------------------------------------------------------------------


def test_cohort_quarantined_together_does_not_return_on_one_day():
    """같은 순간 격리된 1,000건이 90일 뒤 하루에 몰리면 안 된다."""
    dates = {compute_quarantine_until(f"P{i}", "http_404", BASE).date() for i in range(1000)}
    assert len(dates) >= 15, f"{len(dates)}일에만 퍼졌다 — 절벽이 그대로다"


def test_404_and_410_do_not_collide():
    """두 오류 종류가 같은 날 함께 풀리지 않게 축을 분리한다."""
    assert compute_quarantine_until("P1", "http_404", BASE) != compute_quarantine_until(
        "P1", "http_410", BASE
    )


def test_namespace_is_separated_from_regular_recheck_jitter():
    """같은 product_id 의 정기 재검사 지터와 quarantine 지터가 상관되면 안 된다.

    같은 해시를 재사용하면 한 상품의 두 일정이 항상 같은 달력 위상에 놓인다.
    """
    base_seconds = float(QUARANTINE_BASE_DAYS) * 86400.0
    regular = jitter_offset_seconds("P1", base_seconds)
    quarantine = (
        compute_quarantine_until("P1", "http_404", BASE) - BASE
    ).total_seconds() - base_seconds
    assert abs(regular - quarantine) > 1.0


# ---------------------------------------------------------------------------
# 4. 다른 축 비파괴
# ---------------------------------------------------------------------------


def test_regular_recheck_matrix_is_unchanged():
    """정기 재검사 TTL 매트릭스는 이 변경과 무관해야 한다."""
    at = compute_next_volume_check_at("P1", 0, has_retail_match=True, now=BASE)
    days = (at - BASE).total_seconds() / 86400
    assert 27.0 <= days <= 33.0  # 30일 ±10%


def test_retryable_backoff_is_unchanged():
    """404/410 이 아닌 재시도 백오프는 안 건드린다."""
    assert retry_backoff(1) == timedelta(hours=6)
    assert retry_backoff(2) == timedelta(hours=24)
    assert retry_backoff(3) == timedelta(days=7)


def test_base_days_default_is_ninety():
    assert QUARANTINE_BASE_DAYS == 90


def test_custom_base_days_scales_the_band():
    """상한을 낮춰 운용할 때도 ±10% 비율은 유지된다."""
    at = compute_quarantine_until("P1", "http_404", BASE, base_days=10)
    days = (at - BASE).total_seconds() / 86400
    assert 9.0 <= days <= 11.0
