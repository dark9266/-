"""아디다스 공식몰 푸시 어댑터 (Phase 3 배치 3).

v3 푸시 전환 세 번째 실제 어댑터. adidas.co.kr taxonomy API 카테고리별
카탈로그를 페이지네이션 덤프한 뒤 상품 `productId` (예: ``IE5900``) 를
크림 모델번호로 **직접** 매칭한다.

설계 포인트
------------
* 아디다스 강점: `productId` 가 곧 글로벌 SKU 이며 대부분의 크림 DB
  모델번호와 포맷이 일치한다. 상품명 regex 파싱 불필요.
* 어댑터는 producer 전용. HTTP 레이어는 `src.crawlers.adidas.adidas_crawler`
  를 재사용하며 테스트에서는 mock 주입.
* Akamai WAF 민감 — Referer/Origin 헤더는 기존 크롤러 `_build_headers`
  가 이미 주입하므로 어댑터 레벨에서 따로 손대지 않는다 (크롤러 파일 수정 금지).
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
* 매칭 가드는 `src.core.matching_guards` 공통 모듈 재사용.

구현 메모
---------
실제 아디다스 taxonomy API 는 카테고리·페이지네이션 파라미터를
`/api/search/taxonomy?query=&start=` 형태로 받으며, 기존 크롤러는 키워드
검색만 구현되어 있다. 카탈로그 덤프용 페이지네이션 경로는 어댑터가
기대하는 HTTP 인터페이스 (`fetch_taxonomy_page`) 로 추상화해 두고
실 바인딩은 후속 크롤러 확장에서 연결한다 — 본 어댑터의 단위 테스트는
mock HTTP 로 완전 고립되어 동작한다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect
from src.adapters._size_helpers import fetch_in_stock_sizes
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


# ─── 덤프 기본 카테고리 — 크림 47k 전체 커버 (전 카테고리) ──────
# adidas.co.kr taxonomy 경로 slug. 값은 로그/알림 표시용.
DEFAULT_CATEGORIES: dict[str, str] = {
    "men-shoes": "남성 신발",
    "women-shoes": "여성 신발",
    "kids-shoes": "키즈 신발",
    "men-clothing": "남성 의류",
    "women-clothing": "여성 의류",
    "kids-clothing": "키즈 의류",
    "men-accessories": "남성 액세서리",
    "women-accessories": "여성 액세서리",
}

BASE_URL = "https://www.adidas.co.kr"


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(item: dict) -> str:
    """상품 상세 URL 조립."""
    link = item.get("link") or ""
    if link:
        if link.startswith("http"):
            return link
        return BASE_URL + link
    pid = item.get("product_id") or item.get("productId") or ""
    return f"{BASE_URL}/{pid}.html" if pid else BASE_URL


@dataclass
class AdidasMatchStats:
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


class AdidasAdapter:
    """아디다스 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "adidas"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: dict[str, str] | None = None,
        max_pages: int = 10,
        page_size: int = 48,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로. `kream_products` / `kream_collect_queue`
            테이블 조회·적재.
        http_client:
            아디다스 HTTP 레이어. 기본값 None → 모듈 전역 `adidas_crawler`
            사용. 테스트에서는 `fetch_taxonomy_page` 를 제공하는 객체를 주입.
        categories:
            덤프할 카테고리 {slug: name}. 기본 `DEFAULT_CATEGORIES`.
        max_pages:
            카테고리별 최대 페이지 수.
        page_size:
            페이지당 아이템 수 (아디다스 taxonomy 기본 48).
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories or DEFAULT_CATEGORIES
        self._max_pages = max_pages
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.adidas import adidas_crawler

        self._http = adidas_crawler
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """아디다스 카테고리별 카탈로그 페이지네이션 덤프.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, 상품 dict 리스트)`. 각 아이템은 크롤러
            `_parse_items` 가 반환하는 정규화 구조 (`product_id`/`name`/
            `price`/`sizes`/...) 를 유지하며, 어댑터가 `_category_slug`·
            `_category_name` 메타를 추가한다.
        """
        http = await self._get_http()
        products: list[dict] = []

        for slug, name in self._categories.items():
            page = 1
            seen: set[str] = set()
            while page <= self._max_pages:
                try:
                    data = await http.fetch_taxonomy_page(
                        category=slug,
                        page_size=self._page_size,
                        page_number=page,
                    )
                except Exception:
                    logger.exception(
                        "[adidas] 카테고리 덤프 실패: %s(%s) page=%d",
                        name, slug, page,
                    )
                    break

                chunk = (data or {}).get("items") or []
                if not chunk:
                    break
                for item in chunk:
                    pid = item.get("product_id") or item.get("productId") or ""
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    item["_category_slug"] = slug
                    item["_category_name"] = name
                    products.append(item)

                total = int((data or {}).get("totalCount") or 0)
                if total and len(seen) >= total:
                    break
                page += 1

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[adidas] 카탈로그 덤프 완료: %d건", len(products))
        return event, products

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        """크림 DB 전체를 모델번호 stripped key 로 인덱스."""
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number "
                "FROM kream_products WHERE model_number != ''"
            ).fetchall()
        finally:
            conn.close()
        index: dict[str, dict] = {}
        for row in rows:
            key = _strip_key(row["model_number"])
            if key:
                index[key] = dict(row)
        return index

    async def _enqueue_collect(self, item: dict, model_no: str) -> bool:
        """미등재 신상 → kream_collect_queue INSERT OR IGNORE."""
        return await aenqueue_collect(
            self._db_path,
            model_number=normalize_model_number(model_no),
            brand_hint=item.get("brand") or "adidas",
            name_hint=item.get("name") or "",
            source=self.source_name,
            source_url=_build_url(item),
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], AdidasMatchStats]:
        """덤프된 아디다스 아이템 → 크림 DB 매칭 → CandidateMatched publish.

        `product_id` (글로벌 SKU) 를 모델번호로 직접 사용. 상품명 파싱 불필요.
        미등재 상품은 수집 큐에 적재한다.
        """
        stats = AdidasMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []

        for item in products:
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            model_no = (
                item.get("product_id")
                or item.get("productId")
                or item.get("model_number")
                or ""
            ).strip()
            if not model_no:
                stats.no_model_number += 1
                continue

            key = _strip_key(model_no)
            if not key:
                stats.no_model_number += 1
                continue

            # 덤프 ledger — 매칭 전 전수 기록 (오프라인 분석용)
            try:
                from src.core.dump_ledger import record_dump_item
                await record_dump_item(
                    self._db_path,
                    source=self.source_name,
                    model_no=model_no,
                    name=item.get("name") or "",
                    url=item.get("url") or "",
                )
            except Exception:
                logger.debug("[adidas] dump_ledger 실패 (비치명)")

            kream_row = kream_index.get(key)
            if kream_row is None:
                if await self._enqueue_collect(item, model_no):
                    stats.collected_to_queue += 1
                continue

            # 매칭 가드 — 크림 이름 vs 소싱 이름 키워드 비교
            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[adidas] 콜라보 가드 차단: kream=%r source=%r",
                    kream_name[:40],
                    source_name_text[:40],
                )
                stats.skipped_guard += 1
                continue
            stype_diff = subtype_mismatch(
                _keyword_set(kream_name), _keyword_set(source_name_text)
            )
            if stype_diff:
                logger.info(
                    "[adidas] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("price") or 0)
            url = _build_url(item)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[adidas] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            # PDP 실재고 사이즈 — 빈 결과 무조건 drop
            http = await self._get_http()
            available_sizes = await fetch_in_stock_sizes(
                http, str(item.get("product_id") or ""), source_tag="adidas"
            )
            if not available_sizes:
                logger.info(
                    "[adidas] PDP 재고 없음 drop: pid=%s model=%s",
                    item.get("product_id"),
                    model_no,
                )
                stats.soldout_dropped += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_no),
                retail_price=price,
                size="",  # 리스팅 단계엔 사이즈 선택 없음 — 수익 consumer 가 보강
                url=url,
                available_sizes=available_sizes,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[adidas] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = ["AdidasAdapter", "AdidasMatchStats", "DEFAULT_CATEGORIES"]
