"""실재고 사이즈 수집 공유 헬퍼.

각 어댑터가 매칭 가드 통과 후 PDP 를 호출해 "실재고 있는 사이즈 목록"을
얻을 때 공통으로 사용. 빈 튜플 반환 = drop 정책 (호출자 책임).

2026-04-16 사고 (HQ4307-001 LAUNCH / HQ6893 사이즈 1개) 재발 방지를 위해
22 어댑터 전수 적용.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockEvidence:
    """PDP 조회 1회의 재고 증거 (1c-3, 소싱처 재고 게이트용).

    `sizes` 는 기존 `fetch_in_stock_sizes` 와 동일 계약(빈 튜플=drop 가능).
    `stock_state`/`stock_reason` 은 `RetailProduct` 필드를 그대로 옮긴 것 —
    ``""`` (레거시/구조적 미지원) 이면 호출자는 기존 drop 정책을 따른다.
    """

    sizes: tuple[str, ...] = ()
    stock_state: str = ""
    stock_reason: str = ""


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


async def fetch_stock_evidence(
    crawler: Any,
    product_id: str,
    *,
    source_tag: str = "",
) -> StockEvidence:
    """`crawler.get_product_detail(product_id)` 호출 → 재고 증거 전체 반환.

    `fetch_in_stock_sizes` 와 동일한 PDP 호출이지만, `RetailProduct` 가
    채운 `stock_state`/`stock_reason` (1c-3) 까지 함께 넘겨 어댑터가
    `SourceStockSnapshot` 을 구성할 수 있게 한다. 기존 `fetch_in_stock_sizes`
    시그니처는 변경하지 않는다 — 이 함수는 신규 추가.

    PDP 호출/조회 실패(예외, None 반환)는 명시적 UNKNOWN 으로 취급한다
    (기존 `fetch_in_stock_sizes` 는 빈 튜플=drop 이었지만, 이 함수는 조회실패와
    "레거시 크롤러(stock_state 미지원)"를 호출자가 구분할 수 있게 남겨둔다).
    """
    if not product_id:
        return StockEvidence(sizes=(), stock_state="UNKNOWN", stock_reason="empty_product_id")
    try:
        product = await crawler.get_product_detail(str(product_id))
    except Exception as exc:  # noqa: BLE001 — 격리
        logger.warning(
            "[%s] PDP 재고 증거 조회 실패 pid=%s err=%s",
            source_tag or "size_helpers",
            product_id,
            exc,
        )
        return StockEvidence(sizes=(), stock_state="UNKNOWN", stock_reason="pdp_exception")
    if not product:
        return StockEvidence(sizes=(), stock_state="UNKNOWN", stock_reason="pdp_none")

    sizes_attr = getattr(product, "sizes", None) or []
    out: list[str] = []
    for s in sizes_attr:
        if not getattr(s, "in_stock", True):
            continue
        sz = str(getattr(s, "size", "") or "").strip()
        if sz:
            out.append(sz)

    stock_state = str(getattr(product, "stock_state", "") or "")
    stock_reason = str(getattr(product, "stock_reason", "") or "")
    return StockEvidence(sizes=tuple(out), stock_state=stock_state, stock_reason=stock_reason)


__all__ = ["StockEvidence", "fetch_in_stock_sizes", "fetch_stock_evidence"]
