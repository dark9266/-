"""W컨셉 푸시 어댑터 (Phase 3 배치 3).

v3 푸시 전환 세 번째 실제 어댑터. W컨셉은 POST 검색 API
(`display/api/v3/search/result/product`, DISPLAY-API-KEY 헤더)를
키워드 기반으로 호출해 상품 리스트를 받는다. 카테고리/브랜드 페이지네이션
덤프는 브랜드 키워드(예: "Nike", "adidas")를 순회하며 pageNo 를 증가시킨다.

설계 포인트
------------
* W컨셉 상품은 `itemCd` / 상품명 기반으로 모델번호를 추출해야 한다
  (`productManagementCd` 같은 SKU 필드가 검색 응답에 없음). 추출 로직은
  기존 크롤러 `src.crawlers.wconcept._extract_model_number` 를 재사용.
* 어댑터는 producer 전용. HTTP 레이어는 `src.crawlers.wconcept.wconcept_crawler`
  를 재사용하며, 테스트에서는 mock 주입. 상세 HTML 보강은 덤프 대량 처리
  성능 이슈로 본 단계에서 수행하지 않는다 (검색 메타만 사용).
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
* 매칭 가드는 `src.core.matching_guards` 공통 모듈 재사용.
* POST 는 읽기 목적 검색 쿼리 한정 — 본 어댑터는 HTTP 레이어를 직접
  다루지 않으므로 기존 크롤러의 `assert_post_allowed` 체크가 유지된다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect_batch
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


# 기본 덤프 브랜드 키워드 — 크림 커버리지 우선순위 기준.
# W컨셉은 브랜드 공식 필터 대신 키워드 검색을 사용.
DEFAULT_BRAND_KEYWORDS: dict[str, str] = {
    "Nike": "Nike",
    "adidas": "adidas",
    "New Balance": "New Balance",
    "Salomon": "Salomon",
    "Asics": "Asics",
    "The North Face": "The North Face",
    "Hoka": "Hoka",
    "Vans": "Vans",
}


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(item_cd: str) -> str:
    return f"https://www.wconcept.co.kr/Product/{item_cd}"


@dataclass
class WconceptMatchStats:
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


class WconceptAdapter:
    """W컨셉 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "wconcept"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        brand_keywords: dict[str, str] | None = None,
        max_pages: int = 5,
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
        http_client:
            W컨셉 HTTP 레이어. 기본값 None → 모듈 전역 `wconcept_crawler` 사용.
            테스트에서는 `search_products(keyword, limit)` 를 제공하는 객체를 주입.
        brand_keywords:
            덤프할 브랜드 {label: keyword}. 기본 `DEFAULT_BRAND_KEYWORDS`.
        max_pages:
            브랜드별 최대 페이지 수 (W컨셉 검색 API `pageNo` 기준).
        page_size:
            페이지당 아이템 수. W컨셉 상한 40.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._brand_keywords = brand_keywords or DEFAULT_BRAND_KEYWORDS
        self._max_pages = max_pages
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.wconcept import wconcept_crawler

        self._http = wconcept_crawler
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """W컨셉 브랜드 키워드별 카탈로그 페이지네이션 덤프.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, 상품 dict 리스트)`. 각 아이템에는
            `_brand_label`·`_brand_keyword` 메타가 추가된다.
        """
        http = await self._get_http()
        products: list[dict] = []

        for brand_label, brand_keyword in self._brand_keywords.items():
            seen_item_cds: set[str] = set()
            for page in range(1, self._max_pages + 1):
                try:
                    items = await http.search_products(
                        keyword=brand_keyword,
                        limit=self._page_size,
                        page_no=page,
                    )
                except TypeError:
                    # 기존 크롤러 시그니처(page_no 미지원) 하위호환:
                    # 1페이지만 가능 — 2페이지 이상이면 중단.
                    if page > 1:
                        break
                    try:
                        items = await http.search_products(
                            keyword=brand_keyword,
                            limit=self._page_size,
                        )
                    except Exception:
                        logger.exception(
                            "[wconcept] 브랜드 덤프 실패: %s page=%d",
                            brand_label, page,
                        )
                        break
                except Exception:
                    logger.exception(
                        "[wconcept] 브랜드 덤프 실패: %s page=%d",
                        brand_label, page,
                    )
                    break

                if not items:
                    break

                new_in_page = 0
                for item in items:
                    item_cd = str(item.get("product_id") or "")
                    if not item_cd or item_cd in seen_item_cds:
                        continue
                    seen_item_cds.add(item_cd)
                    item["_brand_label"] = brand_label
                    item["_brand_keyword"] = brand_keyword
                    products.append(item)
                    new_in_page += 1

                # 페이지 아이템이 page_size 미만이면 마지막 페이지로 간주
                if len(items) < self._page_size:
                    break
                # 전부 중복이면 더 돌 이유 없음
                if new_in_page == 0:
                    break

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[wconcept] 카탈로그 덤프 완료: %d건", len(products))
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

    def _build_collect_row(
        self, item: dict, model_no: str
    ) -> tuple[str, str, str, str, str]:
        """미등재 신상 → batch flush 용 row 튜플."""
        item_cd = str(item.get("product_id") or "")
        return (
            normalize_model_number(model_no),
            item.get("brand") or item.get("_brand_label") or "",
            item.get("name") or "",
            self.source_name,
            _build_url(item_cd) if item_cd else "",
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], WconceptMatchStats]:
        """덤프된 W컨셉 아이템 → 크림 DB 매칭 → CandidateMatched publish.

        W컨셉 상품 dict 는 크롤러 `_parse_search_response` 가 이미
        상품명에서 모델번호를 추출해 `model_number` 필드에 넣어둔다.
        여기선 그 필드를 직접 신뢰하고, 비어있으면 no_model_number 로 집계.
        """
        stats = WconceptMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

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

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, model_from))
                continue

            # 매칭 가드 — 크림 이름 vs 소싱 이름 키워드 비교
            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[wconcept] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[wconcept] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("price") or 0)
            item_cd = str(item.get("product_id") or "")
            url = item.get("url") or _build_url(item_cd)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[wconcept] 비정수 kream_product_id 스킵: %r",
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

        if pending_collect:
            try:
                inserted = await aenqueue_collect_batch(self._db_path, pending_collect)
                stats.collected_to_queue += inserted
            except Exception:
                logger.warning(
                    "[wconcept] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[wconcept] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = ["WconceptAdapter", "WconceptMatchStats", "DEFAULT_BRAND_KEYWORDS"]
