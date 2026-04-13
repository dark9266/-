"""EQL 푸시 어댑터 (Phase 3 배치 4).

EQL(eqlstore.com)은 한섬 편집숍으로 HTML 파싱 기반 검색 API
(`/public/search/getSearchGodPaging`) 를 제공한다. 카시나/W컨셉과 달리
브랜드 전용 필터 엔드포인트가 공개돼 있지 않기 때문에, 카탈로그 전수
덤프는 **브랜드 키워드 기반 검색** 으로 수행한다 (Nike/adidas/Salomon 등).
기존 크롤러 `src.crawlers.eql.EqlCrawler.search_products(keyword, limit)` 가
이미 godNm 속성에서 모델번호를 추출해 반환하므로 어댑터는 그 결과를
그대로 크림 DB 인덱스에 붙인다.

설계 포인트
------------
* 어댑터는 producer 전용. HTTP 레이어는 `src.crawlers.eql.eql_crawler` 를
  재사용하며, 테스트에서는 `search_products(keyword, limit, page_no=?)`
  를 제공하는 mock 을 주입한다. 기존 크롤러 시그니처가 `page_no` 를 받지
  않으므로 TypeError 로 하위호환 fallback (단일 페이지) 처리.
* 모델번호는 크롤러의 `_parse_search_html` 이 이미 `model_number` 필드로
  넣어둔다 — 어댑터는 재파싱하지 않음.
* 매칭 가드(콜라보/서브타입)는 `src.core.matching_guards` 공통 모듈 재사용.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
* POST 미사용 — EQL 검색은 GET (`/public/search/getSearchGodPaging`).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import enqueue_collect
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


BASE_URL = "https://www.eqlstore.com"


# 기본 덤프 브랜드 키워드 — 크림 커버리지 우선순위 기준.
# EQL 은 브랜드 공식 필터 대신 검색 API 의 키워드를 사용.
DEFAULT_BRAND_KEYWORDS: dict[str, str] = {
    "Nike": "NIKE",
    "adidas": "ADIDAS",
    "New Balance": "NEW BALANCE",
    "Asics": "ASICS",
    "Salomon": "SALOMON",
    "Vans": "VANS",
}


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(product_id: str) -> str:
    return f"{BASE_URL}/product/{product_id}/detail" if product_id else ""


@dataclass
class EqlMatchStats:
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


class EqlAdapter:
    """EQL 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "eql"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        crawler: Any = None,
        *,
        brand_keywords: dict[str, str] | None = None,
        max_pages: int = 3,
        page_size: int = 40,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로. `kream_products` / `kream_collect_queue`
            테이블 조회·적재.
        crawler:
            EQL 크롤러 레이어. 기본값 None → 모듈 전역 `eql_crawler` 사용.
            테스트에서는 `search_products(keyword, limit[, page_no])` 를
            제공하는 객체를 주입.
        brand_keywords:
            덤프할 브랜드 {label: keyword}. 기본 `DEFAULT_BRAND_KEYWORDS`.
        max_pages:
            브랜드별 최대 페이지 수. 기존 EQL 크롤러가 page_no 를 지원하지
            않으면 1페이지만 돈다.
        page_size:
            페이지당 아이템 수. EQL 검색 상한 40.
        """
        self._bus = bus
        self._db_path = db_path
        self._crawler = crawler
        self._brand_keywords = brand_keywords or DEFAULT_BRAND_KEYWORDS
        self._max_pages = max_pages
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피 가능)
    # ------------------------------------------------------------------
    async def _get_crawler(self) -> Any:
        if self._crawler is not None:
            return self._crawler
        from src.crawlers.eql import eql_crawler

        self._crawler = eql_crawler
        return self._crawler

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """EQL 브랜드 키워드별 카탈로그 페이지네이션 덤프.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, 상품 dict 리스트)`. 각 아이템에는
            `_brand_label`·`_brand_keyword` 메타가 추가된다.
        """
        crawler = await self._get_crawler()
        products: list[dict] = []

        for brand_label, brand_keyword in self._brand_keywords.items():
            seen_product_ids: set[str] = set()
            for page in range(1, self._max_pages + 1):
                try:
                    items = await crawler.search_products(
                        keyword=brand_keyword,
                        limit=self._page_size,
                        page_no=page,
                    )
                except TypeError:
                    # 기존 EQL 크롤러는 page_no 미지원 → 1페이지만 조회하고 종료.
                    if page > 1:
                        break
                    try:
                        items = await crawler.search_products(
                            keyword=brand_keyword,
                            limit=self._page_size,
                        )
                    except Exception:
                        logger.exception(
                            "[eql] 브랜드 덤프 실패: %s page=%d",
                            brand_label, page,
                        )
                        break
                    # page_no 미지원 → 다음 페이지 루프 중단
                    fallback_single = True
                except Exception:
                    logger.exception(
                        "[eql] 브랜드 덤프 실패: %s page=%d",
                        brand_label, page,
                    )
                    break
                else:
                    fallback_single = False

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
        logger.info("[eql] 카탈로그 덤프 완료: %d건", len(products))
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

    def _enqueue_collect(self, item: dict, model_no: str) -> bool:
        """미등재 신상 → kream_collect_queue INSERT OR IGNORE."""
        pid = str(item.get("product_id") or "")
        return enqueue_collect(
            self._db_path,
            model_number=normalize_model_number(model_no),
            brand_hint=item.get("brand") or item.get("_brand_label") or "",
            name_hint=item.get("name") or "",
            source=self.source_name,
            source_url=_build_url(pid),
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], EqlMatchStats]:
        """덤프된 EQL 아이템 → 크림 DB 매칭 → CandidateMatched publish.

        EQL 크롤러 `_parse_search_html` 이 godNm 속성에서 이미 모델번호를
        추출해 `model_number` 필드로 넣어둔다. 여기선 그 필드를 신뢰하고,
        비어있으면 `no_model_number` 로 집계한다. 같은 브랜드 키워드에서
        중복 아이템이 나올 수 있어 stripped key 단위로 dedup 한다.
        """
        stats = EqlMatchStats(dumped=len(products))
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
                if self._enqueue_collect(item, model_from):
                    stats.collected_to_queue += 1
                continue

            # 매칭 가드 — 크림 이름 vs 소싱 이름 키워드 비교
            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[eql] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[eql] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("price") or 0)
            pid = str(item.get("product_id") or "")
            url = item.get("url") or _build_url(pid)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[eql] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_from),
                retail_price=price,
                size="",  # 리스팅 단계엔 사이즈 정보 없음 — 수익 consumer 가 보강
                url=url,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[eql] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = [
    "EqlAdapter",
    "EqlMatchStats",
    "DEFAULT_BRAND_KEYWORDS",
    "BASE_URL",
]
