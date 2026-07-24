"""SourceStockSnapshot 상태모델 + CandidateMatched.source_stock 계약 테스트."""

from src.core.event_bus import CandidateMatched
from src.models.stock import SourceStockSnapshot, StockState


def test_in_stock_with_sizes_is_usable():
    now = 1000.0
    snap = SourceStockSnapshot(
        state=StockState.IN_STOCK,
        available_sizes=("270",),
        observed_at=now - 10,
        expires_at=now + 100,
    )
    assert snap.usable(now) is True


def test_in_stock_empty_sizes_not_usable():
    now = 1000.0
    snap = SourceStockSnapshot(
        state=StockState.IN_STOCK,
        available_sizes=(),
        observed_at=now - 10,
        expires_at=now + 100,
    )
    assert snap.usable(now) is False


def test_expired_not_usable():
    now = 1000.0
    snap = SourceStockSnapshot(
        state=StockState.IN_STOCK,
        available_sizes=("270",),
        observed_at=now - 200,
        expires_at=now - 100,  # 이미 만료
    )
    assert snap.is_fresh(now) is False
    assert snap.usable(now) is False


def test_unknown_not_usable():
    now = 1000.0
    snap = SourceStockSnapshot(
        state=StockState.UNKNOWN,
        available_sizes=("270",),
        observed_at=now - 10,
        expires_at=now + 100,
        reason_code="api_400",
    )
    assert snap.usable(now) is False


def test_candidate_matched_source_stock_defaults_none():
    candidate = CandidateMatched(
        source="musinsa",
        kream_product_id=123,
        model_no="ABC123",
        retail_price=100000,
        size="270",
        url="https://example.com",
    )
    assert candidate.source_stock is None
