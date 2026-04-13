"""signal_scorer — Phase 4 축 7 지능 필터 (순수 함수 단계).

설계 메모: docs/phase4_design.md

목적:
    ProfitFound 이벤트 + 크림 거래 내역을 입력으로 받아 알림 신호 품질 점수를
    산출. 알림 단계에서 거짓/저질 신호 추가 차단 → 정확도 하드 제약 보강.

이번 단계 (Phase 3 말):
    - 순수 함수만. 외부 I/O 0 — 입력 dataclass/dict 만 받는다.
    - 거래 내역 어댑터(KreamSalesClient)와 Orchestrator 통합은 Phase 4 진입 시
      signal-scorer 에이전트가 이 모듈 위에 빌드한다.

테스트:
    tests/test_signal_scorer.py — 입력 fixture 로 각 축 + 합산 점수 검증.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

# ─── 입력 타입 ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SaleRecord:
    """크림 거래 내역 1행 — sales API 응답을 정규화한 형태."""

    price: int
    size: str
    traded_at: float  # epoch sec


@dataclass(frozen=True, slots=True)
class ScorerInput:
    """signal_scorer 의 단일 입력 단위.

    KreamSalesClient + 현재 매물 정보를 합쳐서 만든다 (Phase 4 진입 시).
    """

    kream_product_id: int
    size: str
    sell_now_price: int
    asks_count: int  # 현재 매물 수 (size 한정 가능, 없으면 전 사이즈 합)
    sales_30d: Sequence[SaleRecord]
    sales_7d: Sequence[SaleRecord]


# ─── 점수 결과 ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """축별 0~1 정규화 점수 + 합산."""

    liquidity: float  # 유동성 깊이
    volatility: float  # 가격 안정성 (낮을수록 ↑)
    velocity: float  # 체결 속도
    price_position: float  # 진입 여유
    total: float  # 가중 합산 (0~1)
    reasons: tuple[str, ...]  # 사람이 읽을 근거


# 기본 가중치 — Phase 4.2 에서 outcome 피드백으로 자동 튜닝
DEFAULT_WEIGHTS: dict[str, float] = {
    "liquidity": 0.35,
    "volatility": 0.20,
    "velocity": 0.25,
    "price_position": 0.20,
}


# ─── 개별 축 ─────────────────────────────────────────────


def liquidity_score(sales_30d_count: int, asks_count: int) -> float:
    """유동성 깊이 — 30일 거래수 / 현재 매물수.

    1.0 초과 = 수요 > 공급. 정규화: ratio / (ratio + 1) 로 0~1 매핑.
    asks=0 (매물 없음) → 1.0 만점 (수요만 있고 공급 0).
    """
    if sales_30d_count <= 0:
        return 0.0
    if asks_count <= 0:
        return 1.0
    ratio = sales_30d_count / asks_count
    return ratio / (ratio + 1.0)


def volatility_score(sales_7d: Sequence[SaleRecord]) -> float:
    """가격 안정성 — 7일 stdev/mean 의 역수 정규화.

    데이터 < 2건 → 0.5 (중립).
    cv (변동계수) 0 → 1.0, cv 0.2 → ~0.5, cv 1.0 → ~0.09.
    """
    prices = [s.price for s in sales_7d if s.price > 0]
    if len(prices) < 2:
        return 0.5
    mean = statistics.fmean(prices)
    if mean <= 0:
        return 0.0
    cv = statistics.stdev(prices) / mean
    return 1.0 / (1.0 + cv * 5.0)


def velocity_score(sales_recent: Sequence[SaleRecord]) -> float:
    """체결 속도 — 최근 거래 간격 중앙값 (작을수록 점수 ↑).

    데이터 < 2건 → 0.0.
    1시간 이내 → 1.0, 24시간 → ~0.5, 7일+ → ~0.05.
    """
    if len(sales_recent) < 2:
        return 0.0
    sorted_ts = sorted(s.traded_at for s in sales_recent)
    intervals = [b - a for a, b in zip(sorted_ts, sorted_ts[1:]) if b > a]
    if not intervals:
        return 0.0
    median_interval = statistics.median(intervals)
    hours = max(median_interval / 3600.0, 0.0)
    return 1.0 / (1.0 + hours / 6.0)


def price_position_score(sell_now_price: int, sales_30d: Sequence[SaleRecord]) -> float:
    """가격 위치 — sell_now 가 30일 가격 분포에서 어디인지.

    P25 이하 = 1.0, P75 이상 = 0.0, 중간 선형.
    데이터 < 4건 → 0.5 (중립).
    """
    prices = sorted(s.price for s in sales_30d if s.price > 0)
    if len(prices) < 4 or sell_now_price <= 0:
        return 0.5
    p25 = prices[len(prices) // 4]
    p75 = prices[len(prices) * 3 // 4]
    if p75 <= p25:
        return 0.5
    if sell_now_price <= p25:
        return 1.0
    if sell_now_price >= p75:
        return 0.0
    return 1.0 - (sell_now_price - p25) / (p75 - p25)


# ─── 합산 ────────────────────────────────────────────────


def score(inp: ScorerInput, *, weights: dict[str, float] | None = None) -> ScoreBreakdown:
    """입력으로부터 ScoreBreakdown 산출.

    weights 미지정 시 DEFAULT_WEIGHTS 사용.
    """
    w = weights or DEFAULT_WEIGHTS
    liq = liquidity_score(len(inp.sales_30d), inp.asks_count)
    vol = volatility_score(inp.sales_7d)
    vel = velocity_score(inp.sales_7d)
    pos = price_position_score(inp.sell_now_price, inp.sales_30d)
    total = (
        liq * w.get("liquidity", 0.0)
        + vol * w.get("volatility", 0.0)
        + vel * w.get("velocity", 0.0)
        + pos * w.get("price_position", 0.0)
    )
    reasons: list[str] = []
    if liq >= 0.7:
        reasons.append(f"유동성↑ ({len(inp.sales_30d)}건/{inp.asks_count}매물)")
    if vol >= 0.7:
        reasons.append("가격 안정")
    elif vol <= 0.3:
        reasons.append("가격 변동 큼")
    if vel >= 0.7:
        reasons.append("체결 빠름")
    if pos >= 0.7:
        reasons.append("저가 진입 여유")
    elif pos <= 0.3:
        reasons.append("고가 구간")
    return ScoreBreakdown(
        liquidity=round(liq, 4),
        volatility=round(vol, 4),
        velocity=round(vel, 4),
        price_position=round(pos, 4),
        total=round(total, 4),
        reasons=tuple(reasons),
    )


def passes_threshold(breakdown: ScoreBreakdown, *, min_total: float = 0.45) -> bool:
    """알림 게이트 — total 이 임계 이상이면 True.

    Phase 4 진입 시 outcome 기반 캘리브레이션으로 임계값 자동 조정.
    """
    return breakdown.total >= min_total


__all__ = [
    "DEFAULT_WEIGHTS",
    "SaleRecord",
    "ScoreBreakdown",
    "ScorerInput",
    "liquidity_score",
    "passes_threshold",
    "price_position_score",
    "score",
    "velocity_score",
    "volatility_score",
]
