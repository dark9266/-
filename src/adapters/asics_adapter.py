"""Asics 한국 공식몰 푸시 어댑터 (Phase 3 배치 6).

아식스 한국몰(``www.asics.co.kr``) 카탈로그를 카테고리 순회로 덤프한 뒤
상품 상세에서 모델번호(``\\d{4}[A-Z]\\d{3}-\\d{3}``, 예: ``1203A879-021``)
를 추출해 크림 DB 와 매칭한다. 크림 DB 에 등재된 Asics 2,470개 상품의
SKU 포맷과 직접 key 매칭이 가능해 이름 매칭/fuzzy 는 사용하지 않는다.

설계 포인트
------------
* 단일 브랜드(Asics) 직영 덤프 — salomon_adapter / hoka_adapter 와 동일
  인터페이스(``dump_catalog``/``match_to_kream``/``run_once``) 유지.
* 2단계 덤프:
    1) 카테고리 페이지에서 ``/p/{id}`` 타일 수집 (dedup)
    2) 각 tile 에 대해 상세 페이지 호출 → 모델번호/사이즈 재고 보강
* 사이즈별 재고 확인: ``sizes`` 리스트 내 ``available=True`` 가 하나도
  없으면 품절 처리 (안전 기본값). 상세에 사이즈가 아예 없으면 True.
* 모델번호 정규식 불통과 tile 은 ``no_model_number`` 통계로 적재만 되고
  이벤트 발행은 안 함 — 수집 큐에도 넣지 않음(불량 key 주입 방지).
* HTTP 레이어는 생성자 주입 가능 — 테스트는 mock 주입. 기본값 None →
  싱글톤 ``asics_crawler`` 래퍼 사용.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
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


# Asics 모델번호: 4자리 숫자 + 1문자 + 3자리 숫자 + `-` + 3자리 색상코드.
ASICS_SKU_RE = re.compile(r"^\d{4}[A-Z]\d{3}-\d{3}$")

BASE_URL = "https://www.asics.co.kr"


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(product_id: str) -> str:
    return f"{BASE_URL}/p/{product_id}" if product_id else ""


@dataclass
class AsicsMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    no_model_number: int = 0
    invalid_sku: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "no_model_number": self.no_model_number,
            "invalid_sku": self.invalid_sku,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
        }


class AsicsAdapter:
    """Asics 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터.

    덤프 단위는 **product master 1개 = 1 row**. 사이즈는 각 master 의
    ``sizes`` 필드 안에 dict 리스트로 보존된다. 매칭 단계에서는 대표
    사이즈(첫 available=True) 를 ``CandidateMatched.size`` 로 전달.
    """

    source_name: str = "asics"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: tuple[str, ...] | None = None,
        max_categories: int = 50,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. ``CatalogDumped``·``CandidateMatched`` 를 publish.
        db_path:
            크림 DB SQLite 경로. ``kream_products`` / ``kream_collect_queue``
            테이블 조회·적재.
        http_client:
            Asics HTTP 레이어. 다음 두 메서드 제공 필수:
              - ``fetch_category_products(category_code: str) -> list[dict]``
              - ``fetch_product_detail(product_id: str) -> dict``
            기본값 None → 내부 기본 덤퍼(``_DefaultAsicsHttp``) 사용.
        categories:
            순회할 카테고리 코드 튜플. None → ``ASICS_CATEGORY_CODES``.
        max_categories:
            안전 상한 — 혹시 모를 런어웨이 방지.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories
        self._max_categories = max_categories

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.asics import ASICS_CATEGORY_CODES, asics_crawler

        if self._categories is None:
            self._categories = ASICS_CATEGORY_CODES
        self._http = _DefaultAsicsHttp(asics_crawler)
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프 — 카테고리 순회 + 상세 보강
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """카테고리 순회 → tile dedup → 상세 보강 → flat list 반환.

        반환 dict 구조:
            {
                "product_id": "20406",
                "name": "젤 누노비키",
                "url": "https://www.asics.co.kr/p/20406",
                "model_number": "1203A879-021",
                "price_krw": 159000,
                "sizes": [{"size": "270", "available": True}, ...],
                "available": True,
            }
        """
        http = await self._get_http()

        if self._categories is None:
            from src.crawlers.asics import ASICS_CATEGORY_CODES

            self._categories = ASICS_CATEGORY_CODES

        cats = list(self._categories)[: self._max_categories]

        dedup: dict[str, dict] = {}
        for code in cats:
            try:
                tiles = await http.fetch_category_products(code)
            except Exception:
                logger.exception("[asics] 카테고리 덤프 실패 code=%s", code)
                continue
            for t in tiles or []:
                pid = (t.get("product_id") or "").strip()
                if not pid or pid in dedup:
                    continue
                dedup[pid] = {
                    "product_id": pid,
                    "name": t.get("name") or "",
                    "url": t.get("url") or _build_url(pid),
                    "price_krw": int(t.get("price_krw") or 0),
                    "model_number": "",
                    "sizes": [],
                    "available": True,
                }

        # 상세 보강 — product master 당 1회 상세 호출
        for pid, item in dedup.items():
            try:
                detail = await http.fetch_product_detail(pid)
            except Exception:
                logger.exception("[asics] 상세 보강 실패 pid=%s", pid)
                continue
            if not detail:
                continue
            model = (detail.get("model_number") or "").strip().upper()
            if model:
                item["model_number"] = model
            name = detail.get("name") or ""
            if name and not item.get("name"):
                item["name"] = name
            dprice = int(detail.get("price_krw") or 0)
            if dprice and not item.get("price_krw"):
                item["price_krw"] = dprice
            sizes = detail.get("sizes") or []
            if sizes:
                item["sizes"] = [dict(s) for s in sizes]
                item["available"] = any(s.get("available") for s in sizes)
            else:
                # 사이즈 정보가 없으면 상세 필드의 available 신호 사용
                item["available"] = bool(detail.get("available", True))

        variants = list(dedup.values())
        event = CatalogDumped(
            source=self.source_name,
            product_count=len(variants),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[asics] 카탈로그 덤프 완료: %d products", len(variants))
        return event, variants

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        """크림 DB 를 모델번호 stripped key 로 인덱스."""
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
        return enqueue_collect(
            self._db_path,
            model_number=normalize_model_number(model_no),
            brand_hint="Asics",
            name_hint=item.get("name") or "",
            source=self.source_name,
            source_url=item.get("url") or _build_url(item.get("product_id") or ""),
        )

    async def match_to_kream(
        self, variants: list[dict]
    ) -> tuple[list[CandidateMatched], AsicsMatchStats]:
        """덤프된 master 리스트 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = AsicsMatchStats(dumped=len(variants))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        seen_skus: set[str] = set()

        for item in variants:
            if not item.get("available", True):
                stats.soldout_dropped += 1
                continue

            raw_sku = (item.get("model_number") or "").strip().upper()
            if not raw_sku:
                stats.no_model_number += 1
                continue
            if not ASICS_SKU_RE.match(raw_sku):
                stats.invalid_sku += 1
                continue
            if raw_sku in seen_skus:
                continue
            seen_skus.add(raw_sku)

            key = _strip_key(raw_sku)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                if self._enqueue_collect(item, raw_sku):
                    stats.collected_to_queue += 1
                continue

            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[asics] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[asics] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            price = int(item.get("price_krw") or 0)
            url = item.get("url") or _build_url(item.get("product_id") or "")
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[asics] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            # 대표 사이즈 — 첫 available=True, 없으면 첫 사이즈, 없으면 빈 문자열
            rep_size = ""
            for s in item.get("sizes") or []:
                if s.get("available"):
                    rep_size = str(s.get("size") or "")
                    break
            if not rep_size and item.get("sizes"):
                rep_size = str((item["sizes"][0] or {}).get("size") or "")

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(raw_sku),
                retail_price=price,
                size=rep_size,
                url=url,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[asics] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, variants = await self.dump_catalog()
        _, stats = await self.match_to_kream(variants)
        return stats.as_dict()


class _DefaultAsicsHttp:
    """기본 덤퍼 — 기존 ``asics_crawler`` 싱글톤을 그대로 재사용.

    테스트는 이 클래스 대신 ``fetch_category_products`` /
    ``fetch_product_detail`` 두 메서드를 제공하는 mock 을 주입한다.
    """

    def __init__(self, crawler: Any) -> None:
        self._crawler = crawler

    async def fetch_category_products(self, category_code: str) -> list[dict]:
        return await self._crawler.fetch_category_products(category_code)

    async def fetch_product_detail(self, product_id: str) -> dict:
        return await self._crawler.fetch_product_detail(product_id)


__all__ = [
    "ASICS_SKU_RE",
    "AsicsAdapter",
    "AsicsMatchStats",
]
