"""노스페이스 한국 공식몰 푸시 어댑터 (Phase 3 배치 6).

``thenorthfacekorea.co.kr`` 카테고리 리스팅 HTML 을 페이지네이션 덤프해
style code(예: ``NA5AS41B``) 단위로 크림 DB 와 매칭한다. 크림 DB 에는
``The North Face`` 상품 2,240개가 이 style code 형식으로 저장돼 있어
`normalize_model_number` 의 stripped key 매칭으로 곧바로 꽂힌다.

설계 원칙
----------
* producer 전용. ``Orchestrator`` 를 직접 참조하지 않는다.
* HTTP 레이어 외부 주입. 테스트에서는 ``fetch_tiles_category(cat, page)``
  를 제공하는 mock 을 주입. 기본값 None 이면 ``_DefaultTnfHttp`` — 내부
  싱글톤 크롤러 ``thenorthface_crawler`` 재사용.
* 매칭 가드(콜라보/서브타입) 는 ``src.core.matching_guards`` 공통 모듈.
* 크림 실호출 금지. 로컬 SQLite 만 조회·적재.
* POST 미사용 — 전부 GET.
* 사이즈별 재고 정보는 리스팅 단계에 없어 ``size=""`` 로 비운다. 수익
  판정 consumer 가 필요 시 상세에서 보강.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect_batch
from src.adapters._size_helpers import fetch_in_stock_sizes
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


BASE_URL = "https://www.thenorthfacekorea.co.kr"

# 기본 덤프 카테고리 — 크롤러의 DEFAULT_CATEGORIES 와 맞추지만
# 어댑터 쪽에서도 독립적으로 override 가능하도록 상수 보관.
DEFAULT_CATEGORIES: dict[str, str] = {
    "men": "남성",
    "women": "여성",
    "kids": "키즈",
    "equipment": "이큅먼트",
    "whitelabel": "화이트라벨",
    "shoes": "신발",
}

# 매칭 전 모델번호 포맷 가드 — `NA5AS41B` 형태 (영숫자 6~12)
_TNF_MODEL_RE = re.compile(r"^[A-Z0-9]{6,12}$")


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(model_number: str) -> str:
    return f"{BASE_URL}/product/{model_number}"


@dataclass
class TnfMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    no_model_number: int = 0
    invalid_model: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "no_model_number": self.no_model_number,
            "invalid_model": self.invalid_model,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
        }


class TheNorthFaceAdapter:
    """노스페이스 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "thenorthface"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: dict[str, str] | None = None,
        max_pages: int = 20,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. ``CatalogDumped``·``CandidateMatched`` publish.
        db_path:
            크림 DB SQLite 경로. ``kream_products`` / ``kream_collect_queue``
            조회·적재.
        http_client:
            HTTP 레이어. ``fetch_tiles_category(category: str, page: int)
            -> list[dict]`` 제공 필요. dict 는 ``TnfTile.as_dict()`` 스키마.
        categories:
            덤프할 카테고리 ``{path: label}``. 기본 ``DEFAULT_CATEGORIES``.
        max_pages:
            카테고리별 최대 페이지 수.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories or DEFAULT_CATEGORIES
        self._max_pages = max_pages

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 로드
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.thenorthface import thenorthface_crawler

        self._http = _DefaultTnfHttp(thenorthface_crawler)
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프 — 카테고리 × 페이지 순회
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """카테고리별 페이지 순회 덤프. 모델번호 dedup."""
        http = await self._get_http()
        dedup: dict[str, dict] = {}

        for cat_path, cat_label in self._categories.items():
            for page in range(1, self._max_pages + 1):
                try:
                    rows = await http.fetch_tiles_category(cat_path, page)
                except Exception:
                    logger.exception(
                        "[thenorthface] 카테고리 덤프 실패: %s page=%d",
                        cat_path,
                        page,
                    )
                    break
                if not rows:
                    break
                new_rows = 0
                for item in rows:
                    model = (item.get("model_number") or "").strip().upper()
                    if not model or model in dedup:
                        continue
                    row = dict(item)
                    row["model_number"] = model
                    row["_category"] = cat_path
                    row["_category_label"] = cat_label
                    dedup[model] = row
                    new_rows += 1
                if new_rows == 0:
                    break

        products = list(dedup.values())
        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[thenorthface] 카탈로그 덤프 완료: %d건", len(products))
        return event, products

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
            "The North Face",
            item.get("name") or "",
            self.source_name,
            item.get("url") or _build_url(model_no),
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], TnfMatchStats]:
        """덤프된 아이템 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = TnfMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        for item in products:
            # 품절 필터
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            model_no = (item.get("model_number") or "").strip().upper()
            if not model_no:
                stats.no_model_number += 1
                continue
            if not _TNF_MODEL_RE.match(model_no):
                stats.invalid_model += 1
                continue

            # 덤프 ledger — 매칭 전에 전수 기록 (오프라인 분석용)
            try:
                from src.core.dump_ledger import record_dump_item
                await record_dump_item(
                    self._db_path,
                    source=self.source_name,
                    model_no=model_no,
                    name=item.get("name") or "",
                    url=item.get("url") or _build_url(model_no),
                )
            except Exception:
                logger.debug("[thenorthface] dump_ledger 실패 (비치명)")

            key = _strip_key(model_no)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, model_no))
                continue

            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[thenorthface] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[thenorthface] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            price = int(item.get("price") or item.get("original_price") or 0)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[thenorthface] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            crawler = await self._get_http()
            pid = str(item.get("product_id") or model_no or "")
            available_sizes = await fetch_in_stock_sizes(
                crawler, pid, source_tag="thenorthface"
            )
            if not available_sizes:
                logger.info(
                    "[thenorthface] PDP 재고 없음 drop: model=%s", model_no
                )
                stats.soldout_dropped += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_no),
                retail_price=price,
                size="",
                url=item.get("url") or _build_url(model_no),
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
                    "[thenorthface] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[thenorthface] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


class _DefaultTnfHttp:
    """기본 HTTP 레이어 — 싱글톤 ``thenorthface_crawler`` 재사용.

    테스트에서는 이 클래스 대신 ``fetch_tiles_category`` 를 제공하는 mock 을
    주입한다.
    """

    def __init__(self, crawler: Any) -> None:
        self._crawler = crawler

    async def fetch_tiles_category(self, category: str, page: int) -> list[dict]:
        tiles = await self._crawler.fetch_category_page(category, page=page)
        return [t.as_dict() for t in tiles]

    async def get_product_detail(self, product_id: str):
        return await self._crawler.get_product_detail(product_id)


__all__ = [
    "DEFAULT_CATEGORIES",
    "TheNorthFaceAdapter",
    "TnfMatchStats",
]
