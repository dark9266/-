"""abcmart (a-rt.com) 푸시 어댑터 (Phase 3 배치 3).

그랜드스테이지(channel=10002) + 온더스팟(channel=10003) 을 단일 어댑터가
순차 덤프하여 `source_name="abcmart"` 로 통합 발행한다.

설계 포인트
------------
* 2개 채널(grandstage, onthespot) 을 순차 덤프 후 합쳐서 `CatalogDumped`
  이벤트 **1건** (product_count = 합산) 을 publish.
* 각 상품 dict 에 `_channel` 메타를 유지 → 매칭 단계에서 url 구성 시 채널별
  base_url 선택에 사용.
* 모델번호는 검색 API 의 `STYLE_INFO` + `COLOR_ID` 조합 → 크림 포맷
  (`IB7746-001`) 으로 정규화. color 가 비어 있으면 style 만 사용한다.
* 어댑터는 producer 전용. HTTP 레이어는 테스트에서 mock 주입.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
* 매칭 가드는 `src.core.matching_guards` 공통 모듈 재사용.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect
from src.adapters._size_helpers import fetch_in_stock_sizes
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


# ─── 채널 정의 ─────────────────────────────────────────────
# 크롤러 `src/crawlers/abcmart.py` 의 `GRANDSTAGE_CONFIG` / `ONTHESPOT_CONFIG`
# 와 동기화 — 변경 시 양쪽 업데이트.
GRANDSTAGE = "grandstage"
ONTHESPOT = "onthespot"

CHANNEL_BASE_URL: dict[str, str] = {
    GRANDSTAGE: "https://grandstage.a-rt.com",
    ONTHESPOT: "https://www.onthespot.co.kr",
}

CHANNEL_LABEL: dict[str, str] = {
    GRANDSTAGE: "그랜드스테이지",
    ONTHESPOT: "온더스팟",
}

# 기본 덤프 채널 — 2개 모두
DEFAULT_CHANNELS: tuple[str, ...] = (GRANDSTAGE, ONTHESPOT)

# 카탈로그 listing 에 사용할 브랜드 키워드 셋. 케찹/크림 매칭 풀 기준 메이저
# 운동화/스포츠 브랜드 위주. 키워드는 검색 API 에 그대로 전달되어 PRDT_NO
# 단위로 dedupe 되므로 중복 안전.
DEFAULT_BRAND_KEYWORDS: tuple[str, ...] = (
    "nike",
    "jordan",
    "adidas",
    "new balance",
    "puma",
    "asics",
    "salomon",
    "hoka",
    "vans",
    "converse",
    "reebok",
    "on running",
)


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_model(style_info: str, color_id: str) -> str:
    """STYLE_INFO + COLOR_ID → 크림 포맷 모델번호.

    crawler `_build_model_number` 와 동일 규칙. color 가 3자리 숫자면
    하이픈으로 결합, 아니면 style 단독.
    """
    if not style_info:
        return ""
    if color_id and re.match(r"^\d{3}$", color_id):
        return f"{style_info}-{color_id}"
    return style_info


def _build_url(channel: str, prdt_no: Any) -> str:
    base = CHANNEL_BASE_URL.get(channel, "")
    if not base or prdt_no is None:
        return ""
    return f"{base}/product/new?prdtNo={prdt_no}"


@dataclass
class AbcmartMatchStats:
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


class AbcmartAdapter:
    """abcmart (그랜드스테이지 + 온더스팟) 카탈로그 덤프 + 크림 DB 매칭 어댑터.

    두 채널을 순차 덤프해 `source_name="abcmart"` 로 통합한다.
    """

    source_name: str = "abcmart"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        channels: tuple[str, ...] | list[str] | None = None,
        max_pages: int = 6,
        page_size: int = 200,
        brand_keywords: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로.
        http_client:
            abcmart HTTP 레이어. 다음 시그니처의 `fetch_channel_listing`
            메서드를 제공해야 한다::

                async def fetch_channel_listing(
                    channel: str, *, max_pages: int, page_size: int
                ) -> list[dict]

            반환 dict 는 crawler 검색 응답의 원본 필드
            (`PRDT_NO`·`PRDT_NAME`·`STYLE_INFO`·`COLOR_ID`·
            `PRDT_DC_PRICE`·`NRMAL_AMT`·`BRAND_NAME`·`SOLD_OUT`) 를 유지.
            테스트에서는 mock 주입. 기본 None → 미구현 → 빈 덤프.
        channels:
            덤프할 채널 키 튜플. 기본 `DEFAULT_CHANNELS` (2개 전부).
        max_pages:
            채널별 최대 페이지 수.
        page_size:
            페이지당 아이템 수.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._channels = tuple(channels or DEFAULT_CHANNELS)
        self._max_pages = max_pages
        self._page_size = page_size
        self._brand_keywords = tuple(brand_keywords or DEFAULT_BRAND_KEYWORDS)

    # ------------------------------------------------------------------
    # HTTP 레이어
    # ------------------------------------------------------------------
    def _get_http(self) -> Any | None:
        if self._http is not None:
            return self._http
        # 지연 import — 테스트 격리. ArtCrawler 싱글톤 두 개를 채널 라우팅
        # 래퍼로 묶어 어댑터가 기대하는 fetch_channel_listing 시그니처 제공.
        from src.crawlers.abcmart import grandstage_crawler, onthespot_crawler

        brand_keywords = self._brand_keywords

        class _ArtListingHttp:
            def __init__(self) -> None:
                self._routes = {
                    GRANDSTAGE: grandstage_crawler,
                    ONTHESPOT: onthespot_crawler,
                }

            async def fetch_channel_listing(
                self,
                channel: str,
                *,
                max_pages: int,
                page_size: int,
            ) -> list[dict]:
                crawler = self._routes.get(channel)
                if crawler is None:
                    return []
                return await crawler.fetch_listing(
                    brand_keywords=list(brand_keywords),
                    max_pages=max_pages,
                    page_size=page_size,
                )

        self._http = _ArtListingHttp()
        return self._http

    def _get_channel_http(self, channel: str) -> Any | None:
        """채널별 PDP 조회용 crawler 반환. 테스트 주입 시 self._http 그대로."""
        if self._http is not None and not hasattr(self._http, "_routes"):
            # 테스트 주입 fake — get_product_detail 자체 보유 가정
            return self._http
        if self._http is None:
            self._get_http()
        routes = getattr(self._http, "_routes", None)
        if routes is None:
            return self._http
        return routes.get(channel)

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프 — 2채널 통합
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """그랜드스테이지 + 온더스팟 순차 덤프 → 통합 이벤트 1건 publish.

        Returns
        -------
        tuple
            `(CatalogDumped, 통합 상품 dict 리스트)`.
            각 dict 에 `_channel` (grandstage/onthespot) 메타가 추가된다.
        """
        http = self._get_http()
        products: list[dict] = []

        if http is None:
            logger.warning("[abcmart] http_client 미주입 — 빈 덤프")
        else:
            for channel in self._channels:
                try:
                    items = await http.fetch_channel_listing(
                        channel,
                        max_pages=self._max_pages,
                        page_size=self._page_size,
                    )
                except Exception:
                    logger.exception(
                        "[abcmart] 채널 덤프 실패: %s(%s)",
                        channel, CHANNEL_LABEL.get(channel, ""),
                    )
                    continue
                for item in items or []:
                    item["_channel"] = channel
                    item["_channel_label"] = CHANNEL_LABEL.get(channel, "")
                    products.append(item)

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info(
            "[abcmart] 카탈로그 덤프 완료: 총 %d건 (채널 %s)",
            len(products), ",".join(self._channels),
        )
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

    async def _enqueue_collect(self, item: dict, model_no: str, channel: str) -> bool:
        """미등재 신상 → kream_collect_queue INSERT OR IGNORE."""
        prdt_no = item.get("PRDT_NO")
        return await aenqueue_collect(
            self._db_path,
            model_number=normalize_model_number(model_no),
            brand_hint=item.get("BRAND_NAME") or "",
            name_hint=item.get("PRDT_NAME") or "",
            source=self.source_name,
            source_url=_build_url(channel, prdt_no),
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], AbcmartMatchStats]:
        """덤프된 상품 리스트 → 크림 DB 매칭 → CandidateMatched publish.

        모델번호는 STYLE_INFO + COLOR_ID 에서 조합한다 (상품명 파싱 불필요).
        url 은 상품이 속한 채널의 base_url 로 구성하여 채널 구분을 유지한다.
        """
        stats = AbcmartMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []

        for item in products:
            if str(item.get("SOLD_OUT", "")).lower() == "y":
                stats.soldout_dropped += 1
                continue

            style = (item.get("STYLE_INFO") or "").strip()
            color = (item.get("COLOR_ID") or "").strip()
            model = _build_model(style, color)
            if not model:
                stats.no_model_number += 1
                continue

            key = _strip_key(model)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            channel = item.get("_channel") or ""
            if kream_row is None:
                if await self._enqueue_collect(item, model, channel):
                    stats.collected_to_queue += 1
                continue

            # 매칭 가드
            source_name_text = item.get("PRDT_NAME") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[abcmart] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[abcmart] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 생성
            sell_price = int(item.get("PRDT_DC_PRICE") or 0)
            normal_price = int(item.get("NRMAL_AMT") or 0)
            price = sell_price or normal_price
            prdt_no = item.get("PRDT_NO")
            url = _build_url(channel, prdt_no)
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[abcmart] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            # PDP 실재고 사이즈 — 빈 결과 무조건 drop
            channel_http = self._get_channel_http(channel)
            available_sizes = await fetch_in_stock_sizes(
                channel_http, str(prdt_no or ""), source_tag=f"abcmart:{channel}"
            )
            if not available_sizes:
                logger.info(
                    "[abcmart] PDP 재고 없음 drop: prdt_no=%s model=%s channel=%s",
                    prdt_no, model, channel,
                )
                stats.soldout_dropped += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model),
                retail_price=price,
                size="",  # 리스팅 단계엔 사이즈 없음 — 수익 consumer 가 보강
                url=url,
                available_sizes=available_sizes,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[abcmart] 매칭 완료: %s", stats.as_dict())
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
    "AbcmartAdapter",
    "AbcmartMatchStats",
    "DEFAULT_CHANNELS",
    "GRANDSTAGE",
    "ONTHESPOT",
]
