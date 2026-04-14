"""29CM 푸시 어댑터 (Phase 3 배치 1).

v3 푸시 전환 — 두 번째 실 어댑터. 무신사 어댑터와 동일한 인터페이스로
29CM 카탈로그를 브랜드 키워드 기반으로 덤프하고 크림 DB 와 매칭한다.

설계 원칙:
    - producer 전용. orchestrator 를 직접 참조하지 않는다.
    - HTTP 레이어는 기존 `src.crawlers.twentynine_cm.twentynine_cm_crawler`
      를 재사용 (크롤러 수정 금지). 테스트에서는 생성자 주입으로 교체.
    - 29CM 는 키워드 검색 API (`search-api.29cm.co.kr/api/v4/products`)
      가 주력이므로 크림 커버리지 브랜드 키워드 목록으로 덤프한다.
    - 매칭 가드(콜라보/서브타입) 는 `src.core.matching_guards` 공통 재사용.
    - 가격 새너티는 수익 단계 consumer 가 담당 (어댑터는 크림 sell_now 모름).
    - 크림 실호출 금지 (어댑터는 로컬 DB 매칭만 수행).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import enqueue_collect_batch
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import extract_model_from_name, normalize_model_number

logger = logging.getLogger(__name__)


# ─── 29CM 대상 브랜드 키워드 ─────────────────────────────────
# 크림 커버리지와 겹치는 핵심 브랜드 — 역방향 BRAND_SOURCES 와 정렬.
# 29CM 는 카테고리 코드 기반 덤프가 아닌 키워드 검색이 주력이라
# 브랜드명 자체를 키워드로 사용한다.
# ⚠️ Jordan 키워드 전체 제외 — 29CM 는 실제 Jordan 스니커를 판매하지
# 않으며, "Jordan" 은 치약 브랜드 "조르단"(100건), "조던"/"Air Jordan"
# 둘 다 JILLSTUART 가방/0건 등 전부 false positive (실측 2026-04-14).
DEFAULT_BRAND_KEYWORDS: tuple[str, ...] = (
    "Nike",
    "Adidas",
    "New Balance",
    "Salomon",
    "Arc'teryx",
    "Vans",
    "Asics",
    "Hoka",
)


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


@dataclass
class MatchStats:
    """29CM 매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0
    no_model_number: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
            "no_model_number": self.no_model_number,
        }


class TwentynineCmAdapter:
    """29CM 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "29cm"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        brand_keywords: tuple[str, ...] | None = None,
        limit_per_keyword: int = 100,
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
            29CM HTTP 레이어. 기본값 None → 모듈 전역 `twentynine_cm_crawler`
            사용. 테스트에서는 mock 주입. `search_products(keyword, limit)`
            메서드만 호출한다 (기존 크롤러의 표면 재사용).
        brand_keywords:
            덤프할 브랜드 키워드 목록. 기본 `DEFAULT_BRAND_KEYWORDS`.
        limit_per_keyword:
            브랜드 키워드당 최대 수집 상품 수.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._brand_keywords = brand_keywords or DEFAULT_BRAND_KEYWORDS
        self._limit_per_keyword = limit_per_keyword

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.twentynine_cm import twentynine_cm_crawler

        self._http = twentynine_cm_crawler
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """29CM 브랜드 키워드 기반 카탈로그 덤프.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, 상품 dict 리스트)`. 이벤트는 내부적으로
            `bus.publish` 도 수행한 상태. 상품 dict 스키마는 기존
            `twentynine_cm_crawler.search_products` 반환 구조를 그대로 사용.
        """
        http = await self._get_http()
        products: list[dict] = []
        seen_ids: set[str] = set()  # 동일 상품 중복 제거 (브랜드 교차 노출)

        for keyword in self._brand_keywords:
            try:
                items = await http.search_products(
                    keyword, limit=self._limit_per_keyword
                )
            except Exception:
                logger.exception("[29cm] 브랜드 덤프 실패: %s", keyword)
                continue
            for item in items:
                pid = str(item.get("product_id") or "")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                item["_brand_keyword"] = keyword
                products.append(item)

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[29cm] 카탈로그 덤프 완료: %d건", len(products))
        return event, products

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        """크림 DB 전체를 모델번호 stripped key 로 인덱스.

        블로킹 sqlite3 — 어댑터 수준에서 단발 실행이라 허용.
        테스트에서는 tmp db 로 고립.
        """
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
        return (
            normalize_model_number(model_no),
            item.get("brand") or "",
            item.get("name") or "",
            self.source_name,
            item.get("url")
            or f"https://www.29cm.co.kr/products/{item.get('product_id') or ''}",
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], MatchStats]:
        """덤프된 상품 리스트 → 크림 DB 매칭 → CandidateMatched publish.

        통계:
            - dumped              : 입력 총량
            - soldout_dropped     : 품절 제외
            - no_model_number     : 상품명에서 모델번호 추출 실패
            - matched             : CandidateMatched publish 성공
            - collected_to_queue  : 미등재 신상 큐 적재 성공
            - skipped_guard       : 콜라보/서브타입 가드 차단
        """
        stats = MatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        for item in products:
            # 품절 필터 (29CM 는 PB 개념이 얕아 PB 필터는 생략)
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            name = item.get("name") or ""
            # 29CM 크롤러가 이미 넣어준 model_number 우선, 없으면 상품명에서 추출
            model_from_item = item.get("model_number") or ""
            model_from_name = (
                model_from_item or extract_model_from_name(name) or ""
            )
            if not model_from_name:
                stats.no_model_number += 1
                continue

            key = _strip_key(model_from_name)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, model_from_name))
                continue

            # 매칭 가드
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, name):
                logger.info(
                    "[29cm] 콜라보 가드 차단: kream=%r source=%r",
                    kream_name[:40],
                    name[:40],
                )
                stats.skipped_guard += 1
                continue
            stype_diff = subtype_mismatch(_keyword_set(kream_name), _keyword_set(name))
            if stype_diff:
                logger.info(
                    "[29cm] 서브타입 가드 차단: source=%r extra=%s",
                    name[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("price") or 0)
            url = (
                item.get("url")
                or f"https://www.29cm.co.kr/products/{item.get('product_id') or ''}"
            )
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[29cm] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_from_name),
                retail_price=price,
                size="",  # 리스팅 단계엔 사이즈 정보 없음 — 수익 consumer 가 보강
                url=url,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        if pending_collect:
            try:
                inserted = enqueue_collect_batch(self._db_path, pending_collect)
                stats.collected_to_queue += inserted
            except Exception:
                logger.warning(
                    "[29cm] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[29cm] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = ["DEFAULT_BRAND_KEYWORDS", "MatchStats", "TwentynineCmAdapter"]
