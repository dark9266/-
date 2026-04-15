"""나이키 공식몰 푸시 어댑터 (Phase 3 배치 1).

무신사 어댑터(`src.adapters.musinsa_adapter`) 와 동일한 패턴으로
나이키 KR 공식몰 카탈로그를 덤프하고 크림 DB 와 매칭한다.

설계 원칙:
    - 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
    - HTTP 레이어는 외부 주입(`http_client`). 테스트에서는 mock 주입.
    - 기본 HTTP 구현은 `_DefaultNikeHttp` — httpx + __NEXT_DATA__ 파싱.
      `src/crawlers/nike.py` 는 검색/PDP 용이라 카테고리 덤프 함수가 없어
      어댑터 내부에 카탈로그 리스팅 전용 최소 레이어를 둔다
      (`nike.py` 자체는 수정 금지).
    - LAUNCH(드로우) 상품은 반드시 스킵. `productType`/`publishType`/
      `consumerReleaseType` 중 하나라도 "LAUNCH" 면 탈락.
    - 매칭 가드(콜라보/서브타입) 는 `src.core.matching_guards` 재사용.
    - 크림 실호출 금지 (본 어댑터는 로컬 DB 매칭만 수행).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect_batch
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import extract_model_from_name, normalize_model_number

logger = logging.getLogger(__name__)


# ─── 기본 카탈로그 카테고리 (나이키 KR Wall 경로) ────────────
DEFAULT_CATEGORIES: dict[str, str] = {
    "men-shoes": "남성 신발",
    "women-shoes": "여성 신발",
    "jordan": "조던",
}

# LAUNCH 판정 키 — 드로우 전용 상품 스킵
_LAUNCH_KEYS: tuple[str, ...] = (
    "productType",
    "publishType",
    "consumerReleaseType",
    "releaseType",
)


def _slug(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", (s or "").lower())


def _is_launch(item: dict) -> bool:
    """LAUNCH / 드로우 상품 여부."""
    for k in _LAUNCH_KEYS:
        val = item.get(k)
        if isinstance(val, str) and val.upper() == "LAUNCH":
            return True
    status_modifier = item.get("statusModifier")
    if isinstance(status_modifier, str) and status_modifier.upper() in (
        "BUYABLE_LINE",
        "NOT_BUYABLE",
        "HOLD",
        "UNAVAILABLE",
    ):
        return True
    return False


def _strip_key(model_number: str) -> str:
    return re.sub(r"[\s\-]", "", normalize_model_number(model_number))


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


@dataclass
class MatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    launch_dropped: int = 0
    soldout_dropped: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0
    no_model_number: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "launch_dropped": self.launch_dropped,
            "soldout_dropped": self.soldout_dropped,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
            "no_model_number": self.no_model_number,
        }


# ─── 기본 HTTP 레이어 ──────────────────────────────────────

class _DefaultNikeHttp:
    """나이키 카탈로그 Wall 덤프용 최소 HTTP 레이어.

    `src/crawlers/nike.py` 가 검색/PDP 중심이라 카테고리 덤프 함수가 없어
    어댑터 전용으로 분리. httpx 를 lazy import.
    """

    _WALL_URL = "https://www.nike.com/kr/w/{category}"

    def __init__(self) -> None:
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    )
                },
                timeout=15,
                follow_redirects=True,
            )
        return self._client

    async def fetch_category_listing(
        self, category: str, max_pages: int = 1
    ) -> list[dict]:
        """카테고리 Wall HTML → __NEXT_DATA__ → 상품 dict 리스트."""
        client = await self._get_client()
        url = self._WALL_URL.format(category=category)
        try:
            resp = await client.get(url)
        except Exception:
            logger.exception("[nike] 카테고리 요청 실패: %s", category)
            return []
        if getattr(resp, "status_code", 500) != 200:
            logger.warning(
                "[nike] 카테고리 HTTP %s: %s",
                getattr(resp, "status_code", "?"),
                category,
            )
            return []
        return _parse_wall_products(resp.text)

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None


def _extract_next_data(html: str) -> dict:
    m = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", html or "", re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return {}


def _parse_wall_products(html: str) -> list[dict]:
    """__NEXT_DATA__.Wall.productGroupings → 정규화된 상품 dict 리스트."""
    data = _extract_next_data(html)
    wall = (
        data.get("props", {})
        .get("pageProps", {})
        .get("initialState", {})
        .get("Wall", {})
    )
    groupings = wall.get("productGroupings", []) or []
    out: list[dict] = []
    for group in groupings:
        for prod in group.get("products", []) or []:
            code = prod.get("productCode") or prod.get("styleColor") or ""
            if not code:
                continue
            copy = prod.get("copy", {}) or {}
            prices = prod.get("prices", {}) or {}
            pdp = prod.get("pdpUrl", {}) or {}
            title = copy.get("title") or prod.get("title") or ""
            sub_title = copy.get("subTitle") or ""
            name = f"{title} {sub_title}".strip() or title
            url = pdp.get("url") if isinstance(pdp, dict) else ""
            if not url:
                url = f"https://www.nike.com/kr/t/_/{code}"
            out.append(
                {
                    "productCode": code,
                    "name": name,
                    "brand": "Nike",
                    "price": int(prices.get("currentPrice") or 0),
                    "original_price": int(prices.get("initialPrice") or 0),
                    "url": url,
                    "isSoldOut": bool(prod.get("isSoldOut")),
                    "productType": prod.get("productType", ""),
                    "publishType": prod.get("publishType", ""),
                    "consumerReleaseType": prod.get("consumerReleaseType", ""),
                    "statusModifier": prod.get("statusModifier", ""),
                }
            )
    return out


# ─── 어댑터 본체 ───────────────────────────────────────────

class NikeAdapter:
    """나이키 공식몰 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "nike"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: dict[str, str] | None = None,
        max_pages: int = 3,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로. `kream_products` / `kream_collect_queue`.
        http_client:
            나이키 HTTP 레이어. None → `_DefaultNikeHttp` lazy 생성.
            테스트에서는 mock 주입.
        categories:
            덤프할 카테고리 {slug: 한글명}. 기본 `DEFAULT_CATEGORIES`.
        max_pages:
            카테고리별 최대 페이지 수 (기본 HTTP 레이어에서는 현재 1페이지
            고정이나, 외부 주입 HTTP 가 페이지네이션을 지원할 수 있도록 전달).
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories or DEFAULT_CATEGORIES
        self._max_pages = max_pages

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 초기화
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is None:
            self._http = _DefaultNikeHttp()
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """나이키 카탈로그 카테고리 덤프. LAUNCH 상품은 이 단계에서 선탈락.

        Returns
        -------
        tuple
            `(CatalogDumped 이벤트, 상품 dict 리스트)`. LAUNCH 상품은
            제외된 상태의 리스트가 반환된다 (매칭 단계의 통계는 별도 계산).
        """
        http = await self._get_http()
        products: list[dict] = []
        launch_skipped = 0
        for slug, name in self._categories.items():
            try:
                items = await http.fetch_category_listing(
                    slug, max_pages=self._max_pages
                )
            except Exception:
                logger.exception("[nike] 카테고리 덤프 실패: %s %s", slug, name)
                continue
            for item in items:
                if _is_launch(item):
                    launch_skipped += 1
                    continue
                item["_category_slug"] = slug
                item["_category_name"] = name
                products.append(item)

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(products),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info(
            "[nike] 카탈로그 덤프 완료: %d건 (LAUNCH 스킵 %d건)",
            len(products),
            launch_skipped,
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

    def _build_collect_row(
        self, item: dict, model_no: str
    ) -> tuple[str, str, str, str, str]:
        """미등재 신상 → batch flush 용 row 튜플."""
        return (
            normalize_model_number(model_no),
            item.get("brand") or "Nike",
            item.get("name") or "",
            self.source_name,
            item.get("url") or "",
        )

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], MatchStats]:
        """덤프된 상품 리스트 → 크림 DB 매칭 → CandidateMatched publish.

        나이키는 productCode 가 곧 스타일 컬러(모델번호) 이지만,
        이름 안에 `/ 모델번호` 형태로 병기되는 경우도 있어 이름에서 추출하는
        경로를 1순위로 두고, 실패 시 productCode 를 fallback 으로 사용한다.
        """
        stats = MatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []

        for item in products:
            if _is_launch(item):
                # dump_catalog 에서 이미 걸러지지만 방어적 재체크.
                stats.launch_dropped += 1
                continue
            if item.get("isSoldOut"):
                stats.soldout_dropped += 1
                continue

            name = item.get("name") or ""
            model_from_name = extract_model_from_name(name) or ""
            product_code = str(item.get("productCode") or "")
            model_no = model_from_name or product_code
            if not model_no:
                stats.no_model_number += 1
                continue

            key = _strip_key(model_no)
            if not key:
                stats.no_model_number += 1
                continue

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, model_no))
                continue

            # 매칭 가드
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, name):
                logger.info(
                    "[nike] 콜라보 가드 차단: kream=%r source=%r",
                    kream_name[:40],
                    name[:40],
                )
                stats.skipped_guard += 1
                continue
            stype_diff = subtype_mismatch(_keyword_set(kream_name), _keyword_set(name))
            if stype_diff:
                logger.info(
                    "[nike] 서브타입 가드 차단: source=%r extra=%s",
                    name[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # CandidateMatched 발행
            price = int(item.get("price") or 0)
            url = item.get("url") or f"https://www.nike.com/kr/t/_/{product_code}"
            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[nike] 비정수 kream_product_id 스킵: %r",
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

        if pending_collect:
            try:
                inserted = await aenqueue_collect_batch(self._db_path, pending_collect)
                stats.collected_to_queue += inserted
            except Exception:
                logger.warning(
                    "[nike] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[nike] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, products = await self.dump_catalog()
        _, stats = await self.match_to_kream(products)
        return stats.as_dict()


__all__ = ["MatchStats", "NikeAdapter", "DEFAULT_CATEGORIES"]
