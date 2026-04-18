"""BEAKER 푸시 어댑터 (Phase 3 배치 5).

BEAKER 는 삼성물산 SSF Shop 산하 편집숍(brandShopNo=BDMA09, brndShopId=MCBR).
`src.crawlers.beaker.BeakerCrawler.search_products(keyword, limit, page_no)` 가
BEAKER 카테고리 리스팅을 `/selectProductList` 로 페이지네이션해 dict 리스트를
돌려준다. 어댑터는 그 결과를 크림 DB 모델번호 인덱스로 매칭한다.

한섬 백엔드 여부
----------------
컬럼명(sizeItmNm/onlineUsefulInvQty/godNm 등)은 한섬 EQL 과 유사하지만,
실제 플랫폼은 **삼성물산 SSF Shop** 으로 백엔드는 분리돼 있다.
EQL 어댑터를 참고만 할 뿐 런타임 의존은 없다.

설계 포인트
------------
* producer 전용. HTTP 레이어는 `src.crawlers.beaker.beaker_crawler` 재사용.
* 카탈로그 덤프 단위 = BEAKER 카테고리 (WOMEN/MEN/BAG_SHOES/BEAUTY).
  브랜드가 아닌 카테고리 단위로 덤프하지만 `_brand_keyword` 필드 이름은
  EQL 어댑터와 일관성을 위해 유지.
* 매칭 가드(콜라보/서브타입)는 `src.core.matching_guards` 공통 모듈 재사용.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
* POST 미사용 — 전부 GET.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect
from src.adapters._size_helpers import fetch_in_stock_sizes
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


BASE_URL = "https://www.ssfshop.com"


# BEAKER 덤프 대상 카테고리 키 (BeakerCrawler.CATEGORY_MAP 과 맞아야 한다).
DEFAULT_CATEGORY_KEYWORDS: dict[str, str] = {
    "BEAKER_WOMEN": "BEAKER_WOMEN",
    "BEAKER_MEN": "BEAKER_MEN",
    "BEAKER_BAG_SHOES": "BEAKER_BAG_SHOES",
}


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(product_id: str) -> str:
    return (
        f"{BASE_URL}/public/goods/detail/{product_id}/view"
        if product_id else ""
    )


@dataclass
class BeakerMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    no_model_number: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "no_model_number": self.no_model_number,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
        }


class BeakerAdapter:
    """BEAKER 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "beaker"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        crawler: Any = None,
        *,
        brand_keywords: dict[str, str] | None = None,
        max_pages: int = 3,
        page_size: int = 60,
    ) -> None:
        self._bus = bus
        self._db_path = db_path
        self._crawler = crawler
        self._brand_keywords = brand_keywords or DEFAULT_CATEGORY_KEYWORDS
        self._max_pages = max_pages
        self._page_size = page_size

    async def _get_crawler(self) -> Any:
        if self._crawler is not None:
            return self._crawler
        from src.crawlers.beaker import beaker_crawler

        self._crawler = beaker_crawler
        return self._crawler

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """BEAKER 카테고리 키별 페이지네이션 덤프."""
        crawler = await self._get_crawler()
        products: list[dict] = []

        for brand_label, brand_keyword in self._brand_keywords.items():
            seen_product_ids: set[str] = set()
            fallback_single = False
            for page in range(1, self._max_pages + 1):
                try:
                    items = await crawler.search_products(
                        keyword=brand_keyword,
                        limit=self._page_size,
                        page_no=page,
                    )
                except TypeError:
                    if page > 1:
                        break
                    try:
                        items = await crawler.search_products(
                            keyword=brand_keyword,
                            limit=self._page_size,
                        )
                    except Exception:
                        logger.exception(
                            "[beaker] 카테고리 덤프 실패: %s page=%d",
                            brand_label, page,
                        )
                        break
                    fallback_single = True
                except Exception:
                    logger.exception(
                        "[beaker] 카테고리 덤프 실패: %s page=%d",
                        brand_label, page,
                    )
                    break

                if not items:
                    break

                new_in_page = 0
                for item in items:
                    pid = str(item.get("product_id") or "")
                    if not pid or pid in seen_product_ids:
                        continue
                    seen_product_ids.add(pid)
                    item["_brand_label"] = brand_label
                    item["_brand_keyword"] = brand_keyword
                    products.append(item)
                    new_in_page += 1

                if fallback_single:
                    break
                if len(items) < self._page_size:
                    break
                if new_in_page == 0:
                    break

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[beaker] 카탈로그 덤프 완료: %d건", len(products))
        return event, products

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        from src.core.kream_index import get_kream_index
        return get_kream_index(self._db_path).get()

    async def _enqueue_collect(self, item: dict, model_no: str) -> bool:
        pid = str(item.get("product_id") or "")
        return await aenqueue_collect(
            self._db_path,
            model_number=normalize_model_number(model_no),
            brand_hint=item.get("brand") or item.get("_brand_label") or "",
            name_hint=item.get("name") or "",
            source=self.source_name,
            source_url=_build_url(pid),
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], BeakerMatchStats]:
        """덤프된 BEAKER 아이템 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = BeakerMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        seen_keys: set[str] = set()

        for item in products:
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            model_from = (item.get("model_number") or "").strip()
            if not model_from:
                stats.no_model_number += 1
                continue

            key = _strip_key(model_from)
            if not key:
                stats.no_model_number += 1
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)

            kream_row = kream_index.get(key)
            if kream_row is None:
                if await self._enqueue_collect(item, model_from):
                    stats.collected_to_queue += 1
                continue

            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[beaker] 콜라보 가드 차단: kream=%r source=%r",
                    kream_name[:40], source_name_text[:40],
                )
                stats.skipped_guard += 1
                continue
            stype_diff = subtype_mismatch(
                _keyword_set(kream_name), _keyword_set(source_name_text)
            )
            if stype_diff:
                logger.info(
                    "[beaker] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40], stype_diff,
                )
                stats.skipped_guard += 1
                continue

            price = int(item.get("price") or 0)
            pid = str(item.get("product_id") or "")
            url = item.get("url") or _build_url(pid)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[beaker] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            # PDP 실재고 사이즈 — 빈 결과 무조건 drop
            crawler = await self._get_crawler()
            available_sizes = await fetch_in_stock_sizes(
                crawler, pid, source_tag="beaker"
            )
            if not available_sizes:
                logger.info(
                    "[beaker] PDP 재고 없음 drop: pid=%s model=%s",
                    pid, model_from,
                )
                stats.soldout_dropped += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_from),
                retail_price=price,
                size="",
                url=url,
                available_sizes=available_sizes,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[beaker] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = [
    "BeakerAdapter",
    "BeakerMatchStats",
    "DEFAULT_CATEGORY_KEYWORDS",
    "BASE_URL",
]
