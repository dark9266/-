"""실재고 사이즈 수집 공유 헬퍼.

각 어댑터가 매칭 가드 통과 후 PDP 를 호출해 "실재고 있는 사이즈 목록"을
얻을 때 공통으로 사용. 빈 튜플 반환 = drop 정책 (호출자 책임).

2026-04-16 사고 (HQ4307-001 LAUNCH / HQ6893 사이즈 1개) 재발 방지를 위해
22 어댑터 전수 적용.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def fetch_in_stock_sizes(
    crawler: Any,
    product_id: str,
    *,
    source_tag: str = "",
) -> tuple[str, ...]:
    """`crawler.get_product_detail(product_id)` 호출 → 재고 있는 사이즈 튜플.

    빈 튜플의 의미 (전부 동일 처리 — drop):
    - PDP 호출 실패 (네트워크/차단/타임아웃)
    - 전 사이즈 품절 (in_stock=False)
    - LAUNCH/비구매가능 상품 (PDP 가 None 반환)
    - 사이즈 정보 없는 PDP

    호출자는 반드시 빈 결과를 "drop" 으로 처리해야 한다 — 과거의
    "listing-only 폴백" 정책은 HQ4307/HQ6893 사고 원인이 되어 폐기.
    """
    if not product_id:
        return ()
    try:
        product = await crawler.get_product_detail(str(product_id))
    except Exception as exc:  # noqa: BLE001 — 격리
        logger.warning(
            "[%s] PDP 사이즈 조회 실패 pid=%s err=%s",
            source_tag or "size_helpers",
            product_id,
            exc,
        )
        return ()
    if not product:
        return ()
    sizes_attr = getattr(product, "sizes", None)
    if not sizes_attr:
        return ()
    out: list[str] = []
    for s in sizes_attr:
        # in_stock 필드 없으면 기본 True 로 간주 (일부 크롤러는 이미 품절을
        # 필터링한 채로 반환하므로 안전 디폴트).
        if not getattr(s, "in_stock", True):
            continue
        sz = str(getattr(s, "size", "") or "").strip()
        if sz:
            out.append(sz)
    return tuple(out)


__all__ = ["fetch_in_stock_sizes"]
