"""튠 푸시 어댑터 (Phase 3 배치 3).

튠(tune.kr)은 Shopify Storefront GraphQL 기반 편집숍이다. 카탈로그 전수
덤프는 ``products(first, after)`` 커서 페이지네이션으로 수행하고, variant
title(예: ``"IQ3446-010 / 230"``)에서 모델번호·사이즈를 파싱해 **variant
단위** flat list 로 바꾼다. 그 다음 무신사 어댑터와 동일하게 크림 DB
모델번호 인덱스에 붙여 `CandidateMatched` 를 발행하거나,
`kream_collect_queue` 에 미등재 신상으로 쌓는다.

설계 포인트
------------
* 어댑터는 producer 전용 — orchestrator/크림 API 실호출 금지.
* HTTP 레이어는 ``fetch_products_page(cursor, first)`` 만 제공하면 되는
  얇은 인터페이스로 추상화. 기본값 None 이면 `src.crawlers.tune` 싱글톤의
  httpx 클라이언트를 재사용하는 내부 덤퍼(지연 로드)를 사용한다.
  테스트에서는 mock 주입.
* variant title 파싱은 `src.crawlers.tune._parse_variant_title` 를 그대로
  재사용해 파서 중복을 피한다.
* 매칭 가드(콜라보/서브타입)는 `src.core.matching_guards` 공통 모듈 호출.
* 가격 새너티는 수익 consumer 에서 수행 — 어댑터 단계에서는 스킵.
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
from src.crawlers.tune import _parse_variant_title
from src.matcher import extract_model_from_name, normalize_model_number

logger = logging.getLogger(__name__)


BASE_URL = "https://tune.kr"


# 크림 미등재 vendor — 의류/캡/가방/잡화 위주. 라이브 1240 unique sku
# 분포 측정 결과 hit 0% 인 vendor 만 (2026-04-14). matched 손실 0,
# no_model_number/collect_queue 노이즈 제거 목적. 새 운동화 라인 출시 시
# 재평가 필요 (config 수동 갱신).
VENDOR_BLACKLIST: frozenset[str] = frozenset({
    "On",
    "Cayl",
    "Roa Hiking",
    "Montbell",
    "Xlim",
    "Goldwin",
    "Song For The Mute",
    "Clarks Originals",
    "By Parra",
    "BoTT",
    "Khakis",
    "Metalwood",
    "Ostrya",
    "Levi's",
    "Birkenstock",
    "Dr. Martens",
    "Arc'teryx",
    # 2026-04-15 추가 — page1 진단으로 kream 0행 확정 vendor.
    # scan-debugger 보고: NO_HIT 14건 / kream 보유 0건. matched 손실 0.
    "TTT MSW",
    "Nancy",
    "Oakley",
    "Sneeze Magazine",
    "PWA",
    "Tabi Footwear",
})


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
class TuneMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    vendor_blocked: int = 0
    no_model_number: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "vendor_blocked": self.vendor_blocked,
            "no_model_number": self.no_model_number,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
        }


class TuneAdapter:
    """튠 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터.

    덤프 단위는 **variant 1개 = 1 row**. Shopify product 는 여러 variant 를
    가질 수 있고 튠은 variant title 에 ``"<모델번호> / <사이즈>"`` 형식으로
    모델번호·사이즈를 함께 담아 전달한다.
    """

    source_name: str = "tune"

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
            튠 HTTP 레이어. ``fetch_products_page(cursor: str | None, first:
            int) -> tuple[list[dict], str | None]`` 를 제공해야 한다.
            기본값 None → 내부 기본 덤퍼(지연 로드) 사용. 테스트는 mock 주입.
        max_pages:
            최대 페이지 수 (안전 상한).
        page_size:
            페이지당 product 수 (Shopify Storefront 최대 250).
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
        from src.crawlers.tune import tune_crawler

        self._http = _DefaultTuneHttp(tune_crawler)
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프 — GraphQL products(first, after) 커서 페이지네이션
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """튠 Shopify Storefront GraphQL 카탈로그 덤프.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, variant dict 리스트)`. variant dict 구조::

                {
                    "handle": str,
                    "title": str,       # product title
                    "vendor": str,
                    "sku": str,         # variant title 파싱 모델번호
                    "size": str,
                    "price": int,
                    "available": bool,
                }
        """
        http = await self._get_http()
        variants_out: list[dict] = []
        cursor: str | None = None

        for _ in range(self._max_pages):
            try:
                nodes, next_cursor = await http.fetch_products_page(
                    cursor=cursor, first=self._page_size
                )
            except Exception:
                logger.exception("[tune] GraphQL 덤프 실패 cursor=%s", cursor)
                break

            if not nodes:
                break

            for node in nodes:
                handle = (node.get("handle") or "").strip()
                title = node.get("title") or ""
                vendor = node.get("vendor") or ""
                variant_edges = (node.get("variants") or {}).get("edges") or []
                for vedge in variant_edges:
                    vnode = vedge.get("node") or {}
                    vtitle = vnode.get("title") or ""
                    model_no, size = _parse_variant_title(vtitle)
                    price_amount = vnode.get("price") or {}
                    try:
                        price = int(float(price_amount.get("amount") or "0"))
                    except (TypeError, ValueError):
                        price = 0
                    available = bool(vnode.get("availableForSale", False))
                    variants_out.append({
                        "handle": handle,
                        "title": title,
                        "vendor": vendor,
                        "sku": model_no,
                        "size": size,
                        "price": price,
                        "available": available,
                    })

            if not next_cursor:
                break
            cursor = next_cursor

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(variants_out),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[tune] 카탈로그 덤프 완료: %d variants", len(variants_out))
        return event, variants_out

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
            item.get("vendor") or "",
            item.get("title") or "",
            self.source_name,
            _build_url(item.get("handle") or ""),
        )

    async def match_to_kream(
        self, variants: list[dict]
    ) -> tuple[list[CandidateMatched], TuneMatchStats]:
        """덤프된 variant 리스트 → 크림 DB 매칭 → CandidateMatched publish.

        variant title 에서 파싱한 모델번호를 크림 DB 인덱스에 조회한다.
        매칭 가드 통과 시 이벤트 발행, 미등재 시 `kream_collect_queue` 적재.
        같은 모델번호의 variant 가 여러 개(사이즈별)면 1회만 매칭한다.
        """
        stats = TuneMatchStats(dumped=len(variants))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        seen_keys: set[str] = set()

        for item in variants:
            if not item.get("available", False):
                stats.soldout_dropped += 1
                continue
            if (item.get("vendor") or "").strip() in VENDOR_BLACKLIST:
                stats.vendor_blocked += 1
                continue

            sku = (item.get("sku") or "").strip()
            if not sku:
                stats.no_model_number += 1
                continue

            key = _strip_key(sku)
            if not key:
                stats.no_model_number += 1
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)

            kream_row = kream_index.get(key)
            match_model = sku

            if kream_row is None:
                # product title 에서 실제 모델번호 추출 fallback
                # (튠 NB 계열 — variant SKU 는 내부 코드(`NBPFEB737B`), 실제 모델
                # 번호는 product title `U1906LAI` 에 노출) — 카시나와 동일 패턴.
                product_title = item.get("title") or ""
                name_model = extract_model_from_name(product_title) or ""
                if name_model:
                    name_key = _strip_key(name_model)
                    if name_key and name_key != key and name_key not in seen_keys:
                        alt = kream_index.get(name_key)
                        if alt is not None:
                            kream_row = alt
                            match_model = name_model
                            seen_keys.add(name_key)

            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, sku))
                continue

            # 매칭 가드
            source_name_text = item.get("title") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[tune] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[tune] 서브타입 가드 차단: source=%r extra=%s",
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
                    "[tune] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(match_model),
                retail_price=price,
                size=item.get("size") or "",
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
                    "[tune] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[tune] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, variants = await self.dump_catalog()
        _, stats = await self.match_to_kream(variants)
        return stats.as_dict()


class _DefaultTuneHttp:
    """기본 덤퍼 — 기존 `tune_crawler` 의 httpx 클라이언트를 재사용해
    Shopify Storefront GraphQL ``products(first, after)`` 커서 페이지네이션을
    수행하는 얇은 래퍼.

    테스트는 이 클래스 대신 `fetch_products_page` 를 제공하는 mock 을 주입.
    """

    _CATALOG_QUERY = """\
{
  products(first: %d%s) {
    edges {
      cursor
      node {
        id
        title
        handle
        vendor
        variants(first: 50) {
          edges {
            node {
              title
              price { amount currencyCode }
              availableForSale
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""

    def __init__(self, crawler: Any) -> None:
        self._crawler = crawler

    async def fetch_products_page(
        self, cursor: str | None, first: int = 250
    ) -> tuple[list[dict], str | None]:
        from src.crawlers.tune import STOREFRONT_URL

        client = await self._crawler._get_client()
        after = f', after: "{cursor}"' if cursor else ""
        query = self._CATALOG_QUERY % (first, after)
        resp = await client.get(STOREFRONT_URL, params={"query": query})
        if resp.status_code != 200:
            logger.warning("[tune] GraphQL HTTP %d", resp.status_code)
            return [], None
        try:
            data = resp.json()
        except ValueError:
            return [], None
        products = (data.get("data") or {}).get("products") or {}
        edges = products.get("edges") or []
        nodes = [e.get("node") for e in edges if e and e.get("node")]
        page_info = products.get("pageInfo") or {}
        next_cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
        return nodes, next_cursor


__all__ = [
    "TuneAdapter",
    "TuneMatchStats",
    "BASE_URL",
]
