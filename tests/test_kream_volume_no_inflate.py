"""pinia 캡 × 멀티플라이어 뻥튀기 방어 회귀 테스트.

과거 버그: pinia 5건 캡 × 3 = 15 로 volume_7d 뻥튀기 → 하드 플로어 ≥1
무력화 + 246건 알림 오염. 2026-04-15 commit 6170947 에서 `_estimate_volume`
을 raw 반환으로 중성화. 이 테스트는 재발 방지 앵커.
"""
from __future__ import annotations

from src.crawlers.kream import KreamCrawler


def test_estimate_volume_never_multiplies():
    """_estimate_volume 은 pinia_count 관계없이 raw 값을 그대로 반환해야."""
    assert KreamCrawler._estimate_volume(5, 5) == 5
    assert KreamCrawler._estimate_volume(0, 5) == 0
    assert KreamCrawler._estimate_volume(3, 5) == 3
    # 대량 거래 케이스도 가공 없이 통과
    assert KreamCrawler._estimate_volume(42, 5) == 42


def test_estimate_volume_ignores_multiplier_signature():
    """'cap × N' 뻥튀기 패턴이 재도입되면 즉시 실패.

    과거 버그 시그니처: pinia_count(5) × multiplier(3) = 15.
    입력값 5 를 넣었을 때 결과가 5 가 아닌 15/10/20 이면 재발.
    """
    for raw in (5,):
        result = KreamCrawler._estimate_volume(raw, 5)
        assert result == raw, (
            f"_estimate_volume({raw}, 5) = {result} — 캡 × 멀티 재도입 의심"
        )
