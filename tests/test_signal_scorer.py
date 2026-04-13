"""signal_scorer 단위 테스트 (Phase 4 스캐폴딩).

순수 함수 — DB/HTTP I/O 0.
"""

from __future__ import annotations

import time

from src.core.signal_scorer import (
    SaleRecord,
    ScorerInput,
    liquidity_score,
    passes_threshold,
    price_position_score,
    score,
    velocity_score,
    volatility_score,
)

# ─── liquidity ───────────────────────────────────────────


def test_liquidity_zero_when_no_sales():
    assert liquidity_score(0, 5) == 0.0


def test_liquidity_full_when_no_asks():
    assert liquidity_score(10, 0) == 1.0


def test_liquidity_normalizes_to_unit_interval():
    s = liquidity_score(20, 5)  # ratio=4
    assert 0.0 < s < 1.0
    assert abs(s - 4 / 5) < 1e-9


# ─── volatility ──────────────────────────────────────────


def test_volatility_neutral_with_sparse_data():
    assert volatility_score([SaleRecord(100_000, "270", 0)]) == 0.5


def test_volatility_high_score_for_stable_prices():
    sales = [SaleRecord(100_000 + i * 100, "270", 0) for i in range(10)]
    s = volatility_score(sales)
    assert s > 0.9


def test_volatility_low_score_for_wild_swings():
    sales = [
        SaleRecord(50_000, "270", 0),
        SaleRecord(150_000, "270", 0),
        SaleRecord(60_000, "270", 0),
        SaleRecord(180_000, "270", 0),
    ]
    s = volatility_score(sales)
    assert s < 0.4


# ─── velocity ────────────────────────────────────────────


def test_velocity_zero_with_one_sale():
    assert velocity_score([SaleRecord(100_000, "270", 0)]) == 0.0


def test_velocity_high_for_short_intervals():
    now = time.time()
    sales = [SaleRecord(100_000, "270", now - i * 600) for i in range(5)]  # 10분 간격
    s = velocity_score(sales)
    assert s > 0.9


def test_velocity_low_for_week_long_intervals():
    now = time.time()
    sales = [SaleRecord(100_000, "270", now - i * 86400 * 7) for i in range(5)]  # 7일 간격
    s = velocity_score(sales)
    assert s < 0.1


# ─── price_position ──────────────────────────────────────


def test_price_position_neutral_with_sparse_data():
    assert price_position_score(100_000, [SaleRecord(100_000, "270", 0)]) == 0.5


def test_price_position_full_when_below_p25():
    sales = [SaleRecord(100_000 + i * 1000, "270", 0) for i in range(20)]
    # p25 ≈ 105_000 → 90_000 은 그 이하
    assert price_position_score(90_000, sales) == 1.0


def test_price_position_zero_when_above_p75():
    sales = [SaleRecord(100_000 + i * 1000, "270", 0) for i in range(20)]
    assert price_position_score(200_000, sales) == 0.0


# ─── 합산 score ──────────────────────────────────────────


def test_score_returns_breakdown_with_weighted_total():
    now = time.time()
    sales_30d = [SaleRecord(100_000 + i * 500, "270", now - i * 3600) for i in range(20)]
    sales_7d = sales_30d[:7]
    inp = ScorerInput(
        kream_product_id=1,
        size="270",
        sell_now_price=99_000,
        asks_count=2,
        sales_30d=sales_30d,
        sales_7d=sales_7d,
    )
    b = score(inp)
    assert 0.0 <= b.liquidity <= 1.0
    assert 0.0 <= b.volatility <= 1.0
    assert 0.0 <= b.velocity <= 1.0
    assert 0.0 <= b.price_position <= 1.0
    assert 0.0 <= b.total <= 1.0
    # 강한 신호 → reasons 비어있지 않음
    assert b.reasons


def test_score_respects_custom_weights():
    inp = ScorerInput(
        kream_product_id=1,
        size="270",
        sell_now_price=100_000,
        asks_count=1,
        sales_30d=[SaleRecord(100_000, "270", 0)] * 10,
        sales_7d=[SaleRecord(100_000, "270", float(i)) for i in range(5)],
    )
    # liquidity 만 본다
    b = score(inp, weights={"liquidity": 1.0})
    assert b.total == b.liquidity


def test_passes_threshold_default():
    now = time.time()
    sales = [SaleRecord(100_000, "270", now - i * 1800) for i in range(20)]
    inp = ScorerInput(
        kream_product_id=1,
        size="270",
        sell_now_price=99_000,
        asks_count=2,
        sales_30d=sales,
        sales_7d=sales[:7],
    )
    b = score(inp)
    assert passes_threshold(b) is True


def test_passes_threshold_blocks_weak_signal():
    inp = ScorerInput(
        kream_product_id=1,
        size="270",
        sell_now_price=200_000,
        asks_count=100,
        sales_30d=[SaleRecord(100_000, "270", 0)],
        sales_7d=[],
    )
    b = score(inp)
    assert passes_threshold(b) is False
