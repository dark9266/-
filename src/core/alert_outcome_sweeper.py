"""alert_outcome_sweeper — Phase 4 축 8 sweep 실행기.

src/core/alert_outcome.py 의 sweep_pending() 에 주입할 check_fn 을 빌드.
크림 상품 상세 1회 조회로 사이즈별 last_sale_price 를 확인:
    last_sale_price >= kream_sell_price_at_fire  →  sold=True

호출 절약:
    - 같은 product_id 의 다중 followup 은 batch 로 1회만 fetch (in-memory cache)
    - 크림 호출 purpose 태그 = "alert_outcome_sweep" 로 캡 분리 추적
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Protocol

from src.core.alert_outcome import FollowupRecord
from src.core.kream_budget import kream_purpose

logger = logging.getLogger(__name__)


CheckFn = Callable[[FollowupRecord], Awaitable[tuple[bool, int | None]]]


class _KreamFetchLike(Protocol):
    async def get_product_detail(self, product_id: str): ...  # noqa: E704


def make_check_fn(crawler: _KreamFetchLike | None = None) -> CheckFn:
    """sweep_pending 에 주입할 check_fn 생성.

    크림 product_id 별 1회 조회 → 모든 size 의 last_sale_price 캐시 → 비교.

    Returns
    -------
    async (FollowupRecord) -> (sold: bool, actual_price: int | None)
    """
    if crawler is None:
        from src.crawlers.kream import kream_crawler  # 지연 import (테스트 격리)

        crawler = kream_crawler  # type: ignore[assignment]

    cache: dict[str, dict[str, int | None]] = {}

    async def _fetch_size_last_sales(pid: str) -> dict[str, int | None]:
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
        size_map: dict[str, int | None] = {}
        for sp in getattr(product, "size_prices", []) or []:
            size_map[str(sp.size).strip()] = sp.last_sale_price
        cache[pid] = size_map
        return size_map

    async def check(rec: FollowupRecord) -> tuple[bool, int | None]:
        size_map = await _fetch_size_last_sales(rec.kream_product_id)
        if not size_map:
            return False, None
        # size 가 비어 있으면 (MIN 대표가 알림) → 전 사이즈 중 가장 비싼 last_sale 비교
        if not rec.size:
            prices = [p for p in size_map.values() if isinstance(p, int) and p > 0]
            if not prices:
                return False, None
            best = max(prices)
            return best >= rec.kream_sell_price_at_fire, best
        # 특정 사이즈 — 해당 사이즈의 last_sale 만
        last = size_map.get(rec.size.strip())
        if not isinstance(last, int) or last <= 0:
            return False, None
        return last >= rec.kream_sell_price_at_fire, last

    return check


__all__ = ["CheckFn", "make_check_fn"]
