"""푸마 푸시 어댑터 (Phase 3 배치 6).

푸마 한국 공식몰(kr.puma.com, SFCC/SFRA) 카테고리를 `Search-UpdateGrid`
페이지네이션으로 통째 덤프한 뒤, GA4 ecommerce 페이로드의 `style_number`
(=크림 모델번호 `6자리_2자리` → `6자리-2자리` 정규화)를 직접 크림 DB 와
매칭한다.

설계 포인트
------------
* 단일 브랜드 어댑터 — 브랜드 판정 불필요.
* Puma 스타일 번호 = 크림 모델번호. 소싱 포맷(`403767_03`) 과 크림 포맷
  (`403767-03`) 만 맞춰주면 정확 매칭. 상품명 파싱 불필요.
* 콜라보(로제 등) 존재 → `matching_guards` 공통 모듈 재사용.
* 그리드 응답엔 사이즈별 재고가 없다. `size=""` 로 발행하고 수익
  consumer 가 상세 단계에서 보강.
* 어댑터는 producer 전용. 오케스트레이터 직접 참조 금지.
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

BASE_URL = "https://kr.puma.com"
SEARCH_SHOW_PATH = "/on/demandware.store/Sites-KR-Site/ko_KR/Search-Show"

# Puma 모델번호 정규식 (소싱 `_` / 크림 `-` 둘 다 허용, 내부는 하이픈 통일)
PUMA_STYLE_RE = re.compile(r"^\d{6}-\d{2}$")

# 푸마 카탈로그 덤프 기본 카테고리. kr.puma.com PLP 에서 검증됨.
# 신발 > 상의/아우터 > 하의 > 스포츠 의류 순으로 크림 커버리지 비중이 높다.
DEFAULT_CATEGORIES: dict[str, str] = {
    "mens-shoes": "남성 신발",
    "womens-shoes": "여성 신발",
    "kids-shoes": "키즈 신발",
    "mens-apparel": "남성 의류",
    "womens-apparel": "여성 의류",
    "mens-accessories": "남성 액세서리",
    "womens-accessories": "여성 액세서리",
}

DEFAULT_PAGE_SIZE = 48


def _strip_key(model_number: str) -> str:
    """크림 DB 인덱스 키 — `-`·공백 제거. normalize 후 호출."""
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _to_puma_model(raw: str) -> str:
    """소싱 응답 포맷(`403767_03` / `403767-03`) → 크림 DB 포맷(`403767-03`).

    실패 시 빈 문자열. `normalize_model_number` 를 통과하면 `_` 가 그대로
    남기 때문에, 어댑터에서 먼저 `_` 를 `-` 로 치환한 뒤 정규화·검증한다.
    """
    if not raw:
        return ""
    unified = raw.strip().replace("_", "-")
    normalized = normalize_model_number(unified)
    if not PUMA_STYLE_RE.match(normalized):
        return ""
    return normalized


def _keyword_set(text: str) -> set[str]:
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(model_no: str) -> str:
    """PDP 직링크가 없으므로 모델번호 검색 URL 로 대체."""
    if not model_no:
        return BASE_URL
    return f"{BASE_URL}{SEARCH_SHOW_PATH}?q={model_no}"


@dataclass
class PumaMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    invalid_style: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "invalid_style": self.invalid_style,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
        }


class PumaAdapter:
    """푸마 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터.

    덤프 단위는 PLP row 1개. 카테고리 여러 개를 순회하고, 각 카테고리
    안에서 `start` 오프셋을 0, sz, 2*sz, ... 로 증가시키며 빈 응답이
    나올 때까지 페이지네이션.
    """

    source_name: str = "puma"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: dict[str, str] | None = None,
        max_pages_per_category: int = 40,
        page_size: int = DEFAULT_PAGE_SIZE,
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
            푸마 HTTP 레이어. `fetch_category_grid(cgid, start, sz)
            -> list[dict]` 를 제공해야 한다. 기본 None → 내부 기본 크롤러
            사용. 테스트에서는 mock 주입.
        categories:
            덤프할 카테고리 {cgid: 라벨}. 기본 `DEFAULT_CATEGORIES`.
        max_pages_per_category:
            카테고리별 최대 페이지 수 (안전 상한).
        page_size:
            페이지당 상품 수. 기본 48.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories or DEFAULT_CATEGORIES
        self._max_pages = max_pages_per_category
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.puma import puma_crawler

        self._http = puma_crawler
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """카테고리별 Search-UpdateGrid 페이지네이션 덤프."""
        http = await self._get_http()
        products: list[dict] = []
        # product_id 기준 dedup (같은 상품이 여러 카테고리에 노출).
        seen_ids: set[str] = set()

        for cgid, label in self._categories.items():
            for page_idx in range(self._max_pages):
                start = page_idx * self._page_size
                try:
                    items = await http.fetch_category_grid(
                        cgid, start=start, sz=self._page_size
                    )
                except Exception:
                    logger.exception(
                        "[puma] 카테고리 덤프 실패: cgid=%s start=%d", cgid, start
                    )
                    break

                if not items:
                    break

                added_this_page = 0
                for item in items:
                    item["_cgid"] = cgid
                    item["_category_label"] = label
                    pid = str(item.get("product_id") or "")
                    if pid and pid in seen_ids:
                        continue
                    if pid:
                        seen_ids.add(pid)
                    products.append(item)
                    added_this_page += 1

                # 응답이 페이지 크기보다 작으면 마지막 페이지
                if len(items) < self._page_size:
                    break
                # 새로 추가된 게 하나도 없으면 루프 방지
                if added_this_page == 0:
                    break

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[puma] 카탈로그 덤프 완료: %d건", len(products))
        return event, products

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        """Puma 한정으로 크림 DB 를 stripped key 로 인덱스.

        브랜드 필터 `brand='Puma'` 추가 — 동일 번호가 다른 브랜드와 충돌할
        가능성은 낮지만(`403767-03` 패턴은 Puma 고유), 안전상 격리.
        """
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number "
                "FROM kream_products "
                "WHERE brand = 'Puma' AND model_number != ''"
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
            brand_hint="Puma",
            name_hint=item.get("name") or "",
            source=self.source_name,
            source_url=_build_url(model_no),
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], PumaMatchStats]:
        """덤프된 상품 리스트 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = PumaMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []

        # 모델번호 기준 dedup — 같은 style 이 여러 카테고리에 있을 수 있음.
        seen_models: set[str] = set()

        for item in products:
            # 품절 필터
            if item.get("is_sold_out") or not item.get("available", True):
                stats.soldout_dropped += 1
                continue

            raw = str(
                item.get("model_number")
                or item.get("style_number")
                or item.get("item_id")
                or ""
            )
            model_no = _to_puma_model(raw)
            if not model_no:
                stats.invalid_style += 1
                continue

            if model_no in seen_models:
                continue
            seen_models.add(model_no)

            key = _strip_key(model_no)
            if not key:
                stats.invalid_style += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                # 미등재 신상 → collect_queue 적재 후보
                if self._enqueue_collect(item, model_no):
                    stats.collected_to_queue += 1
                continue

            # 매칭 가드
            kream_name = kream_row.get("name") or ""
            source_name_text = item.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[puma] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[puma] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("price") or 0)
            url = item.get("url") or _build_url(model_no)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[puma] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=model_no,
                retail_price=price,
                size="",  # 그리드 경로엔 사이즈별 재고 없음 — consumer 가 보강
                url=url,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[puma] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = [
    "DEFAULT_CATEGORIES",
    "PUMA_STYLE_RE",
    "PumaAdapter",
    "PumaMatchStats",
]
