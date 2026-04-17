"""카시나 푸시 어댑터 (Phase 3 배치 1).

v3 푸시 전환 두 번째 실제 어댑터. 카시나 NHN shopby API 브랜드별 카탈로그를
페이지네이션 덤프한 뒤 `productManagementCd` 를 크림 모델번호로 **직접** 매칭한다.

설계 포인트
------------
* 카시나 강점: Nike / adidas 브랜드는 `productManagementCd` 가 사실상
  크림 포맷(`HF3704-003`, `IB7862-100`) 그대로이므로 상품명 regex 파싱 불필요.
  New Balance / Jordan 등 내부 SKU(`NBPDFF003Z`) 면 크림 DB 인덱스 미스 →
  수집 큐 적재 후보로 처리한다.
* 어댑터는 producer 전용. HTTP 레이어는 `src.crawlers.kasina.kasina_crawler`
  (하위 메서드 `_search_raw`) 를 재사용하며 테스트에서는 mock 주입.
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

from src.adapters._collect_queue import aenqueue_collect_batch
from src.adapters._size_helpers import fetch_in_stock_sizes
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import extract_model_from_name, normalize_model_number

logger = logging.getLogger(__name__)


# ─── 카시나 brandNo (크롤러와 동기화 — 변경 시 양쪽 업데이트) ─────────
BRAND_NIKE = 40331479
BRAND_ADIDAS = 40331389
BRAND_NEWBALANCE = 40331363
BRAND_JORDAN = 40331479  # Jordan은 Nike 브랜드 아래에 포함됨

# 기본 덤프 브랜드 — 크림 커버리지 우선순위 기준
DEFAULT_BRANDS: dict[int, str] = {
    BRAND_NIKE: "Nike",
    BRAND_ADIDAS: "adidas",
    BRAND_NEWBALANCE: "New Balance",
}

# 크림 포맷 모델번호 (예: HF3704-003). 이 패턴이면 Nike/adidas EXACT 매칭 대상.
KREAM_MODEL_RE = re.compile(r"^[A-Z]{1,4}\d{3,5}-\d{2,4}$")


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(product_no: int | str) -> str:
    # 2026-04-17: `/products/{id}` 404 — 실제 라우트 `/product-detail/{id}`.
    return f"https://www.kasina.co.kr/product-detail/{product_no}"


@dataclass
class KasinaMatchStats:
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


class KasinaAdapter:
    """카시나 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "kasina"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        brands: dict[int, str] | None = None,
        max_pages: int = 10,
        page_size: int = 100,
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
            카시나 HTTP 레이어. 기본값 None → 모듈 전역 `kasina_crawler` 사용.
            테스트에서는 `_search_raw` 를 제공하는 객체를 주입.
        brands:
            덤프할 브랜드 {brandNo: name}. 기본 `DEFAULT_BRANDS`.
        max_pages:
            브랜드별 최대 페이지 수.
        page_size:
            페이지당 아이템 수.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._brands = brands or DEFAULT_BRANDS
        self._max_pages = max_pages
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.kasina import kasina_crawler

        self._http = kasina_crawler
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """카시나 브랜드별 카탈로그 페이지네이션 덤프.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, shopby 아이템 dict 리스트)`.
            각 아이템은 `productNo/productName/productManagementCd/...`
            원본 구조를 유지하며 `_brand_no`·`_brand_name` 메타가 추가된다.
        """
        http = await self._get_http()
        products: list[dict] = []

        for brand_no, brand_name in self._brands.items():
            page = 1
            seen_product_nos: set[Any] = set()
            while page <= self._max_pages:
                try:
                    data = await http._search_raw(
                        brand_no=brand_no,
                        page_size=self._page_size,
                        page_number=page,
                    )
                except Exception:
                    logger.exception(
                        "[kasina] 브랜드 덤프 실패: %s(%d) page=%d",
                        brand_name, brand_no, page,
                    )
                    break

                chunk = (data or {}).get("items") or []
                if not chunk:
                    break
                for item in chunk:
                    pno = item.get("productNo")
                    if pno in seen_product_nos:
                        continue
                    seen_product_nos.add(pno)
                    item["_brand_no"] = brand_no
                    item["_brand_name"] = brand_name
                    products.append(item)

                total = int((data or {}).get("totalCount") or 0)
                if total and len(seen_product_nos) >= total:
                    break
                page += 1

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[kasina] 카탈로그 덤프 완료: %d건", len(products))
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
        pno = item.get("productNo")
        return (
            normalize_model_number(model_no),
            item.get("brandNameKo")
            or item.get("brandNameEn")
            or item.get("_brand_name")
            or "",
            item.get("productName") or item.get("productNameEn") or "",
            self.source_name,
            _build_url(pno) if pno is not None else "",
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], KasinaMatchStats]:
        """덤프된 shopby 아이템 → 크림 DB 매칭 → CandidateMatched publish.

        카시나의 구조적 강점 — `productManagementCd` 를 모델번호로 직접 사용
        (상품명 파싱 불필요). 크림 DB 인덱스에 없으면 수집 큐로 적재한다.
        """
        stats = KasinaMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        for item in products:
            if item.get("isSoldOut"):
                stats.soldout_dropped += 1
                continue

            mgmt = (item.get("productManagementCd") or "").strip()
            if not mgmt:
                stats.no_model_number += 1
                continue

            key = _strip_key(mgmt)
            if not key:
                stats.no_model_number += 1
                continue

            # 덤프 ledger — 매칭 전 전수 기록 (오프라인 분석용)
            try:
                from src.core.dump_ledger import record_dump_item
                await record_dump_item(
                    self._db_path,
                    source=self.source_name,
                    model_no=mgmt,
                    name=item.get("productName") or item.get("name") or "",
                    url=item.get("url") or "",
                )
            except Exception:
                logger.debug("[kasina] dump_ledger 실패 (비치명)")

            kream_row = kream_index.get(key)
            match_model = mgmt

            # Fallback 1: productName 에서 모델번호 regex 추출 재시도
            # (카시나 NB 는 mgmt=내부SKU, productName 에 실 NB 코드 노출;
            #  adidas 일반품도 NOS 케이스 일부가 mgmt 대신 productName 에 노출)
            if kream_row is None:
                source_name_probe = (
                    item.get("productName") or item.get("productNameEn") or ""
                )
                name_model = extract_model_from_name(source_name_probe) or ""
                if name_model:
                    name_key = _strip_key(name_model)
                    if name_key and name_key != key:
                        alt = kream_index.get(name_key)
                        if alt is not None:
                            kream_row = alt
                            match_model = name_model

            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, mgmt))
                continue

            # 매칭 가드 — 크림 이름 vs 소싱 이름 키워드 비교
            source_name_text = (
                item.get("productName") or item.get("productNameEn") or ""
            )
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[kasina] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[kasina] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("salePrice") or 0)
            pno = item.get("productNo")
            url = _build_url(pno) if pno is not None else ""
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[kasina] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            # PDP 실재고 사이즈 — 빈 결과 무조건 drop
            http = await self._get_http()
            available_sizes = await fetch_in_stock_sizes(
                http, str(pno or ""), source_tag="kasina"
            )
            if not available_sizes:
                logger.info(
                    "[kasina] PDP 재고 없음 drop: pno=%s model=%s",
                    pno,
                    match_model,
                )
                stats.soldout_dropped += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(match_model),
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
                    "[kasina] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[kasina] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = ["KasinaAdapter", "KasinaMatchStats", "DEFAULT_BRANDS"]
