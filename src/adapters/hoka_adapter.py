"""호카(HOKA) 푸시 어댑터 (Phase 3 배치 5).

본사 Salesforce Commerce Cloud 의 Coveo-Show 엔드포인트를 키워드 순회로
덤프해 **masterID+color** 형태(``1171904-ASRN``)의 SKU 를 크림 DB 와 매칭.
한국 직판이 없어 가격은 USD → KRW 환산(기본 ×1400) 후 저장한다.

설계 포인트
------------
* HOKA 는 상세/재고 API 공개 차단. 검색 결과에 노출되는 것만으로 ``available=True``
  로 간주. 수익 판정은 하류 오케스트레이터가 재검증.
* ``salomon_adapter.py`` 의 인터페이스(``dump_catalog``/``match_to_kream``/
  ``run_once``) 를 그대로 맞춰 Orchestrator 재사용성 확보.
* HTTP 레이어는 생성자 주입 가능 — 테스트에서는 ``fetch_tiles_keyword``
  mock 주입. 기본값 None → ``_DefaultHokaHttp`` (싱글톤 크롤러 재사용).
* 크림 실호출 금지. 로컬 SQLite 만 조회.
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


# HOKA 모델번호: ``masterID(7자리)-COLOR(대문자 2~6)``
HOKA_SKU_RE = re.compile(r"^\d{7}-[A-Z]{2,6}$")

# 환율 — 어댑터 생성자에서 override 가능. 1 USD ≒ 1400 KRW 고정 상수.
DEFAULT_USD_TO_KRW: int = 1400

BASE_URL = "https://www.hoka.com"


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


@dataclass
class HokaMatchStats:
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


class HokaAdapter:
    """호카 Coveo-Show 기반 카탈로그 덤프 + 크림 매칭 어댑터.

    덤프 단위는 **tile 1개 = 1 row** (master+color SKU 단위). 사이즈는
    검색 결과에 포함되지 않아 ``size=""`` 로 비워둔다.
    """

    source_name: str = "hoka"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        keywords: tuple[str, ...] | None = None,
        usd_to_krw: int = DEFAULT_USD_TO_KRW,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. ``CatalogDumped``·``CandidateMatched`` 를 publish.
        db_path:
            크림 DB SQLite 경로.
        http_client:
            HTTP 레이어. ``fetch_tiles_keyword(keyword: str) -> list[dict]``
            메서드를 제공해야 한다. dict 는 ``HokaTile.as_dict()`` 스키마.
            기본값 None → 내부 기본 덤퍼 사용.
        keywords:
            순회할 검색 키워드. 기본값 None → ``HOKA_SEARCH_KEYWORDS``.
        usd_to_krw:
            USD → KRW 환산 배수. 수익 판정은 하류에서 재검증하므로 고정
            상수로 충분.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._keywords = keywords
        self._usd_to_krw = usd_to_krw

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 로드 (테스트 시 실모듈 import 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.hoka import HOKA_SEARCH_KEYWORDS, hoka_crawler

        if self._keywords is None:
            self._keywords = HOKA_SEARCH_KEYWORDS
        self._http = _DefaultHokaHttp(hoka_crawler)
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프 — 키워드 순회
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """키워드 리스트를 순회해 tile 단위 flat list 덤프.

        중복 SKU 는 dedup. 반환 dict 구조:
            {
                "sku": "1176251-FCG",
                "master_id": "1176251",
                "color_code": "FCG",
                "name": "Mach Remastered",
                "url": "https://www.hoka.com/en/us/...",
                "price_usd": 170.0,
                "price_krw": 238000,
                "available": True,
            }
        """
        http = await self._get_http()

        if self._keywords is None:
            from src.crawlers.hoka import HOKA_SEARCH_KEYWORDS

            self._keywords = HOKA_SEARCH_KEYWORDS

        dedup: dict[str, dict] = {}
        for kw in self._keywords:
            try:
                tiles = await http.fetch_tiles_keyword(kw)
            except Exception:
                logger.exception("[hoka] Coveo 덤프 실패 keyword=%s", kw)
                continue

            for t in tiles or []:
                sku = (t.get("sku") or "").strip().upper()
                if not sku or sku in dedup:
                    continue
                try:
                    price_usd = float(t.get("price_usd") or 0.0)
                except (TypeError, ValueError):
                    price_usd = 0.0
                dedup[sku] = {
                    "sku": sku,
                    "master_id": (t.get("master_id") or "").strip(),
                    "color_code": (t.get("color_code") or "").strip(),
                    "name": t.get("name") or "",
                    "url": t.get("url") or "",
                    "price_usd": price_usd,
                    "price_krw": int(round(price_usd * self._usd_to_krw)),
                    "available": bool(t.get("available", True)),
                }

        variants = list(dedup.values())
        event = CatalogDumped(
            source=self.source_name,
            product_count=len(variants),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[hoka] 카탈로그 덤프 완료: %d tiles", len(variants))
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

    def _build_collect_row(
        self, item: dict, model_no: str
    ) -> tuple[str, str, str, str, str]:
        """미등재 신상 → batch flush 용 row 튜플."""
        return (
            normalize_model_number(model_no),
            "HOKA",
            item.get("name") or "",
            self.source_name,
            item.get("url") or "",
        )

    async def match_to_kream(
        self, variants: list[dict]
    ) -> tuple[list[CandidateMatched], HokaMatchStats]:
        """덤프된 tile 리스트 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = HokaMatchStats(dumped=len(variants))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []
        seen_skus: set[str] = set()

        for item in variants:
            if not item.get("available", True):
                stats.soldout_dropped += 1
                continue

            sku = (item.get("sku") or "").strip().upper()
            if not sku:
                stats.no_model_number += 1
                continue
            if not HOKA_SKU_RE.match(sku):
                stats.invalid_sku += 1
                continue
            if sku in seen_skus:
                continue
            seen_skus.add(sku)

            key = _strip_key(sku)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, sku))
                continue

            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[hoka] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[hoka] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            price_krw = int(item.get("price_krw") or 0)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[hoka] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(sku),
                retail_price=price_krw,
                size="",  # HOKA 는 검색 단계에서 사이즈 미노출
                url=item.get("url") or "",
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
                    "[hoka] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[hoka] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, variants = await self.dump_catalog()
        _, stats = await self.match_to_kream(variants)
        return stats.as_dict()


