"""아크테릭스 코리아 푸시 어댑터 (Phase 3 배치 2).

무신사/카시나 어댑터와 동일한 푸시 파이프라인을 `api.arcteryx.co.kr`
Laravel REST API 에 적용한다. 카테고리 리스팅으로 상품 후보를 덤프한 뒤
옵션 API 를 통해 모델번호(Colour 레벨 `code`) 를 보강해 크림 DB 와 매칭한다.

설계 원칙
----------
* 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
* HTTP 레이어는 외부 주입. 기본값은 `_DefaultArcteryxHttp` 이지만
  `src/crawlers/arcteryx.py` 는 검색/상세 전용이고 카테고리 리스팅
  엔드포인트가 구현돼 있지 않아, 본 어댑터 내부에 최소 호출 계층을
  둔다. `arcteryx.py` 자체는 **수정 금지**.
* 테스트에서는 `_list_raw` / `_options_raw` 두 메서드를 제공하는
  오브젝트를 주입해 mock. 실호출 금지.
* 매칭 가드(콜라보/서브타입) 는 `src.core.matching_guards` 재사용.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import httpx

from src.adapters._collect_queue import enqueue_collect
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number
from src.utils.rate_limiter import AsyncRateLimiter

logger = logging.getLogger(__name__)


API_BASE = "https://api.arcteryx.co.kr"
WEB_BASE = "https://arcteryx.co.kr"

# api.arcteryx.co.kr Laravel REST API 용 헤더 (브라우저 모방).
_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": f"{WEB_BASE}/",
    "Origin": WEB_BASE,
}

# 기본 덤프 카테고리 — `category_id` 정수 ID 기반 (2026-04-13 실호출 확인).
# 응답 `paginate.total` 기준 건수: "1"=623(최대), "97"=243, "50"=76, "100"=25.
# 카테고리 이름은 각 row 의 `category_name` 필드로 동적 확인되므로 값은
# 로깅용 placeholder. 안정화 후 카테고리 트리 전수 탐색으로 확장 예정.
DEFAULT_CATEGORIES: dict[str, str] = {
    "1": "카테고리 1",
    "97": "카테고리 97",
    "50": "카테고리 50",
    "100": "카테고리 100",
}


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _build_url(product_id: int | str) -> str:
    return f"{WEB_BASE}/products/{product_id}"


def _extract_model_from_options(data: dict) -> str:
    """옵션 API 응답 → 모델번호(Colour level 의 `code`).

    `src/crawlers/arcteryx.py::_parse_options` 와 동일한 로직 중
    `model_number` 추출 부분만 단순화한 형태. 사이즈는 쓰지 않는다.
    """
    options = data.get("options") or []
    for option in options:
        if option.get("level") == 1:
            code = option.get("code") or ""
            if code:
                return str(code)
            values = option.get("values") or []
            if values and isinstance(values[0], dict):
                return str(values[0].get("value") or "")
    return ""


@dataclass
class ArcteryxMatchStats:
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


class ArcteryxAdapter:
    """아크테릭스 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "arcteryx"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: dict[str, str] | None = None,
        max_pages: int = 10,
        page_size: int = 60,
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
            아크테릭스 HTTP 레이어. 테스트에서는 `_list_raw` /
            `_options_raw` 를 제공하는 mock 을 주입. 기본값 None 이면
            `_DefaultArcteryxHttp` 를 쓰지만 본 어댑터는 **실호출을
            기대하지 않는다** (실호출은 Phase 3 안정화 후 별도 전환).
        categories:
            덤프할 카테고리 {code: name}. 기본 `DEFAULT_CATEGORIES`.
        max_pages:
            카테고리별 최대 페이지 수.
        page_size:
            페이지당 아이템 수.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories or DEFAULT_CATEGORIES
        self._max_pages = max_pages
        self._page_size = page_size

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 import / 생성
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        self._http = _DefaultArcteryxHttp()
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """아크테릭스 카테고리별 카탈로그 페이지네이션 덤프.

        덤프 단계에서는 리스팅 결과만 보관한다. 모델번호 보강은
        `match_to_kream` 단계에서 `_options_raw` 호출로 수행 —
        전체 옵션 호출을 매번 돌리지 않도록 매칭 시점에 lazy 보강.
        """
        http = await self._get_http()
        products: list[dict] = []

        for category, display_name in self._categories.items():
            page = 1
            seen_ids: set[Any] = set()
            while page <= self._max_pages:
                try:
                    data = await http._list_raw(
                        category=category,
                        page_size=self._page_size,
                        page_number=page,
                    )
                except Exception:
                    logger.exception(
                        "[arcteryx] 카테고리 덤프 실패: %s page=%d",
                        category,
                        page,
                    )
                    break

                rows = (data or {}).get("rows") or []
                if not rows:
                    break
                for item in rows:
                    pid = item.get("product_id")
                    if pid is None or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    item["_category"] = category
                    item["_category_name"] = display_name
                    products.append(item)

                total = int((data or {}).get("total") or 0)
                if total and len(seen_ids) >= total:
                    break
                page += 1

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[arcteryx] 카탈로그 덤프 완료: %d건", len(products))
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

    def _enqueue_collect(self, item: dict, model_no: str) -> bool:
        """미등재 신상 → kream_collect_queue INSERT OR IGNORE."""
        pid = item.get("product_id")
        return enqueue_collect(
            self._db_path,
            model_number=normalize_model_number(model_no),
            brand_hint="Arc'teryx",
            name_hint=item.get("product_name") or "",
            source=self.source_name,
            source_url=_build_url(pid) if pid is not None else "",
        )

    async def _resolve_model_number(self, http: Any, item: dict) -> str:
        """아이템에 이미 model_number 가 있으면 사용, 없으면 옵션 API 호출.

        아크테릭스 리스팅 응답에는 모델번호가 없으므로 대부분의 경우
        `_options_raw` 로 보강한다. 테스트 fixture 에서는 `product_code`
        같은 필드를 직접 넣어 옵션 호출을 생략할 수 있다.
        """
        # 사전 필드 (테스트 편의 / 미래 확장)
        for key in ("model_number", "product_code", "style_code"):
            val = item.get(key)
            if val:
                return str(val)

        pid = item.get("product_id")
        if pid is None:
            return ""
        try:
            data = await http._options_raw(product_id=pid)
        except Exception:
            logger.exception("[arcteryx] 옵션 조회 실패: id=%s", pid)
            return ""
        if not isinstance(data, dict):
            return ""
        return _extract_model_from_options(data)

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], ArcteryxMatchStats]:
        """덤프된 아이템 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = ArcteryxMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        http = await self._get_http()

        for item in products:
            # 품절 필터 — Laravel API 는 sale_state != "ON" 이면 품절 취급
            sale_state = (item.get("sale_state") or "").upper()
            if sale_state and sale_state != "ON":
                stats.soldout_dropped += 1
                continue

            model_no = await self._resolve_model_number(http, item)
            if not model_no:
                stats.no_model_number += 1
                continue

            key = _strip_key(model_no)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                # 미등재 신상 → collect_queue 적재 후보
                if self._enqueue_collect(item, model_no):
                    stats.collected_to_queue += 1
                continue

            # 매칭 가드 — 크림 이름 vs 소싱 이름 키워드 비교
            source_name_text = item.get("product_name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[arcteryx] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[arcteryx] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 이벤트 생성
            price = int(item.get("sell_price") or item.get("retail_price") or 0)
            pid = item.get("product_id")
            url = _build_url(pid) if pid is not None else ""
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[arcteryx] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_no),
                retail_price=price,
                size="",  # 리스팅 단계엔 사이즈 정보 없음 — 수익 consumer 가 보강
                url=url,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        logger.info("[arcteryx] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


# ----------------------------------------------------------------------
# 기본 HTTP 레이어 — api.arcteryx.co.kr Laravel REST 실호출
# ----------------------------------------------------------------------
class _DefaultArcteryxHttp:
    """Laravel REST 실호출 레이어.

    * `_list_raw`: `GET /api/products/search?search_type=category&category_id=&page=`
      → `{rows, total}` 로 정규화. 서버는 `per_page=40` 고정이므로 인자는 무시.
    * `_options_raw`: `GET /api/products/{product_id}/options`
      → 어댑터의 `_extract_model_from_options` 가 level=1 `code` 를 읽는다.

    rate: `AsyncRateLimiter(max_concurrent=2, min_interval=2.0)` — 기존
    `src/crawlers/arcteryx.py` 와 동일 보수값. 어댑터 인스턴스별로 1개만
    생성(싱글톤 X) — 런타임 내 어댑터 자체가 단일 인스턴스라 안전.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._limiter = AsyncRateLimiter(max_concurrent=2, min_interval=2.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=_HEADERS,
                timeout=15.0,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    async def _list_raw(
        self,
        *,
        category: str,
        page_size: int = 40,
        page_number: int = 1,
    ) -> dict:
        client = await self._get_client()
        params = {
            "search_type": "category",
            "category_id": str(category),
            "page": str(page_number),
        }
        async with self._limiter.acquire():
            try:
                resp = await client.get(
                    f"{API_BASE}/api/products/search", params=params
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "[arcteryx] list HTTP 오류 category=%s page=%d: %s",
                    category,
                    page_number,
                    exc,
                )
                return {}
        if resp.status_code != 200:
            logger.warning(
                "[arcteryx] list HTTP %d category=%s page=%d",
                resp.status_code,
                category,
                page_number,
            )
            return {}
        try:
            payload = resp.json()
        except ValueError:
            logger.warning(
                "[arcteryx] list JSON 파싱 실패 category=%s page=%d",
                category,
                page_number,
            )
            return {}
        data = payload.get("data") or {}
        return {
            "rows": data.get("rows") or [],
            "total": int(data.get("count") or 0),
        }

    async def _options_raw(self, *, product_id: int | str) -> dict:
        client = await self._get_client()
        async with self._limiter.acquire():
            try:
                resp = await client.get(
                    f"{API_BASE}/api/products/{product_id}/options"
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "[arcteryx] options HTTP 오류 id=%s: %s", product_id, exc
                )
                return {}
        if resp.status_code != 200:
            logger.warning(
                "[arcteryx] options HTTP %d id=%s", resp.status_code, product_id
            )
            return {}
        try:
            payload = resp.json() or {}
        except ValueError:
            return {}
        # 응답이 `{success, data: {...}}` 로 감싸진 경우도 있고 data 직접인 경우도 있음.
        # level=1 code 를 읽는 `_extract_model_from_options` 가 options 키만 본다.
        if isinstance(payload, dict) and "options" in payload:
            return payload
        inner = payload.get("data") if isinstance(payload, dict) else None
        return inner if isinstance(inner, dict) else {}


__all__ = [
    "ArcteryxAdapter",
    "ArcteryxMatchStats",
    "DEFAULT_CATEGORIES",
]
