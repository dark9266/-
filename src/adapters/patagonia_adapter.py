"""파타고니아 코리아 푸시 어댑터 (Phase 3 배치 6).

설계 원칙
----------
* 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
* HTTP 레이어는 외부 주입. 기본값은 `_DefaultPatagoniaHttp` (싱글톤
  크롤러 재사용). 테스트에서는 `fetch_catalog` 를 제공하는 mock 주입.
* 매칭 가드(콜라보/서브타입) 는 `src.core.matching_guards` 재사용.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.

매칭 전략
----------
파타고니아 크림 DB 는 **5자리 스타일 코드**(예: ``24142``)로 저장되고,
한 크림 상품에 복수 코드(``85240/85241``)가 들어 있는 경우도 있다.
사이트 pcode(``44937R5``)에서 앞 5자리(``44937``)만 스타일 키로 뽑아
크림 DB 의 분리된 토큰 각각과 대조한다 (``_build_kream_index``).

사이즈는 풀 덤프 단계에서 옵션까지 내려오므로, 최초 매칭에서
사이즈별 재고 리스트까지 확보 가능. 현재는 사이즈 0 (리스팅 단계와
동일) 로 전파하고, 사이즈별 분해는 하류 소비자(오케스트레이터)에서
필요 시 재구성.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


@dataclass
class PatagoniaMatchStats:
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


class PatagoniaAdapter:
    """파타고니아 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "patagonia"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: tuple[str, ...] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로. `kream_products`·`kream_collect_queue`
            테이블 조회·적재.
        http_client:
            HTTP 레이어. `fetch_catalog(categories: tuple[str,...]|None)
            -> list[dict]` 메서드만 제공하면 된다. 기본값 None →
            내부 싱글톤 `patagonia_crawler` 재사용.
        categories:
            덤프할 카테고리 cid 튜플. 기본 None → 크롤러 기본값.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 로드 (테스트 시 실모듈 import 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.patagonia import patagonia_crawler

        self._http = _DefaultPatagoniaHttp(patagonia_crawler)
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """전체 카탈로그 덤프 → CatalogDumped publish."""
        http = await self._get_http()
        try:
            catalog = await http.fetch_catalog(self._categories)
        except Exception:
            logger.exception("[patagonia] 카탈로그 덤프 실패")
            catalog = []

        catalog = list(catalog or [])
        event = CatalogDumped(
            source=self.source_name,
            product_count=len(catalog),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[patagonia] 카탈로그 덤프 완료: %d건", len(catalog))
        return event, catalog

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        """크림 DB → {5자리 style_code: row}.

        한 크림 상품에 복수 코드(``85240/85241``)가 있는 경우 각 코드를
        인덱스에 등록한다. 중복 key 는 먼저 본 row 유지.
        """
        from src.crawlers.patagonia import split_kream_model_numbers

        conn = sqlite3.connect(self._db_path)
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
            codes = split_kream_model_numbers(row["model_number"] or "")
            if not codes:
                continue
            drow = dict(row)
            for code in codes:
                index.setdefault(code, drow)
        return index

    def _enqueue_collect(self, item: dict, style_code: str) -> bool:
        """미등재 신상 → kream_collect_queue INSERT OR IGNORE."""
        conn = sqlite3.connect(self._db_path)
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO kream_collect_queue "
                "(model_number, brand_hint, name_hint, source, source_url) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    normalize_model_number(style_code),
                    "Patagonia",
                    item.get("name") or item.get("name_kr") or "",
                    self.source_name,
                    item.get("url") or "",
                ),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()

    async def match_to_kream(
        self, catalog: list[dict]
    ) -> tuple[list[CandidateMatched], PatagoniaMatchStats]:
        """덤프된 아이템 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = PatagoniaMatchStats(dumped=len(catalog))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        seen_styles: set[str] = set()

        for item in catalog:
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            style_code = (item.get("style_code") or "").strip().upper()
            if not style_code:
                stats.no_model_number += 1
                continue
            if style_code in seen_styles:
                continue
            seen_styles.add(style_code)

            kream_row = kream_index.get(style_code)
            if kream_row is None:
                if self._enqueue_collect(item, style_code):
                    stats.collected_to_queue += 1
                continue

            # 매칭 가드 — 크림 이름 vs 소싱 이름 키워드 비교
            source_name_text = item.get("name") or item.get("name_kr") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[patagonia] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[patagonia] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            price = int(item.get("price") or 0)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[patagonia] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(style_code),
                retail_price=price,
                size="",  # 사이즈 분해는 하류 소비자가 item['sizes'] 로 수행
                url=item.get("url") or "",
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[patagonia] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, catalog = await self.dump_catalog()
        _, stats = await self.match_to_kream(catalog)
        return stats.as_dict()


class _DefaultPatagoniaHttp:
    """기본 덤퍼 — 기존 `patagonia_crawler` 싱글톤을 재사용."""

    def __init__(self, crawler: Any) -> None:
        self._crawler = crawler

    async def fetch_catalog(
        self, categories: tuple[str, ...] | None
    ) -> list[dict]:
        return await self._crawler.fetch_catalog(categories)


__all__ = [
    "PatagoniaAdapter",
    "PatagoniaMatchStats",
]
