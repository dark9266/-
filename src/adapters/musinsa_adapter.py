"""무신사 푸시 어댑터 (Phase 2.4).

v3 푸시 전환의 첫 실제 어댑터. 기존 파일럿 자산
`scripts/push_dump_full.py` 의 로직(카테고리 덤프 → PB/품절 필터 →
이름 선매칭 → 크림 DB 매칭 → collect_queue 적재)을 재구성해
이벤트 버스에 `CatalogDumped` / `CandidateMatched` 를 발행한다.

설계 원칙:
    - 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
    - HTTP 레이어는 기존 `src.crawlers.musinsa_httpx.musinsa_crawler` 를
      그대로 사용 (재작성 금지). 테스트에서는 생성자 주입으로 교체.
    - 매칭 가드(콜라보/서브타입) 는 `src.core.matching_guards` 공통 모듈 재사용.
    - 가격 새너티는 이 단계에서 크림 sell_now 를 모르므로 스킵 —
      수익 단계 consumer 에서 수행.
    - 크림 실호출 금지 (본 어댑터는 로컬 DB 매칭만 수행).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect_batch
from src.adapters._size_helpers import fetch_in_stock_sizes
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import extract_model_from_name, normalize_model_number

logger = logging.getLogger(__name__)


# ─── 무신사 PB 블랙리스트 (push_dump_full.py 와 동일) ──────────
PB_BLACKLIST_SLUGS: frozenset[str] = frozenset({
    "musinsastandard",
    "musinsa",
    "muztd",
    "muztdstandard",
    "mmlg",
    "musinsamen",
    "musinsawomen",
    "musinsasports",
    "musinsabasic",
    "musinsacollection",
    "musinsaedition",
    "musinsaoriginal",
    "musinsaoutdoor",
})

# 덤프 기본 카테고리 — 크림 커버리지와 실제 교집합이 있는 카테고리만.
# 6개 카테고리 덤프 시 의류/가방 상품명엔 모델번호 regex 매칭 불가 상품이
# 대부분이라 `no_model_number` drop 으로 끝남 → 분모만 부풀리고 matching
# 비율을 왜곡. 크림 커버리지가 확실한 103(신발) 단독 덤프로 축소.
DEFAULT_CATEGORIES: dict[str, str] = {
    "103": "신발",   # hit 26%
    "001": "상의",   # hit 7% — 적은 매칭이라도 정확하면 유지 (사용자 정책)
    "002": "아우터",  # hit 15%
    "004": "가방",   # hit 12%
    "101": "모자",   # hit 6%
}
# 카테고리는 모두 유지 — 인기상품만 파는 게 아니므로 볼륨+정확도 동시 추구.
# 효율 저하는 카테고리 자르기가 아니라 collect_queue 보수화로 해결.

# 덤프 대상 브랜드 — 크림 DB 에 커버리지가 있고 goodsName 에 모델번호가
# 노출되는 브랜드만. 카테고리 전체 덤프(sortCode=NEW)는 모델번호 없는
# 브랜드(fitflop/gap/tommyjeans 등) 가 분모를 부풀리고 신상 중심이라 크림
# 미등재 비율이 높다. 브랜드별 `brand=X&sortCode=POPULAR` 쿼리가 크림 등재
# 상품과 교집합이 훨씬 크다(실측 2026-04-14).
DEFAULT_BRANDS: tuple[str, ...] = (
    "nike",
    "jordan",
    "adidas",
    "newbalance",
    "puma",
    "converse",
    "reebok",
    "vans",
    "asics",
    "hoka",
    "salomon",
    "arcteryx",
    "thenorthface",
    "adidasgolf",
    "newera",
)


def _slug(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", (s or "").lower())


def _is_pb(brand: str, brand_name: str) -> bool:
    a, b = _slug(brand), _slug(brand_name)
    for pb in PB_BLACKLIST_SLUGS:
        if pb and (pb in a or pb in b):
            return True
    return False


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


@dataclass
class MatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    pb_dropped: int = 0
    soldout_dropped: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0
    no_model_number: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "pb_dropped": self.pb_dropped,
            "soldout_dropped": self.soldout_dropped,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
            "no_model_number": self.no_model_number,
        }


class MusinsaAdapter:
    """무신사 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "musinsa"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: dict[str, str] | None = None,
        brands: tuple[str, ...] | None = None,
        max_pages: int = 10,
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
            무신사 HTTP 레이어. 기본값 None → 모듈 전역 `musinsa_crawler` 사용.
            테스트에서는 mock 주입.
        categories:
            덤프할 카테고리 {code: name}. 기본 `DEFAULT_CATEGORIES`.
        max_pages:
            카테고리별 최대 페이지 수 (1페이지 60건).
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories or DEFAULT_CATEGORIES
        self._brands = brands if brands is not None else DEFAULT_BRANDS
        self._max_pages = max_pages

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.musinsa_httpx import musinsa_crawler

        self._http = musinsa_crawler
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """무신사 카탈로그 페이지네이션 덤프.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, 상품 dict 리스트)`. 이벤트는 내부적으로
            `bus.publish` 도 수행한 상태.
        """
        http = await self._get_http()
        products: list[dict] = []
        seen_goods: set[str] = set()

        # 브랜드 시드가 비어있으면 카테고리 전체 덤프 (구 동작 유지)
        if not self._brands:
            for code, name in self._categories.items():
                try:
                    items = await http.fetch_category_listing(
                        code, max_pages=self._max_pages
                    )
                except Exception:
                    logger.exception("[musinsa] 카테고리 덤프 실패: %s %s", code, name)
                    continue
                for item in items:
                    item["_category_code"] = code
                    item["_category_name"] = name
                products.extend(items)
        else:
            # 브랜드별 POPULAR 쿼리 — 카테고리 전체 NEW 보다 크림 등재 교집합이 큼
            for code, name in self._categories.items():
                for brand in self._brands:
                    try:
                        items = await http.fetch_category_listing(
                            code,
                            max_pages=self._max_pages,
                            brand=brand,
                            sort_code="POPULAR",
                        )
                    except Exception:
                        logger.exception(
                            "[musinsa] 브랜드 덤프 실패: code=%s brand=%s", code, brand
                        )
                        continue
                    for item in items:
                        goods_no = str(item.get("goodsNo") or "")
                        if not goods_no or goods_no in seen_goods:
                            continue
                        seen_goods.add(goods_no)
                        item["_category_code"] = code
                        item["_category_name"] = name
                        item["_brand_filter"] = brand
                        products.append(item)

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[musinsa] 카탈로그 덤프 완료: %d건", len(products))
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
            item.get("brandName") or item.get("brand") or "",
            item.get("goodsName") or "",
            self.source_name,
            f"https://www.musinsa.com/products/{item.get('goodsNo') or ''}",
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], MatchStats]:
        """덤프된 상품 리스트 → 크림 DB 매칭 → CandidateMatched publish.

        통계:
            - dumped              : 입력 총량
            - pb_dropped          : 무신사 PB 제외
            - soldout_dropped     : 품절 제외
            - no_model_number     : 상품명에서 모델번호 추출 실패 (매칭 후보 아님)
            - matched             : CandidateMatched publish 성공
            - collected_to_queue  : 미등재 신상 큐 적재 성공
            - skipped_guard       : 콜라보/서브타입 가드 차단
        """
        stats = MatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        for item in products:
            # 품절/PB 필터
            if item.get("isSoldOut"):
                stats.soldout_dropped += 1
                continue
            if _is_pb(item.get("brand", ""), item.get("brandName", "")):
                stats.pb_dropped += 1
                continue

            name = item.get("goodsName") or ""
            model_from_name = extract_model_from_name(name) or ""
            if not model_from_name:
                stats.no_model_number += 1
                continue

            key = _strip_key(model_from_name)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            # Vans 전용 퍼징: 크림은 11자 형식 (VN000EJ9BLK) 과
            # 12자 형식 (VN000CRRBJ41) 혼재. 무신사 goodsName 은 항상 12자
            # (사이즈/variant 꼬리 1 포함). exact miss 시 꼬리 숫자 1자 제거 재시도.
            if kream_row is None and key.startswith("VN") and len(key) >= 12 and key[-1].isdigit():
                kream_row = kream_index.get(key[:-1])
            if kream_row is None:
                # 미등재 신상 → batch flush 대기열
                pending_collect.append(self._build_collect_row(item, model_from_name))
                continue

            # 매칭 가드
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, name):
                logger.info(
                    "[musinsa] 콜라보 가드 차단: kream=%r source=%r",
                    kream_name[:40],
                    name[:40],
                )
                stats.skipped_guard += 1
                continue
            stype_diff = subtype_mismatch(_keyword_set(kream_name), _keyword_set(name))
            if stype_diff:
                logger.info(
                    "[musinsa] 서브타입 가드 차단: source=%r extra=%s",
                    name[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("price") or 0)
            goods_no = str(item.get("goodsNo") or "")
            url = f"https://www.musinsa.com/products/{goods_no}"
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                # 크림 product_id 가 숫자가 아닌 경우 (과거 DB 호환)
                logger.warning(
                    "[musinsa] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            # PDP 실재고 사이즈 수집 — 빈 결과는 무조건 drop (HQ6893 회귀 방지).
            http = await self._get_http()
            available_sizes = await fetch_in_stock_sizes(
                http, goods_no, source_tag="musinsa"
            )
            if not available_sizes:
                logger.info(
                    "[musinsa] PDP 재고 없음 drop: pid=%s model=%s",
                    goods_no,
                    model_from_name,
                )
                stats.soldout_dropped += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_from_name),
                retail_price=price,
                size="",  # 리스팅 단계엔 사이즈 정보 없음 — 수익 consumer 가 보강
                url=url,
                available_sizes=available_sizes,
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
                    "[musinsa] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[musinsa] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = ["MatchStats", "MusinsaAdapter", "DEFAULT_CATEGORIES"]
