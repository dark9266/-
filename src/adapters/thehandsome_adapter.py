"""더한섬닷컴 푸시 어댑터 (Phase 3 배치 5).

더한섬닷컴(thehandsome.com)은 한섬(Handsome Corp.) 공식 통합몰. EQL 이
편집숍이라면 thehandsome 은 TIME/SYSTEM/BALLY/LANVIN/FEAR OF GOD 등
럭셔리·디자이너 브랜드 본진이다. 본 어댑터는 Spring Boot REST JSON API
(`/api/display/1/ko/category/categoryGoodsList`, `/api/goods/1/ko/goods/`)
를 통해 **브랜드 화이트리스트** 단위로 카탈로그를 GET 만으로 덤프하고,
모델번호(한섬 ERP 코드 goodsNo) 기준 크림 DB 매칭을 수행한다.

설계 포인트
------------
* 한섬 goodsNo 포맷(예: `ON2G3ABG001NWU`)은 크림의 나이키/아디다스 SKU 와
  스키마가 완전히 달라 **exact match 전용**. 대부분 `collected_to_queue` 로
  적재돼 향후 크림 럭셔리 확장 대비 자산이 된다.
* 매칭 가드(콜라보/서브타입) 는 `src.core.matching_guards` 재사용.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
* POST 미사용 — 모든 엔드포인트 GET. 읽기 전용 정책 엄수.
* producer 전용, `src/adapters/__init__.py` 미수정 (병렬 충돌 방지).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


BASE_URL = "https://www.thehandsome.com"
LOCALE = "ko"


# 기본 브랜드 화이트리스트 — 크림 DB 와 교집합 가능성이 있는 럭셔리/디자이너
# 한섬 계열. 브랜드 리스트 API 의 `brandNm` 과 대소문자 무관 비교.
DEFAULT_BRAND_WHITELIST: tuple[str, ...] = (
    "BALLY",
    "LANVIN",
    "LANVIN COLLECTION",
    "LANVIN BLANC",
    "FEAR OF GOD",
    "MOOSE KNUCKLES",
    "OUR LEGACY",
    "RE/DONE",
    "DKNY",
    "VERONICA BEARD",
    "NILI LOTAN",
    "AGNONA",
    "ASPESI",
    "TEN C",
)


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(product_id: str) -> str:
    return f"{BASE_URL}/{LOCALE}/PM/productDetail/{product_id}" if product_id else ""


@dataclass
class ThehandsomeMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    no_model_number: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0
    brand_filtered: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "no_model_number": self.no_model_number,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
            "brand_filtered": self.brand_filtered,
        }


class ThehandsomeAdapter:
    """더한섬닷컴 카탈로그 덤프 + 크림 매칭 + 이벤트 발행."""

    source_name: str = "thehandsome"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        crawler: Any = None,
        *,
        brand_whitelist: tuple[str, ...] | None = None,
        max_pages: int = 5,
        page_size: int = 40,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` publish.
        db_path:
            크림 DB SQLite 경로.
        crawler:
            더한섬 크롤러. 기본 None → 모듈 전역 `thehandsome_crawler` 로드.
            테스트에서는 `list_brands()` 와 `dump_brand_goods(brand_no, ...)`
            를 제공하는 객체를 주입.
        brand_whitelist:
            덤프할 브랜드명 튜플. 기본 `DEFAULT_BRAND_WHITELIST`.
        max_pages:
            브랜드별 최대 페이지 수.
        page_size:
            페이지당 아이템 수.
        """
        self._bus = bus
        self._db_path = db_path
        self._crawler = crawler
        self._brand_whitelist = tuple(
            n.upper() for n in (brand_whitelist or DEFAULT_BRAND_WHITELIST)
        )
        self._max_pages = max_pages
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import
    # ------------------------------------------------------------------
    async def _get_crawler(self) -> Any:
        if self._crawler is not None:
            return self._crawler
        from src.crawlers.thehandsome import thehandsome_crawler

        self._crawler = thehandsome_crawler
        return self._crawler

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """화이트리스트 브랜드별 카탈로그 페이지네이션 덤프."""
        crawler = await self._get_crawler()

        try:
            brands = await crawler.list_brands()
        except Exception:
            logger.exception("[thehandsome] 브랜드 리스트 로드 실패")
            brands = []

        # 화이트리스트 교집합
        target_brands: list[dict] = []
        for b in brands or []:
            nm = (b.get("brandNm") or "").strip().upper()
            if nm in self._brand_whitelist:
                target_brands.append(b)
        logger.info(
            "[thehandsome] 덤프 대상 브랜드: %d/%d",
            len(target_brands), len(brands or []),
        )

        products: list[dict] = []
        for b in target_brands:
            brand_no = str(b.get("brandNo") or "")
            brand_nm = b.get("brandNm") or ""
            if not brand_no:
                continue
            try:
                items = await crawler.dump_brand_goods(
                    brand_no,
                    page_size=self._page_size,
                    max_pages=self._max_pages,
                )
            except Exception:
                logger.exception(
                    "[thehandsome] 브랜드 덤프 실패: %s (%s)",
                    brand_nm, brand_no,
                )
                continue
            for it in items:
                it["_brand_label"] = brand_nm
                it["_brand_no"] = brand_no
            products.extend(items)

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[thehandsome] 카탈로그 덤프 완료: %d건", len(products))
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
    ) -> tuple[list[CandidateMatched], ThehandsomeMatchStats]:
        """덤프된 아이템 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = ThehandsomeMatchStats(dumped=len(products))
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

            # 매칭 가드 — 크림 이름 vs 소싱 이름
            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[thehandsome] 콜라보 가드 차단: kream=%r source=%r",
                    kream_name[:40], source_name_text[:40],
                )
                stats.skipped_guard += 1
                continue
            stype_diff = subtype_mismatch(
                _keyword_set(kream_name), _keyword_set(source_name_text)
            )
            if stype_diff:
                logger.info(
                    "[thehandsome] 서브타입 가드 차단: source=%r extra=%s",
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
                    "[thehandsome] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_from),
                retail_price=price,
                size="",
                url=url,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[thehandsome] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = [
    "ThehandsomeAdapter",
    "ThehandsomeMatchStats",
    "DEFAULT_BRAND_WHITELIST",
    "BASE_URL",
]
