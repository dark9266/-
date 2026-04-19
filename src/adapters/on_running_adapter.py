"""On Running 푸시 어댑터.

사이트맵 덤프 + PDP JSON-LD 파싱 + 크림 DB 매칭 + CandidateMatched publish.

설계 원칙
--------
* 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
* HTTP 레이어는 외부 주입(`_fetch_sitemap` / `_fetch_pdp` 두 메서드 제공).
  테스트에서는 mock 주입, 실서버 검증은 `_DefaultOnRunningHttp`.
* 매칭: `matcher.py` 의 `normalize_model_number` + 스트립 키. Fuzzy 금지.
* 구형 SKU 는 `.` → `-` 변환 후 매칭 시도 (`on_running.normalize_sku_for_kream`).
* 사이즈별 재고 정보는 SSR 로 내려오지 않아 `available_sizes=()` 발행 —
  기존 v3 runtime 의 listing-only 하위 호환 경로로 처리됨.
* 크림 실호출 금지 — 로컬 SQLite `kream_products` 만 조회.
* POST/PUT/DELETE 금지 (읽기 전용 원칙).
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
from src.crawlers.on_running import (
    OnVariant,
    extract_color_slug_from_url,
    is_valid_sku,
    normalize_sku_for_kream,
)
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


def _strip_key(model_number: str) -> str:
    """어댑터 19곳 공통 스트립 키."""
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


@dataclass
class OnRunningMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    sitemap_urls: int = 0
    pdp_failed: int = 0
    soldout_dropped: int = 0
    no_model_number: int = 0
    invalid_sku: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "sitemap_urls": self.sitemap_urls,
            "pdp_failed": self.pdp_failed,
            "soldout_dropped": self.soldout_dropped,
            "no_model_number": self.no_model_number,
            "invalid_sku": self.invalid_sku,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
        }


class OnRunningAdapter:
    """On Running 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터.

    덤프 단위는 **variant 1건 = 1 row**. On 의 PDP 하나는 ProductGroup 이며
    `hasVariant[]` 가 색상별 개별 SKU 를 쏟아낸다. 각 variant 가 독립된
    크림 상품과 매칭된다(같은 모델, 색상만 다른 경우).
    """

    source_name: str = "on_running"
    brand_hint: str = "On Running"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        max_products: int | None = None,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로.
        http_client:
            HTTP 레이어. 다음 두 메서드를 제공해야 한다:
              * ``fetch_sitemap() -> list[str]``
              * ``fetch_pdp(url: str) -> dict | None``
            기본값 None → `_DefaultOnRunningHttp`.
        max_products:
            테스트/초기 안정화용 상한. None 이면 사이트맵 전체 순회.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._max_products = max_products

    async def _get_http(self) -> Any:
        if self._http is None:
            self._http = _DefaultOnRunningHttp()
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프 — 사이트맵 → PDP 순회 → variant flat
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """사이트맵 URL 전수 순회 → variant flat list.

        반환 dict 구조 (어댑터 내 표준):
            {
                "url": str,          # PDP URL (offers.url 또는 ProductGroup.url)
                "name": str,         # 상품 라인 이름 (ProductGroup.name)
                "brand": str,        # "On" / "On Running"
                "sku": str,          # variant SKU (원본)
                "color": str,        # 색상명 ("Apollo | Eclipse" 등)
                "color_slug": str,   # URL 에서 추출한 슬러그
                "price": int,        # KRW
                "currency": str,     # "KRW"
                "available": bool,   # InStock 여부
            }
        """
        http = await self._get_http()
        stats_sitemap_urls = 0
        stats_pdp_failed = 0
        out: list[dict] = []

        try:
            urls = await http.fetch_sitemap()
        except Exception:
            logger.exception("[on] 사이트맵 덤프 실패")
            urls = []

        if self._max_products is not None:
            urls = urls[: self._max_products]
        stats_sitemap_urls = len(urls)

        for url in urls:
            # 같은 variant 가 여러 URL(다른 색상 slug) 로 리스팅될 가능성 —
            # PDP 파싱이 ProductGroup 전체 variant 를 한 번에 돌려주므로
            # 같은 ProductGroup URL 이 재순회되지 않도록 seen 체크.
            try:
                pdp = await http.fetch_pdp(url)
            except Exception:
                logger.exception("[on] PDP 호출 예외: %s", url)
                pdp = None
            if pdp is None:
                stats_pdp_failed += 1
                continue
            variants: list[OnVariant] = pdp.get("variants") or []
            line_name = pdp.get("name") or ""
            brand = pdp.get("brand") or "On"
            base_url = pdp.get("url") or url
            for v in variants:
                # offer URL 이 존재하면 그걸 우선 (각 색상별 고유 URL)
                variant_url = v.url or base_url
                out.append({
                    "url": variant_url,
                    "name": f"{line_name} {v.color}".strip(),
                    "line_name": line_name,
                    "brand": brand,
                    "sku": v.sku,
                    "color": v.color,
                    "color_slug": extract_color_slug_from_url(variant_url),
                    "price": v.price,
                    "currency": v.currency,
                    "available": v.in_stock,
                })

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(out),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info(
            "[on] 카탈로그 덤프 완료: sitemap_urls=%d variants=%d pdp_fail=%d",
            stats_sitemap_urls, len(out), stats_pdp_failed,
        )
        # stats_sitemap_urls / pdp_failed 는 match_to_kream 에 넘겨 통계 누적.
        # dump_catalog 단독 호출 시엔 event.product_count 가 1차 지표.
        # (통계 통합은 run_once 에서 처리 — 덤프 실패 수치가 필요하면 쓰레드 상태 공유)
        self._last_sitemap_urls = stats_sitemap_urls
        self._last_pdp_failed = stats_pdp_failed
        return event, out

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        from src.core.kream_index import get_kream_index
        return get_kream_index(self._db_path).get()

    def _build_collect_row(
        self, item: dict, model_no: str,
    ) -> tuple[str, str, str, str, str]:
        """미등재 신상 → batch flush 용 row 튜플."""
        return (
            normalize_model_number(model_no),
            self.brand_hint,
            item.get("name") or "",
            self.source_name,
            item.get("url") or "",
        )

    async def match_to_kream(
        self, variants: list[dict],
    ) -> tuple[list[CandidateMatched], OnRunningMatchStats]:
        """덤프된 variant → 크림 DB 매칭 → CandidateMatched publish.

        매칭 규칙:
        1. SKU 원본 스트립 키 → 크림 DB 조회. 신형 `3MF10074109` 는
           그대로, 구형 `61.97657` 은 `.` → `-` 치환 후 `61-97657`.
        2. 적중 시 콜라보/서브타입 가드 통과 후 CandidateMatched publish.
        3. 미매칭은 collect_queue 로 배치 flush.
        """
        stats = OnRunningMatchStats(
            dumped=len(variants),
            sitemap_urls=getattr(self, "_last_sitemap_urls", 0),
            pdp_failed=getattr(self, "_last_pdp_failed", 0),
        )
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        # 같은 SKU 가 여러 variant row 로 나올 수 있어 dedup — 모델 번호 기준.
        seen_keys: set[str] = set()

        for item in variants:
            if not item.get("available", False):
                stats.soldout_dropped += 1
                continue

            sku_raw = str(item.get("sku") or "").strip().upper()
            if not sku_raw:
                stats.no_model_number += 1
                continue
            if not is_valid_sku(sku_raw):
                stats.invalid_sku += 1
                continue

            # 크림 DB 매칭 키 (구형 `.` → `-`)
            normalized = normalize_sku_for_kream(sku_raw)
            key = _strip_key(normalized)
            if not key:
                stats.no_model_number += 1
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # 덤프 ledger — 전수 기록 (비치명)
            try:
                from src.core.dump_ledger import record_dump_item
                await record_dump_item(
                    self._db_path,
                    source=self.source_name,
                    model_no=normalized,
                    name=item.get("name") or "",
                    url=item.get("url") or "",
                )
            except Exception:
                logger.debug("[on] dump_ledger 실패 (비치명)")

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, normalized))
                continue

            # 매칭 가드
            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[on] 콜라보 가드 차단: kream=%r source=%r",
                    kream_name[:50], source_name_text[:50],
                )
                stats.skipped_guard += 1
                continue
            stype_diff = subtype_mismatch(
                _keyword_set(kream_name), _keyword_set(source_name_text),
            )
            if stype_diff:
                logger.info(
                    "[on] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:50], stype_diff,
                )
                stats.skipped_guard += 1
                continue

            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[on] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            price = int(item.get("price") or 0)

            # available_sizes 는 () — SSR 에 사이즈별 재고가 없음. v3 runtime
            # 핸들러는 이 경우 listing-only 경로 (사이즈 교집합 가드 미적용)
            # 로 크림 sell_now 를 단독 기준으로 수익 계산한다.
            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(normalized),
                retail_price=price,
                size="",
                url=item.get("url") or "",
                available_sizes=(),
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        if pending_collect:
            try:
                inserted = await aenqueue_collect_batch(
                    self._db_path, pending_collect,
                )
                stats.collected_to_queue += inserted
            except Exception:
                logger.warning(
                    "[on] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[on] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, variants = await self.dump_catalog()
        _, stats = await self.match_to_kream(variants)
        return stats.as_dict()


# ----------------------------------------------------------------------
# 기본 HTTP 레이어 — 실호출
# ----------------------------------------------------------------------
class _DefaultOnRunningHttp:
    """실호출 레이어 — `OnRunningCrawler` 싱글톤을 재사용.

    `fetch_sitemap()` / `fetch_pdp(url)` 두 메서드를 그대로 위임한다.
    """

    def __init__(self) -> None:
        from src.crawlers.on_running import on_running_crawler
        self._crawler = on_running_crawler

    async def fetch_sitemap(self) -> list[str]:
        return await self._crawler.fetch_sitemap()

    async def fetch_pdp(self, url: str) -> dict | None:
        return await self._crawler.fetch_pdp(url)


__all__ = [
    "OnRunningAdapter",
    "OnRunningMatchStats",
]
