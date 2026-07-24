"""On Running 푸시 어댑터.

사이트맵 덤프 + PDP JSON-LD 파싱 + 크림 DB 매칭 + CandidateMatched publish.

설계 원칙
--------
* 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
* HTTP 레이어는 외부 주입(`_fetch_sitemap` / `_fetch_pdp` 두 메서드 제공).
  테스트에서는 mock 주입, 실서버 검증은 `_DefaultOnRunningHttp`.
* 매칭: `matcher.py` 의 `normalize_model_number` + 스트립 키. Fuzzy 금지.
* 구형 SKU 는 `.` → `-` 변환 후 매칭 시도 (`on_running.normalize_sku_for_kream`).
* **사이즈 실재고 (1c-4)**: PDP `__NUXT_DATA__` 파싱 결과(`parse_pdp` 의
  `size_stock`) 를 컬러 SKU 로 조회해 `SourceStockSnapshot` 을 구성한다.
  (과거 "SSR 에 사이즈별 재고 없음" 기록은 오판정 — 2026-07-24 정정.)
  전 사이즈 품절 확정만 drop, `__NUXT_DATA__` 파싱 실패/컬러 못 찾음은
  UNKNOWN 보류(runtime 게이트가 검증 대기 처리) — musinsa 1c-3 배선과 동일 패턴.
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
from src.adapters._stock_capability import StockCapability
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.crawlers.on_running import (
    OnVariant,
    SizeStock,
    extract_color_slug_from_url,
    is_valid_sku,
    normalize_sku_for_kream,
)
from src.matcher import normalize_model_number
from src.models.stock import DEFAULT_SOURCE_STOCK_TTL_SEC, SourceStockSnapshot, StockState

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
    unknown_held: int = 0  # 1c-4: 사이즈 재고 조회실패 → verification_pending 보류 발행

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
            "unknown_held": self.unknown_held,
        }


class OnRunningAdapter:
    """On Running 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터.

    덤프 단위는 **variant 1건 = 1 row**. On 의 PDP 하나는 ProductGroup 이며
    `hasVariant[]` 가 색상별 개별 SKU 를 쏟아낸다. 각 variant 가 독립된
    크림 상품과 매칭된다(같은 모델, 색상만 다른 경우).
    """

    source_name: str = "on_running"
    brand_hint: str = "On Running"
    # 1c-4: PDP __NUXT_DATA__ 에 전 색상 × 전 사이즈 실재고가 노출됨 (실측 확정).
    stock_capability: StockCapability = StockCapability.SIZE_STOCK_SUPPORTED

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
            # 1c-5 F5: 사이즈 재고 관측시각 = 이 PDP 를 실제로 fetch 한 시각.
            # match_to_kream 은 전 상품 덤프 완료 후 일괄 실행되므로, 거기서
            # `now()` 를 관측시각으로 쓰면 초기에 fetch 한 PDP 의 오래된
            # 재고가 매칭 시점 기준 30분 재유효로 둔갑한다.
            fetch_observed_at = time.time()
            if pdp is None:
                stats_pdp_failed += 1
                continue
            variants: list[OnVariant] = pdp.get("variants") or []
            line_name = pdp.get("name") or ""
            brand = pdp.get("brand") or "On"
            base_url = pdp.get("url") or url
            # 1c-4: PDP 1회 응답에 이미 담긴 __NUXT_DATA__ 사이즈 재고 — 컬러
            # SKU 별 조회. map 자체가 비어있으면(파싱 실패) 전 variant 가
            # UNKNOWN 보류로, map 은 있는데 특정 sku 만 없으면 그 sku 만
            # UNKNOWN 보류로 구분(match_to_kream 이 처리).
            size_stock_map: dict[str, list[SizeStock]] = pdp.get("size_stock") or {}
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
                    "size_stock": size_stock_map.get(v.sku),
                    "size_stock_map_size": len(size_stock_map),
                    "observed_at": fetch_observed_at,
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

    @staticmethod
    def _match_key_for_item(item: dict) -> str | None:
        """item → 크림 매칭 키 (없으면 None). 1c-5r R2 winner 선별 전용 —
        메인 루프의 통계 부수효과 없이 key 만 재계산한다."""
        if not item.get("available", False):
            return None
        sku_raw = str(item.get("sku") or "").strip().upper()
        if not sku_raw or not is_valid_sku(sku_raw):
            return None
        normalized = normalize_sku_for_kream(sku_raw)
        key = _strip_key(normalized)
        return key or None

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

        # 1c-5r R2: 같은 컬러 SKU 가 여러 사이트맵 URL(PDP) 에서 중복 관측될
        # 때, 기존 "첫 관측 선점" 은 덤프가 30분+ 걸리면 신선한 뒤 관측을
        # 버리고 오래된(stale) 관측이 채택돼 정상 재고가 stale_observation
        # 으로 오판정됐다. SKU 별로 observed_at 이 가장 최신인 항목만 승자로
        # 남기고 나머지는 (순서 무관) 스킵한다.
        winning_index_by_key: dict[str, int] = {}
        for idx, item in enumerate(variants):
            key = self._match_key_for_item(item)
            if key is None:
                continue
            observed_at = item.get("observed_at") or 0.0
            cur_idx = winning_index_by_key.get(key)
            if cur_idx is None:
                winning_index_by_key[key] = idx
                continue
            cur_observed_at = variants[cur_idx].get("observed_at") or 0.0
            if observed_at > cur_observed_at:
                winning_index_by_key[key] = idx
        winning_indices = set(winning_index_by_key.values())

        for idx, item in enumerate(variants):
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
            if idx not in winning_indices:
                # 같은 키의 더 신선한 관측이 따로 있음 — 이 항목은 스킵
                # (그 신선한 항목이 이 루프에서 처리된다).
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

            # 1c-4: PDP __NUXT_DATA__ 사이즈 실재고 배선 (musinsa 1c-3 패턴 준용).
            # - 컬러 SKU 파싱 성공 + in-stock 사이즈 ≥1 → IN_STOCK 스냅샷.
            # - 컬러 SKU 파싱 성공했으나 전 사이즈 stock=0 → 품절 확정 drop.
            # - __NUXT_DATA__ 전체 파싱 실패(map 비어있음) → UNKNOWN/parse_fail.
            # - map 은 있는데 이 컬러 SKU 만 못 찾음 → UNKNOWN/color_not_found.
            # 두 UNKNOWN 케이스 모두 drop 아님 — runtime 게이트가 검증 보류.
            # 1c-5 F5: 관측시각 = PDP fetch 시각(dump_catalog 이 기록). 미주입
            # (하위호환) 시에는 match 시각으로 폴백.
            match_time = time.time()
            fetch_time = item.get("observed_at")
            if fetch_time is None:
                fetch_time = match_time
            color_sizes: list[SizeStock] | None = item.get("size_stock")
            source_stock: SourceStockSnapshot | None
            available_sizes: tuple[str, ...]

            if color_sizes:
                in_stock_sizes = tuple(s.size for s in color_sizes if s.stock > 0)
                if not in_stock_sizes:
                    logger.info(
                        "[on] 사이즈 전량 품절 drop: sku=%s model=%s",
                        sku_raw,
                        normalized,
                    )
                    stats.soldout_dropped += 1
                    continue
                expires_at = fetch_time + DEFAULT_SOURCE_STOCK_TTL_SEC
                if expires_at <= match_time:
                    # fetch 시각 기준 이미 만료 — 매칭이 fetch 보다 한참
                    # 늦어 재고가 stale 해졌을 위험. IN_STOCK 승격 대신
                    # UNKNOWN 보류 (거짓 알림 방지).
                    logger.info(
                        "[on] 관측시각 만료 — UNKNOWN 보류: sku=%s model=%s "
                        "observed_at=%.0f",
                        sku_raw,
                        normalized,
                        fetch_time,
                    )
                    stats.unknown_held += 1
                    available_sizes = ()
                    source_stock = SourceStockSnapshot(
                        state=StockState.UNKNOWN,
                        available_sizes=(),
                        observed_at=fetch_time,
                        expires_at=expires_at,
                        evidence_method="on_nuxt_data",
                        reason_code="stale_observation",
                    )
                else:
                    available_sizes = in_stock_sizes
                    source_stock = SourceStockSnapshot(
                        state=StockState.IN_STOCK,
                        available_sizes=in_stock_sizes,
                        observed_at=fetch_time,
                        expires_at=expires_at,
                        evidence_method="on_nuxt_data",
                    )
            else:
                reason = (
                    "color_not_found" if item.get("size_stock_map_size", 0) > 0 else "parse_fail"
                )
                stats.unknown_held += 1
                available_sizes = ()
                source_stock = SourceStockSnapshot(
                    state=StockState.UNKNOWN,
                    available_sizes=(),
                    observed_at=fetch_time,
                    expires_at=fetch_time + DEFAULT_SOURCE_STOCK_TTL_SEC,
                    evidence_method="on_nuxt_data",
                    reason_code=reason,
                )

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(normalized),
                retail_price=price,
                size="",
                url=item.get("url") or "",
                available_sizes=available_sizes,
                source_stock=source_stock,
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
