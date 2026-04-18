"""웍스아웃 푸시 어댑터 (Phase 3 배치 4).

웍스아웃(worksout.co.kr)은 REST API 기반 편집숍이지만 상품 데이터에
**모델번호 필드가 없다**. 기존 v2 역방향 스캐너(`src/reverse_scanner.py`)는
브랜드 + 이름 키워드 교차 + 콜라보/서브타입 가드로 이름 매칭을 수행한다.
본 어댑터는 동일한 정확도 하드 제약 아래 푸시(카탈로그 덤프 → 크림 DB
매칭) 파이프라인에 이식한 것이다.

설계 포인트
-----------
* 어댑터는 producer 전용 — orchestrator/크림 API 실호출 금지.
* HTTP 레이어는 ``fetch_catalog_page(page, size) -> (list[dict], bool)`` 만
  제공하면 되는 얇은 인터페이스로 추상화. 기본값 None 이면 `src.crawlers.
  worksout` 싱글톤의 httpx 클라이언트를 재사용하는 내부 덤퍼(지연 로드)를
  사용한다. 테스트에서는 mock 주입.
* 모델번호 부재 → 매칭은 **이름 기반** 엄격 필터:
    1) 브랜드 일치(한글/영문 alias 포함, Nike↔Jordan 동급)
    2) 상품명 영문 토큰 2개 이상 교차
    3) 콜라보 가드 (`matching_guards.collab_match_fails`)
    4) 서브타입 가드 (`matching_guards.subtype_mismatch`)
  네 단계 모두 통과한 후보만 `CandidateMatched` publish.
  확신 못 서면 drop (`skipped_guard` 증가). 거짓 매칭 0 지향.
* 크림 DB 인덱스는 브랜드별 버킷으로 구성해 O(N×M) → O(N×브랜드내 M) 축소.
* 미등재 신상 큐 적재는 수행하지 않음 — 모델번호가 없어 `kream_collect_
  queue` 의 PK(model_number) 를 만족시킬 수 없다. 웍스아웃은 매칭 후보만
  기여하고, 신상 수집은 모델번호 있는 소싱처(무신사/튠 등) 역할로 맡긴다.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from src.core.db import sync_connect
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch

logger = logging.getLogger(__name__)


WEB_BASE = "https://www.worksout.co.kr"

# 브랜드 alias (한글 → 영문 정규 표현). reverse_scanner 와 동기.
_BRAND_ALIASES: dict[str, str] = {
    "나이키": "nike",
    "아디다스": "adidas",
    "뉴발란스": "new balance",
    "아식스": "asics",
    "푸마": "puma",
    "컨버스": "converse",
    "반스": "vans",
    "리복": "reebok",
    "조던": "jordan",
    "살로몬": "salomon",
    "호카": "hoka",
    "온러닝": "on",
}

# 한글 모델명 → 영문 매핑 (토큰 교차용). reverse_scanner 와 동기.
_KR_TO_EN_TOKENS: dict[str, str] = {
    "덩크": "dunk",
    "에어포스": "force",
    "에어맥스": "max",
    "조던": "jordan",
    "삼바": "samba",
    "가젤": "gazelle",
    "카야노": "kayano",
    "젤": "gel",
    "올드스쿨": "skool",
    "클라우드": "cloud",
    "캠퍼스": "campus",
    "포럼": "forum",
    "슈퍼스타": "superstar",
    "스탠스미스": "smith",
    "척": "chuck",
    "테일러": "taylor",
    "뮬": "mule",
    "슬라이드": "slide",
    "샥스": "shox",
    "줌": "zoom",
    "플라이": "fly",
    "코르테즈": "cortez",
    "블레이저": "blazer",
    "레트로": "retro",
    "오리지널": "og",
    "프리미엄": "premium",
    "로우": "low",
    "하이": "high",
    "미드": "mid",
}

_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "x", "sp", "nike", "adidas", "new", "balance",
})

_MIN_TOKEN_OVERLAP = 2


def _normalize_brand(raw: str) -> str:
    """브랜드 문자열을 소문자 영문 정규형으로."""
    if not raw:
        return ""
    s = raw.strip().lower()
    for kr, en in _BRAND_ALIASES.items():
        if kr in s:
            return en
    return s


def _extract_tokens(text: str) -> set[str]:
    """상품명 → 토큰 집합(영문+숫자, 한글→영문 변환 포함).

    reverse_scanner._verify_name_match 와 동일 규칙.
    """
    if not text:
        return set()
    lower = text.lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9]+", lower))
    for kr, en in _KR_TO_EN_TOKENS.items():
        if kr in lower:
            tokens.add(en)
    return tokens - _STOPWORDS


def _brand_match(kream_brand_en: str, source_brand_en: str) -> bool:
    """브랜드 일치 판정. Nike↔Jordan 은 동급."""
    if not kream_brand_en or not source_brand_en:
        # 한쪽이라도 비어있으면 이름 토큰에 맡김 (과잉 차단 방지).
        return True
    if kream_brand_en in source_brand_en or source_brand_en in kream_brand_en:
        return True
    if {kream_brand_en, source_brand_en} == {"nike", "jordan"}:
        return True
    return False


@dataclass
class WorksoutMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    matched: int = 0
    collected_to_queue: int = 0  # 웍스아웃은 항상 0 — 호환 유지용 슬롯
    skipped_guard: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
        }


class WorksoutAdapter:
    """웍스아웃 카탈로그 덤프 + 이름 기반 크림 DB 매칭 + 이벤트 발행.

    상품 단위 덤프. 같은 상품이 여러 크림 후보와 매칭될 가능성은 이름
    엄격 필터에서 대부분 걸러지지만, 방어적으로 per-source-product 1회만
    publish 한다 (가장 먼저 통과한 크림 후보 채택).
    """

    source_name: str = "worksout"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        crawler: Any = None,
        *,
        max_pages: int = 50,
        page_size: int = 60,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로. `kream_products` 테이블 조회.
        crawler:
            웍스아웃 HTTP 레이어. ``fetch_catalog_page(page: int, size: int)
            -> tuple[list[dict], bool]`` 를 제공하면 된다. None 이면 내부
            기본 덤퍼(지연 로드) 사용. 테스트는 mock 주입.
        max_pages:
            카테고리 덤프 안전 상한 페이지.
        page_size:
            페이지당 상품 수.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = crawler
        self._max_pages = max_pages
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.worksout import worksout_crawler

        self._http = _DefaultWorksoutHttp(worksout_crawler)
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프 — page 반복
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """웍스아웃 카탈로그 페이지네이션 덤프.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, 상품 dict 리스트)`. 상품 dict 구조::

                {
                    "product_id": str,
                    "name": str,
                    "brand": str,
                    "price": int,
                    "url": str,
                    "is_sold_out": bool,
                    "sizes": list[dict],
                }
        """
        http = await self._get_http()
        products: list[dict] = []

        for page in range(self._max_pages):
            try:
                items, has_next = await http.fetch_catalog_page(
                    page=page, size=self._page_size
                )
            except Exception:
                logger.exception("[worksout] 카탈로그 덤프 실패 page=%d", page)
                break

            if items:
                products.extend(items)

            if not has_next:
                break

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[worksout] 카탈로그 덤프 완료: %d건", len(products))
        return event, products

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭 (이름 기반)
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> list[dict]:
        """크림 DB 전체를 dict 리스트로 로드.

        이름 매칭은 해시 조회가 불가능하므로 선형 스캔. 브랜드 버킷으로
        분할해 후보 집합을 축소한다. 블로킹 sqlite3 — 단발 실행이라 허용.
        """
        with sync_connect(self._db_path, read_only=True) as conn:
            rows = conn.execute(
                "SELECT product_id, name, brand FROM kream_products"
            ).fetchall()

        result: list[dict] = []
        for row in rows:
            d = dict(row)
            d["_brand_en"] = _normalize_brand(d.get("brand", ""))
            d["_tokens"] = _extract_tokens(d.get("name", ""))
            if not d["_tokens"]:
                continue
            result.append(d)
        return result

    def _find_kream_match(
        self,
        source_brand_en: str,
        source_tokens: set[str],
        source_name: str,
        kream_bucket: list[dict],
    ) -> dict | None:
        """후보 버킷 선형 스캔 → 이름 엄격 매칭 통과한 첫 크림 row.

        네 단계 가드:
            1) 브랜드 일치
            2) 토큰 교차 ≥ 2
            3) 콜라보 가드
            4) 서브타입 가드
        """
        for krow in kream_bucket:
            if not _brand_match(source_brand_en, krow["_brand_en"]):
                continue

            overlap = source_tokens & krow["_tokens"]
            if len(overlap) < _MIN_TOKEN_OVERLAP:
                continue

            kream_name = krow.get("name") or ""
            if collab_match_fails(kream_name, source_name):
                continue

            stype_diff = subtype_mismatch(krow["_tokens"], source_tokens)
            if stype_diff:
                continue

            return krow

        return None

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], WorksoutMatchStats]:
        """덤프된 상품 리스트 → 이름 기반 크림 DB 매칭 → 이벤트 발행.

        통계:
            - dumped              : 입력 총량
            - soldout_dropped     : 전체 품절 제외
            - matched             : CandidateMatched publish 성공
            - collected_to_queue  : 항상 0 (모델번호 부재로 큐 적재 불가)
            - skipped_guard       : 브랜드/토큰/콜라보/서브타입 가드 차단
        """
        stats = WorksoutMatchStats(dumped=len(products))
        kream_rows = self._load_kream_index()
        matched: list[CandidateMatched] = []
        seen_source_ids: set[str] = set()

        for item in products:
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            source_id = str(item.get("product_id") or "")
            if source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)

            source_name = item.get("name") or ""
            source_brand_en = _normalize_brand(item.get("brand") or "")
            source_tokens = _extract_tokens(source_name)
            if len(source_tokens) < _MIN_TOKEN_OVERLAP:
                # 이름 토큰이 너무 적으면 매칭 신뢰도 확보 불가 → 포기.
                stats.skipped_guard += 1
                continue

            krow = self._find_kream_match(
                source_brand_en=source_brand_en,
                source_tokens=source_tokens,
                source_name=source_name,
                kream_bucket=kream_rows,
            )
            if krow is None:
                stats.skipped_guard += 1
                continue

            try:
                kream_product_id = int(krow["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[worksout] 비정수 kream_product_id 스킵: %r",
                    krow.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            price = int(item.get("price") or 0)
            url = item.get("url") or f"{WEB_BASE}/product/{source_id}"

            # 리스팅 단계에서 이미 sizes (in_stock) 노출 — 직접 추출
            size_rows = item.get("sizes") or []
            available_sizes: tuple[str, ...] = tuple(
                str(s.get("size") or "").strip()
                for s in size_rows
                if isinstance(s, dict)
                and s.get("in_stock", True)
                and (s.get("size") or "").strip()
            )
            if not available_sizes:
                logger.info(
                    "[worksout] 재고 사이즈 없음 drop: source_id=%s",
                    source_id,
                )
                stats.skipped_guard += 1
                continue

            # 이름 매칭이므로 model_no 는 빈 문자열 — 수익 consumer 가
            # kream_product_id 기준으로 시세를 조회한다.
            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no="",
                retail_price=price,
                size="",
                url=url,
                available_sizes=available_sizes,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

            logger.info(
                "[worksout] 이름 매칭: kream=%r ↔ source=%r (brand=%s)",
                (krow.get("name") or "")[:40],
                source_name[:40],
                source_brand_en,
            )

        logger.info("[worksout] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


class _DefaultWorksoutHttp:
    """기본 덤퍼 — worksout 검색 API 는 빈 키워드 카탈로그 리스팅을 지원하지
    않는다(0건 반환). 브랜드 시드 키워드 리스트를 순회해 검색 결과를 누적
    덤프한다. page 인자는 시드 리스트 인덱스로 해석한다.

    테스트는 이 클래스 대신 `fetch_catalog_page` 를 제공하는 mock 주입.
    """

    # 크림 DB 와 브랜드 교차점이 있는 시드. 순서는 임의.
    BRAND_SEEDS: tuple[str, ...] = (
        "nike",
        "adidas",
        "new balance",
        "asics",
        "puma",
        "converse",
        "vans",
        "jordan",
        "salomon",
        "hoka",
        "on",
        "reebok",
    )

    def __init__(self, crawler: Any) -> None:
        self._crawler = crawler

    async def fetch_catalog_page(
        self, page: int, size: int = 60
    ) -> tuple[list[dict], bool]:
        """시드 인덱스 ``page`` 의 브랜드 검색 결과를 반환.

        ``has_next`` 는 다음 시드가 남아있는지 여부. 실제 페이지 단위
        페이지네이션은 수행하지 않고, 시드당 상위 ``size`` 건만 가져온다.
        """
        if page >= len(self.BRAND_SEEDS):
            return [], False
        keyword = self.BRAND_SEEDS[page]
        try:
            items = await self._crawler.search_products(keyword, limit=size)
        except Exception:
            logger.exception("[worksout] 시드 '%s' 검색 실패", keyword)
            items = []
        has_next = page + 1 < len(self.BRAND_SEEDS)
        return items, has_next


__all__ = [
    "WorksoutAdapter",
    "WorksoutMatchStats",
    "WEB_BASE",
]
