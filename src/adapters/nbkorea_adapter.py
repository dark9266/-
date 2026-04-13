"""뉴발란스 공식몰 푸시 어댑터 (Phase 3 배치 2).

무신사·나이키 어댑터(`src.adapters.musinsa_adapter`,
`src.adapters.nike_adapter`) 와 동일한 패턴으로 뉴발란스 한국 공식몰
(nbkorea.com) 카탈로그를 덤프하고 크림 DB 와 매칭한다.

설계 원칙:
    - 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
    - HTTP 레이어는 외부 주입(`http_client`). 테스트에서는 mock 주입.
    - `src/crawlers/nbkorea.py` 는 검색/PDP 경로(역방향)라 카탈로그 덤프
      전용 함수가 없어 어댑터 내부에 `_DefaultNbkoreaHttp` 를 둔다
      (`nbkorea.py` 자체는 수정 금지).
    - 매칭 모델번호: NB 공식몰의 `DispStyleName` (예: `U20024VT`, `M2002RXD`)
      이 크림 DB 의 `model_number` 와 동일한 포맷이므로 SSR 매핑에서 확보되는
      `display_name` 을 1순위 키로 사용한다. 상품명에서의 regex 추출은
      fallback.
    - 매칭 가드(콜라보/서브타입) 는 `src.core.matching_guards` 재사용.
    - 크림 실호출 금지 (본 어댑터는 로컬 DB 매칭만 수행).
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
from src.matcher import extract_model_from_name, normalize_model_number

logger = logging.getLogger(__name__)


# ─── 기본 덤프 카테고리 (신발 위주, 크롤러 NB_CATEGORIES 와 동기화) ─────
DEFAULT_CATEGORIES: dict[str, str] = {
    "250110": "남성 신발",
    "250210": "여성 신발",
    "250310": "키즈 신발",
}


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(style_code: str, col_code: str) -> str:
    return (
        "https://www.nbkorea.com/product/productDetail.action"
        f"?styleCode={style_code}&colCode={col_code}"
    )


@dataclass
class NbkoreaMatchStats:
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


# ─── 기본 HTTP 레이어 ──────────────────────────────────────

class _DefaultNbkoreaHttp:
    """뉴발란스 카탈로그 덤프용 최소 HTTP 레이어.

    `src/crawlers/nbkorea.py` 는 검색(역방향) 전용이라 카탈로그 일괄 덤프
    함수가 없어 어댑터 전용으로 분리. 기본 구현은 카테고리 SSR 페이지의
    매핑(`display_name -> [(style_code, col_code)]`) 을 수집한 뒤
    `getOtherColorOptInfo.action` 을 호출해 옵션(사이즈/재고/가격) 을 보강한다.

    본 클래스는 실제 실행 시에만 호출되며 테스트에서는 mock 으로 대체된다.
    """

    def __init__(self) -> None:
        self._crawler: Any = None

    async def _get_crawler(self) -> Any:
        if self._crawler is None:
            # lazy import — 테스트 환경에서 nbkorea 모듈을 강제 로드하지 않도록.
            from src.crawlers.nbkorea import nbkorea_crawler

            self._crawler = nbkorea_crawler
        return self._crawler

    async def fetch_category_catalog(
        self, categories: dict[str, str], max_items_per_category: int = 200
    ) -> list[dict]:
        """카테고리 SSR → 매핑 → opt_info 보강까지 일괄 수행.

        Parameters
        ----------
        categories:
            {cate_code: 한글명}. 이 값은 현재 구현에서는 단순히 통계/로그용이며,
            실 크롤러는 내부 NB_CATEGORIES 전역을 사용한다 (12시간 TTL 캐시).
        max_items_per_category:
            보강 상한(과호출 방지용 가드). 기본 200.
        """
        crawler = await self._get_crawler()
        mapping: dict[str, list[tuple[str, str]]] = await crawler._get_mapping()

        products: list[dict] = []
        processed = 0
        for display_name, pairs in mapping.items():
            if processed >= max_items_per_category * max(len(categories), 1):
                break
            for style_code, col_code in pairs:
                try:
                    data = await crawler._fetch_opt_info(style_code, col_code)
                except Exception:
                    logger.exception(
                        "[nbkorea] opt_info 실패: %s/%s", style_code, col_code
                    )
                    continue
                prod_opt = (data or {}).get("prodOpt") or []
                if not prod_opt:
                    continue
                first = prod_opt[0]
                price = int(first.get("Price") or 0)
                nor_price = int(first.get("NorPrice") or 0) or price
                in_stock = any(int(p.get("Qty") or 0) > 0 for p in prod_opt)
                products.append(
                    {
                        "display_name": display_name
                        or str(first.get("DispStyleName") or ""),
                        "style_code": style_code,
                        "col_code": col_code,
                        "name": str(first.get("DispStyleName") or display_name),
                        "price": price,
                        "original_price": nor_price,
                        "is_sold_out": not in_stock,
                        "url": _build_url(style_code, col_code),
                    }
                )
                processed += 1
        return products


# ─── 어댑터 본체 ───────────────────────────────────────────

class NbkoreaAdapter:
    """뉴발란스 공식몰 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "nbkorea"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: dict[str, str] | None = None,
        max_items_per_category: int = 200,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로. `kream_products` / `kream_collect_queue`.
        http_client:
            뉴발란스 HTTP 레이어. None → `_DefaultNbkoreaHttp` lazy 생성.
            테스트에서는 mock 주입.
        categories:
            덤프할 카테고리 {cate_code: 한글명}. 기본 `DEFAULT_CATEGORIES`.
        max_items_per_category:
            카테고리당 최대 보강 상품 수 (과호출 방지 가드).
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories or DEFAULT_CATEGORIES
        self._max_items_per_category = max_items_per_category

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 초기화
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is None:
            self._http = _DefaultNbkoreaHttp()
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """뉴발란스 카테고리 덤프 + 옵션 보강.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, 상품 dict 리스트)`.
        """
        http = await self._get_http()
        try:
            products = await http.fetch_category_catalog(
                self._categories,
                max_items_per_category=self._max_items_per_category,
            )
        except Exception:
            logger.exception("[nbkorea] 카탈로그 덤프 실패")
            products = []

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[nbkorea] 카탈로그 덤프 완료: %d건", len(products))
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
        return (
            normalize_model_number(model_no),
            "New Balance",
            item.get("name") or item.get("display_name") or "",
            self.source_name,
            item.get("url") or "",
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], NbkoreaMatchStats]:
        """덤프된 상품 리스트 → 크림 DB 매칭 → CandidateMatched publish.

        매칭 우선순위:
            1. `display_name` (NB 공식몰 매핑에서 확보한 크림 포맷 모델번호)
            2. 상품명 regex 추출 (`extract_model_from_name`) — fallback
        """
        stats = NbkoreaMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        for item in products:
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            display = (item.get("display_name") or "").strip()
            if not display:
                # fallback: 상품명에서 추출
                display = extract_model_from_name(item.get("name") or "") or ""
            if not display:
                stats.no_model_number += 1
                continue

            key = _strip_key(display)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, display))
                continue

            # 매칭 가드
            kream_name = kream_row.get("name") or ""
            source_name_text = item.get("name") or display
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[nbkorea] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[nbkorea] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 발행
            price = int(item.get("price") or 0)
            url = item.get("url") or _build_url(
                str(item.get("style_code") or ""),
                str(item.get("col_code") or ""),
            )
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[nbkorea] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(display),
                retail_price=price,
                size="",  # 리스팅 단계엔 단일 사이즈 없음 — 수익 consumer 가 보강
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
                    "[nbkorea] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[nbkorea] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = ["NbkoreaAdapter", "NbkoreaMatchStats", "DEFAULT_CATEGORIES"]
