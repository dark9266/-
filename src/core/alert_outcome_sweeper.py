"""alert_outcome_sweeper — Phase 4 축 8 sweep 실행기.

src/core/alert_outcome.py 의 sweep_pending() 에 주입할 check_fn 을 빌드.
크림 상품 상세 1회 조회로 사이즈별 last_sale_price 를 확인:
    last_sale_date > fired_at AND last_sale_price >= kream_sell_price_at_fire  →  sold=True

※ fired_at 이후 체결만 sold 로 인정 (과거 체결로 hit_rate 부풀려지는 결함 방지).

호출 절약:
    - 같은 product_id 의 다중 followup 은 batch 로 1회만 fetch (in-memory cache)
    - 크림 호출 purpose 태그 = "alert_outcome_sweep" 로 캡 분리 추적
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Awaitable, Callable, Protocol

from src.core.alert_outcome import FollowupRecord
from src.core.kream_budget import kream_purpose

logger = logging.getLogger(__name__)


CheckFn = Callable[[FollowupRecord], Awaitable[tuple[bool, int | None]]]


class _KreamFetchLike(Protocol):
    async def get_product_detail(self, product_id: str): ...  # noqa: E704


def _to_epoch(dt: datetime | None) -> float | None:
    """naive datetime → epoch (로컬 tz 해석). None 은 통과."""
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def make_check_fn(crawler: _KreamFetchLike | None = None) -> CheckFn:
    """sweep_pending 에 주입할 check_fn 생성.

    크림 product_id 별 1회 조회 → 모든 size 의 (last_sale_price, last_sale_date) 캐시 → 비교.

    sold 판정: last_sale_date > fired_at AND last_sale_price >= kream_sell_price_at_fire

    Returns
    -------
    async (FollowupRecord) -> (sold: bool, actual_price: int | None)
    """
    if crawler is None:
        from src.crawlers.kream import kream_crawler  # 지연 import (테스트 격리)

        crawler = kream_crawler  # type: ignore[assignment]

    # size → (last_sale_price, last_sale_epoch)
    cache: dict[str, dict[str, tuple[int | None, float | None]]] = {}

    async def _fetch(pid: str) -> dict[str, tuple[int | None, float | None]]:
        if pid in cache:
            return cache[pid]
        with kream_purpose("alert_outcome_sweep"):
            try:
                product = await crawler.get_product_detail(pid)  # type: ignore[union-attr]
            except Exception:
                logger.exception("[sweep] kream get_product_detail 실패 pid=%s", pid)
                cache[pid] = {}
                return {}
        if product is None:
            cache[pid] = {}
            return {}
        size_map: dict[str, tuple[int | None, float | None]] = {}
        for sp in getattr(product, "size_prices", []) or []:
            size_map[str(sp.size).strip()] = (sp.last_sale_price, _to_epoch(sp.last_sale_date))
        cache[pid] = size_map
        return size_map

    async def check(rec: FollowupRecord) -> tuple[bool, int | None]:
        size_map = await _fetch(rec.kream_product_id)
        if not size_map:
            return False, None

        def _is_post_fire_sale(price: int | None, epoch: float | None) -> bool:
            # 체결가·체결시각 모두 있어야 하고, 체결이 알림 이후여야 함
            return (
                isinstance(price, int) and price > 0
                and epoch is not None and epoch > rec.fired_at
            )

        if not rec.size:
            # MIN 대표가 모드 — 알림 이후 체결된 사이즈 중 최고 체결가 사용
            post = [
                (p, e) for (p, e) in size_map.values()
                if _is_post_fire_sale(p, e)
            ]
            if not post:
                return False, None
            best = max(p for p, _ in post)  # type: ignore[misc]
            return best >= rec.kream_sell_price_at_fire, best
        # 특정 사이즈 — 해당 사이즈의 알림 이후 체결만
        price, epoch = size_map.get(rec.size.strip(), (None, None))
        if not _is_post_fire_sale(price, epoch):
            return False, None
        assert isinstance(price, int)
        return price >= rec.kream_sell_price_at_fire, price

    return check


__all__ = ["CheckFn", "make_check_fn"]
