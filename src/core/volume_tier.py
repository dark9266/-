"""거래량 → 티어 산정 + cold 재검사 정책 중앙 함수 (조각 2-1 + 2-3).

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

**조각 2-3 (cold 재검사 정책)**: 아래 함수들은 `next_volume_check_at` 축을
다룬다 — `next_volume_attempt_at`(retryable/quarantined 재시도, 2-1)과는
**별개 축**이다. 혼동 금지:
    - `next_volume_attempt_at` : 실패(retryable/quarantined) 후 "언제 다시
      시도할지" — `retry_backoff()` 가 이 값을 계산한다.
    - `next_volume_check_at`   : 성공(success_positive/success_zero) 후
      "다음 정기 재검사가 언제인지" — `compute_next_volume_check_at()` 가
      이 값을 계산한다.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

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


# ─── 조각 2-3: cold 재검사 TTL 매트릭스 ─────────────────────────────────────

_JITTER_RATIO = 0.10  # ±10% 결정적 지터 (월간 동시 재검사 집중 방지)


def next_check_interval(volume_7d: int | None, *, has_retail_match: bool) -> timedelta:
    """다음 정기 재검사까지 기본 간격 (지터 적용 전) — cold 재검사 TTL 매트릭스.

    오케스트레이터 확정값(2026-07-25 계획서 조각 2-3):
        None(미확인)             -> timedelta(0)   (즉시 — 부트스트랩 대상)
        0 + retail 매칭 있음     -> 30일
        0 + retail 매칭 없음     -> 90일 (연결 안 된 zero — 보류 허용)
        1 ~ (hot_min-1)          -> 7일
        >= hot_min(기본 5)       -> 3일
    """
    if volume_7d is None:
        return timedelta(0)
    if volume_7d < 0:
        return timedelta(0)  # 방어적 — 있을 수 없는 값, unknown 과 동일하게 즉시 재검사
    if volume_7d == 0:
        return timedelta(days=30) if has_retail_match else timedelta(days=90)
    if volume_7d >= settings.realtime_hot_volume_min:
        return timedelta(days=3)
    return timedelta(days=7)


def _deterministic_offset(seed: str, base_seconds: float, ratio: float) -> float:
    """해시 기반 결정적 ±ratio 오프셋(초). 랜덤 금지 — 재현 가능해야 한다.

    `seed` 에 **namespace 를 포함**시켜 축을 분리한다. 같은 product_id 라도
    정기 재검사와 quarantine 이 같은 오프셋을 쓰면 두 이벤트가 상관되어,
    한 상품의 일정이 항상 같은 달력 위상에 몰린다(코덱스 S 자문).
    """
    if base_seconds <= 0:
        return 0.0
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    normalized = int(digest[:8], 16) / 0xFFFFFFFF  # 0.0 ~ 1.0, 해시 기반(비-랜덤)
    fraction = (normalized * 2 - 1) * ratio  # -ratio ~ +ratio
    return base_seconds * fraction


def jitter_offset_seconds(product_id: str, base_seconds: float) -> float:
    """정기 재검사(`next_volume_check_at`) 용 결정적 ±10% 지터(초).

    같은 product_id 는 항상 같은 오프셋을 반환한다(테스트 가능성 요구사항).
    `base_seconds` 가 0 이하면 지터도 0(즉시 재검사 케이스는 지터를 적용하지
    않는다).
    """
    return _deterministic_offset(product_id, base_seconds, _JITTER_RATIO)


# ─── quarantine(404/410) 재시도 시각 — 별도 namespace ────────────────────────
#
# 2026-07-25 발견: quarantine 은 `datetime('now', '+90 days')` 고정이라 **지터가
# 없었다**. 같은 장애 파동에서 격리된 상품들이 90일 뒤 **같은 날 한꺼번에** 되살아
# 난다. 정기 재검사(`next_volume_check_at`)에는 ±10% 지터가 있는데 여기만 빠져 있었다.
#
# 기준시각은 실행시각이 아니라 `last_volume_attempt_at` 이다 — 그래야 재실행해도
# 예정일이 움직이지 않는다(멱등). namespace 는 정기 재검사와 분리한다.
QUARANTINE_BASE_DAYS: int = 90
_QUARANTINE_JITTER_RATIO: float = 0.10  # ±9일 (81~99일)
_QUARANTINE_NAMESPACE: str = "quarantine:v1"


def compute_quarantine_until(
    product_id: str,
    error_class: str,
    attempted_at: datetime,
    *,
    base_days: int = QUARANTINE_BASE_DAYS,
) -> datetime:
    """404/410 격리 상품의 다음 시도 시각 = 시도시각 + 90일 ± 10%.

    - `error_class`(예: "http_404" / "http_410")를 해시에 넣어 두 종류가 서로 다른
      날짜에 풀리게 한다.
    - `attempted_at` 기준이라 같은 입력이면 **몇 번을 다시 계산해도 같은 값**이다.
    - 정기 재검사 지터와 namespace 가 달라 한 상품의 두 일정이 상관되지 않는다.
    """
    base_seconds = float(base_days) * 86400.0
    seed = f"{_QUARANTINE_NAMESPACE}:{product_id}:{error_class}"
    offset = _deterministic_offset(seed, base_seconds, _QUARANTINE_JITTER_RATIO)
    return attempted_at + timedelta(seconds=base_seconds + offset)


def compute_next_volume_check_at(
    product_id: str,
    volume_7d: int | None,
    *,
    has_retail_match: bool,
    now: datetime | None = None,
) -> datetime:
    """다음 정기 재검사 시각 = `next_check_interval()` + 결정적 지터.

    `volume_7d`가 None(미확인)이면 즉시(지터 없이 `now` 그대로) — 부트스트랩
    대상 선별과 동일한 의미(`last_volume_check IS NULL`).
    """
    if now is None:
        now = datetime.now(UTC)
    interval_seconds = next_check_interval(volume_7d, has_retail_match=has_retail_match)
    base_seconds = interval_seconds.total_seconds()
    if base_seconds <= 0:
        return now
    offset = jitter_offset_seconds(product_id, base_seconds)
    return now + timedelta(seconds=base_seconds + offset)


# ─── 조각 2-3: retryable 재시도 백오프 (2-1 임시 6h 고정 대체) ──────────────


def retry_backoff(attempt_count: int) -> timedelta:
    """retryable 재시도까지 대기 간격 — 시도 횟수가 늘수록 완화.

    1회: 6시간 / 2회: 24시간 / 3회 이상: 7일. `attempt_count` 가 1 미만이면
    1회로 취급(방어적).
    """
    if attempt_count <= 1:
        return timedelta(hours=6)
    if attempt_count == 2:
        return timedelta(hours=24)
    return timedelta(days=7)


# ─── 조각 2-3: 이벤트 앞당김 인터페이스 (실 배선은 스코프 밖) ───────────────


def bump_check_at(current: datetime | None, candidate: datetime) -> datetime:
    """retail 재고/가격 이벤트 발생 시 재검사 예정일을 앞당기는 순수 계약.

    `candidate`(이벤트로 제안된 시각)가 현재 예정일보다 이르면 그것으로
    당긴다. `current`가 이미 더 이르면 그대로 유지 — **뒤로 미루지 않는다**
    (앞당김 전용, 지연 아님). `current`가 없으면 `candidate`를 그대로 채택.

    ⚠️ 인터페이스만 — 실제 소싱처 어댑터가 재고/가격 변화를 감지해 이 함수를
    호출하는 배선은 후속 조각(2026-07-25 계획서 조각 2-3 "이벤트 앞당김은
    인터페이스만" — 스코프 밖).
    """
    if current is None:
        return candidate
    return min(current, candidate)
