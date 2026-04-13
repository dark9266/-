"""반스 푸시 어댑터 (Phase 3 배치 2).

v3 푸시 전환 세 번째 실제 어댑터. 반스 공식몰(vans.co.kr)의 Topick Commerce
검색/카테고리 JSON API 를 페이지네이션 덤프한 뒤 각 상품의 `model` 필드를
크림 모델번호로 직접 매칭한다.

설계 포인트
------------
* 반스 강점: 검색 API 응답의 `model` 필드가 사실상 상품의 유일 식별자(SKU)
  이므로 상품명 regex 파싱 불필요. 크림 DB 에 해당 모델이 존재하면
  `CandidateMatched`, 없으면 `kream_collect_queue` 적재.
* 어댑터는 producer 전용. HTTP 레이어는 `src.crawlers.vans.vans_crawler`
  (하위 메서드 `search_products`) 를 재사용하며 테스트에서는 mock 주입.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
* 매칭 가드는 `src.core.matching_guards` 공통 모듈 재사용.
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
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


# 기본 덤프 키워드 — 반스 카탈로그를 브랜드 전량으로 훑기 위한 시드.
# Topick Commerce 검색 API 는 브랜드 전체 리스팅 엔드포인트가 제한적이라
# 대표 실루엣 키워드로 페이지네이션 대체한다.
# 영어/공백 키워드는 자체 검색 매칭 실패 — 한글 + compact 만 사용.
# (직접 probe 결과: "Old Skool" 0건, "OLDSKOOL" 2건, "올드스쿨" 2건)
# TODO: vans 검색 카탈로그 커버리지 매우 부족 — Phase 4 에서 카테고리/브랜드
# listing 엔드포인트로 어댑터 재설계 필요.
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "VANS",
    "올드스쿨",
    "스케이트하이",
    "Sk8-Hi",
    "OLDSKOOL",
)


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(model: str) -> str:
    return f"https://www.vans.co.kr/PRODUCT/{model}" if model else ""


@dataclass
class VansMatchStats:
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


class VansAdapter:
    """반스 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "vans"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        keywords: tuple[str, ...] | None = None,
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
            반스 HTTP 레이어. 기본값 None → 모듈 전역 `vans_crawler` 사용.
            테스트에서는 `search_products(keyword, limit)` 를 제공하는 객체 주입.
        keywords:
            덤프할 키워드 시드. 기본 `DEFAULT_KEYWORDS`.
        limit_per_keyword:
            키워드당 최대 수집 건수 (반스 검색 API 1회 호출 한도).
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._keywords = keywords or DEFAULT_KEYWORDS
        self._limit_per_keyword = limit_per_keyword

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.vans import vans_crawler

        self._http = vans_crawler
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """반스 카탈로그 덤프 — 검색 seed + 카테고리 리스팅 병행.

        두 경로를 합쳐 discovery 커버리지를 높인다:

        1. ``search_products`` 키워드 seed: 가격·이미지·브랜드 풀정보 포함
           (매칭 candidate 로 직접 이어짐)
        2. ``fetch_category_models`` 카테고리 HTML 리스팅: 모델코드 + 이름만
           (kream_collect_queue discovery 용 — 가격 없음, price=0)

        경로 1 이 먼저 실행되어 가격 있는 아이템을 선점하고, 경로 2 는 이미
        본 모델을 dedup 으로 건너뛴다. 경로 2 에서 새로 발견된 모델은
        price=0 으로 리스트에 추가되는데, 크림 DB 매칭 시 collect_queue 에만
        쌓이고 CandidateMatched 는 발행되지 않는다 (price=0 은 profit 계산
        불가 → match_to_kream 가 드랍).
        """
        http = await self._get_http()
        products: list[dict] = []
        seen_models: set[str] = set()

        # 경로 1: 브랜드/실루엣 키워드 seed 검색 (풀정보)
        for keyword in self._keywords:
            try:
                items = await http.search_products(
                    keyword, limit=self._limit_per_keyword
                )
            except Exception:
                logger.exception("[vans] 키워드 덤프 실패: %s", keyword)
                continue

            for item in items or []:
                model = (item.get("model_number") or item.get("product_id") or "").strip()
                if model and model in seen_models:
                    continue
                if model:
                    seen_models.add(model)
                item["_keyword"] = keyword
                products.append(item)

        # 경로 2: 카테고리 HTML 리스팅 (discovery 전용)
        for category in ("SHOES", "MEN", "WOMEN", "KIDS"):
            try:
                cat_items = await http.fetch_category_models(
                    category=category, max_pages=30
                )
            except Exception:
                logger.exception("[vans] 카테고리 덤프 실패: %s", category)
                continue

            for item in cat_items or []:
                model = (item.get("model_number") or "").strip()
                if not model or model in seen_models:
                    continue
                seen_models.add(model)
                # price=0 은 match_to_kream 가 CandidateMatched 대신 queue 로 보냄
                products.append(
                    {
                        "model_number": model,
                        "name": item.get("name") or "",
                        "brand": item.get("brand") or "Vans",
                        "url": item.get("url") or _build_url(model),
                        "price": 0,
                        "is_sold_out": False,
                        "_category": category,
                    }
                )

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[vans] 카탈로그 덤프 완료: %d건", len(products))
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
        return (
            normalize_model_number(model_no),
            item.get("brand") or "Vans",
            item.get("name") or "",
            self.source_name,
            _build_url(model_no),
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], VansMatchStats]:
        """덤프된 반스 상품 리스트 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = VansMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        for item in products:
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            model_no = (item.get("model_number") or item.get("product_id") or "").strip()
            if not model_no:
                stats.no_model_number += 1
                continue

            key = _strip_key(model_no)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, model_no))
                continue

            # 매칭 가드 — 크림 이름 vs 소싱 이름 키워드 비교
            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[vans] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[vans] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            price = int(item.get("price") or 0)
            url = item.get("url") or _build_url(model_no)
            # 카테고리 HTML discovery 경로는 price=0 — 매칭 candidate 발행 스킵
            # (profit 계산 불가). 다음 사이클 search_products 경로에서 가격이
            # 붙으면 정상 매칭.
            if price <= 0:
                stats.skipped_guard += 1
                continue
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[vans] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_no),
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
                    "[vans] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[vans] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = ["VansAdapter", "VansMatchStats", "DEFAULT_KEYWORDS"]
