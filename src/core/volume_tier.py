"""거래량 → 티어 산정 중앙 함수 (조각 2-1).

기존에는 `bootstrap_cold_volumes_light.py.tmp`, `price_refresher.py` 등 여러
곳에서 각자 `volume_7d >= N` 식으로 티어를 산정했다. 이 모듈은 그 판정을 한
곳으로 모은 것 — 신설 + `bootstrap_cold_volumes_light.py` 사용만 이번 스코프.
기존 산정처를 이 함수로 교체하는 건 후속 작업(스코프 밖, 2026-07-25 계획서
"조각 2-1" 참조).

경계값 (숨은 보석 정책 — 거래량 게이트 1 고정과 동일한 정신):
    None / 미확인   -> "unknown"  (한 번도 조회 못 함 — cold 로 단정 금지)
    0               -> "cold"
    1 ~ (min-1)     -> "warm"
    >= min          -> "hot"      (min = settings.realtime_hot_volume_min, 기본 5)
"""

from __future__ import annotations

from src.config import settings

VolumeTier = str  # "hot" | "warm" | "cold" | "unknown"


def tier_for_volume(volume_7d: int | None, *, hot_min: int | None = None) -> VolumeTier:
    """7일 거래량으로 티어 산정.

    None = 미확인(unknown), 0 = cold, 1~(hot_min-1) = warm, >=hot_min = hot.
    """
    if volume_7d is None:
        return "unknown"
    threshold = hot_min if hot_min is not None else settings.realtime_hot_volume_min
    if volume_7d < 0:
        return "unknown"
    if volume_7d == 0:
        return "cold"
    if volume_7d >= threshold:
        return "hot"
    return "warm"
