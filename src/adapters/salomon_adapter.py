"""살로몬 푸시 어댑터 (Phase 3 배치 2).

살로몬 한국 공식몰(Shopify 기반) 카탈로그를 `/products.json?limit=250&page=N`
페이지네이션으로 전체 덤프한 뒤, Shopify variant 의 `sku`(크림 모델번호 `L+8자리`)
를 **직접** 크림 DB 와 매칭한다.

설계 포인트
------------
* 살로몬 강점: Shopify SKU = 크림 모델번호 (예: `L41195100`). 상품명 regex 파싱
  불필요 — 정규식 ``L\\d{8}`` 로 검증 후 그대로 매칭 키로 사용.
* 콜라보 거의 없음. 그래도 안전을 위해 `matching_guards` 공통 모듈을 호출한다.
* 어댑터는 producer 전용. HTTP 레이어는 생성자 주입으로 교체 가능 —
  기본값 None 이면 모듈 내부 기본 덤퍼를 사용. 테스트에서는 mock 주입.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect_batch
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


# 살로몬 SKU = 크림 모델번호. 세 가지 패턴 존재.
#  * `L\d{8}` (3,553 variants, kream 185건) — 표준 신상 SKU
#  * `LC\d{7}` (1,358 variants, kream 56건) — L'ART 콜라보/의류 SKU
#  * `LD\d{7}` (1,063 variants, kream 3건) — 의류/액세서리 라인. 현재
#    크림 교집합은 3건이지만 collect_queue 후보로 쌓아두면 향후 크림 등재
#    시 자동 회수. invalid_sku 로 버리던 1k 건 구제.
SALOMON_SKU_RE = re.compile(r"^(?:L\d{8}|LC\d{7}|LD\d{7})$")

BASE_URL = "https://salomon.co.kr"


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(handle: str) -> str:
    return f"{BASE_URL}/products/{handle}" if handle else ""


@dataclass
class SalomonMatchStats:
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


class SalomonAdapter:
    """살로몬 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터.

    덤프 단위는 **variant 1개 = 1 row**. Shopify product 는 여러 variant 를
    가질 수 있고 각 variant 가 독립 SKU/사이즈/재고를 갖는다.
    """

    source_name: str = "salomon"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        max_pages: int = 20,
        page_size: int = 250,
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
            살로몬 HTTP 레이어. `fetch_products_page(page: int, limit: int)
            -> list[dict]` 메서드를 제공해야 한다. 기본값 None → 내부 기본
            덤퍼(지연 로드) 사용. 테스트에서는 mock 주입.
        max_pages:
            최대 페이지 수 (안전 상한).
        page_size:
            페이지당 product 수 (Shopify 최대 250).
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._max_pages = max_pages
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import (테스트 시 실모듈 로드 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        # 기본 덤퍼: 기존 크롤러 싱글톤의 httpx 클라이언트 재사용해서
        # products.json 페이지네이션만 수행하는 얇은 래퍼.
        from src.crawlers.salomon import salomon_crawler

        self._http = _DefaultSalomonHttp(salomon_crawler)
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프 — products.json 페이지네이션
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """Shopify products.json 페이지네이션 덤프.

        각 product 의 variants 를 전개해 **variant 단위 flat list** 로 반환.
        반환 dict 구조:
            {
                "handle": str,
                "title": str,
                "sku": str,
                "size": str,
                "price": int,
                "available": bool,
            }
        """
        http = await self._get_http()
        variants_out: list[dict] = []

        for page in range(1, self._max_pages + 1):
            try:
                products = await http.fetch_products_page(
                    page=page, limit=self._page_size
                )
            except Exception:
                logger.exception("[salomon] products.json 덤프 실패 page=%d", page)
                break

            if not products:
                break

            for prod in products:
                handle = (prod.get("handle") or "").strip()
                title = prod.get("title") or ""
                for v in prod.get("variants") or []:
                    sku = (v.get("sku") or "").strip().upper()
                    size = (v.get("option2") or v.get("title") or "").strip()
                    price_raw = v.get("price") or "0"
                    try:
                        price = int(float(price_raw))
                    except (TypeError, ValueError):
                        price = 0
                    variants_out.append({
                        "handle": handle,
                        "title": title,
                        "sku": sku,
                        "size": size,
                        "price": price,
                        "available": bool(v.get("available", False)),
                    })

            # 페이지가 limit 보다 작으면 마지막
            if len(products) < self._page_size:
                break

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(variants_out),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[salomon] 카탈로그 덤프 완료: %d variants", len(variants_out))
        return event, variants_out

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        from src.core.kream_index import get_kream_index
        return get_kream_index(self._db_path).get()

    def _build_collect_row(
        self, item: dict, model_no: str
    ) -> tuple[str, str, str, str, str]:
        """미등재 신상 → batch flush 용 row 튜플."""
        return (
            normalize_model_number(model_no),
            "Salomon",
            item.get("title") or "",
            self.source_name,
            _build_url(item.get("handle") or ""),
        )

    async def match_to_kream(
        self, variants: list[dict]
    ) -> tuple[list[CandidateMatched], SalomonMatchStats]:
        """덤프된 variant 리스트 → 크림 DB 매칭 → CandidateMatched publish.

        살로몬 강점: SKU = 크림 모델번호 직접 사용. ``L\\d{8}`` 정규식으로
        검증한 뒤, 크림 DB 인덱스에 있으면 매칭 가드 통과 후 이벤트 발행.
        없으면 수집 큐로 적재.
        """
        stats = SalomonMatchStats(dumped=len(variants))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        # SKU 단위 dedup — 같은 SKU variant 여러 개(사이즈별)면 1회만 매칭.
        seen_skus: set[str] = set()

        for item in variants:
            if not item.get("available", False):
                stats.soldout_dropped += 1
                continue

            sku = (item.get("sku") or "").strip().upper()
            if not sku:
                stats.no_model_number += 1
                continue
            if not SALOMON_SKU_RE.match(sku):
                stats.invalid_sku += 1
                continue
            if sku in seen_skus:
                continue
            seen_skus.add(sku)

            key = _strip_key(sku)
            if not key:
                stats.no_model_number += 1
                continue

            # 덤프 ledger — 매칭 전 전수 기록 (오프라인 분석용)
            try:
                from src.core.dump_ledger import record_dump_item
                await record_dump_item(
                    self._db_path,
                    source=self.source_name,
                    model_no=sku,
                    name=item.get("name") or "",
                    url=item.get("url") or "",
                )
            except Exception:
                logger.debug("[salomon] dump_ledger 실패 (비치명)")

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, sku))
                continue

            # 매칭 가드 — 살로몬은 콜라보 거의 없지만 일관성 유지를 위해 호출
            source_name_text = item.get("title") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[salomon] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[salomon] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("price") or 0)
            url = _build_url(item.get("handle") or "")
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[salomon] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            variant_size = (item.get("size") or "").strip()
            available_sizes: tuple[str, ...] = (variant_size,) if variant_size else ()
            if not available_sizes:
                logger.info("[salomon] variant 사이즈 없음 drop: sku=%s", sku)
                stats.soldout_dropped += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(sku),
                retail_price=price,
                size=variant_size,
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
                    "[salomon] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[salomon] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, variants = await self.dump_catalog()
        _, stats = await self.match_to_kream(variants)
        return stats.as_dict()


class _DefaultSalomonHttp:
    """기본 덤퍼 — 기존 `salomon_crawler` 의 httpx 클라이언트를 재사용해
    `/products.json` 페이지네이션을 수행하는 얇은 래퍼.

    테스트는 이 클래스 대신 `fetch_products_page` 를 제공하는 mock 을 주입.
    """

    def __init__(self, crawler: Any) -> None:
        self._crawler = crawler

    async def fetch_products_page(
        self, page: int, limit: int = 250
    ) -> list[dict]:
        client = await self._crawler._get_client()
        url = f"{BASE_URL}/products.json"
        resp = await client.get(url, params={"limit": limit, "page": page})
        if resp.status_code != 200:
            logger.warning(
                "[salomon] products.json HTTP %d page=%d",
                resp.status_code,
                page,
            )
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return data.get("products") or []


__all__ = [
    "SalomonAdapter",
    "SalomonMatchStats",
    "SALOMON_SKU_RE",
]