class _DefaultHokaHttp:
    """기본 덤퍼 — 기존 ``hoka_crawler`` 싱글톤을 재사용.

    테스트는 이 클래스 대신 ``fetch_tiles_keyword`` 를 제공하는 mock 을 주입.
    """

    def __init__(self, crawler: Any) -> None:
        self._crawler = crawler

    async def fetch_tiles_keyword(self, keyword: str) -> list[dict]:
        # 크롤러는 HokaTile dataclass 리스트를 반환하는 내부 API 를 쓰므로
        # 여기서는 search_products (dict 결과) 를 사용해 스키마를 통일한다.
        results = await self._crawler.search_products(keyword, limit=50)
        out: list[dict] = []
        for r in results:
            sku = (r.get("model_number") or "").strip().upper()
            master = sku.split("-", 1)[0] if "-" in sku else sku
            color = sku.split("-", 1)[1] if "-" in sku else ""
            # search_products 는 가격을 KRW 로 변환해 돌려주지만 어댑터는
            # USD → KRW 재변환을 원함. 여기서는 KRW 를 우선 채택.
            price_krw = int(r.get("price") or 0)
            price_usd = round(price_krw / DEFAULT_USD_TO_KRW, 2) if price_krw else 0.0
            out.append({
                "sku": sku,
                "master_id": master,
                "color_code": color,
                "name": r.get("name") or "",
                "url": r.get("url") or "",
                "price_usd": price_usd,
                "available": not r.get("is_sold_out", False),
            })
        return out


__all__ = [
    "DEFAULT_USD_TO_KRW",
    "HOKA_SKU_RE",
    "HokaAdapter",
    "HokaMatchStats",
]
