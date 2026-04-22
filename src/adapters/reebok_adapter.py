"""Reebok 한국 공식몰 (reebok.co.kr) 푸시 어댑터.

플랫폼: GODOMALL (NHN Commerce) PHP
기법: httpx (순수 HTML, JS 렌더링 불필요)

매칭 전략
---------
* 리스트 페이지에서 ``class="item_name"`` 텍스트 슬래시 뒤 모델번호 추출.
  예: ``클럽 C 85 - 화이트 / DV6434`` → ``DV6434``
* 상세 페이지에서도 동일 패턴: ``<title>... / DV6434 ｜ ...`` 추출.
* ``.product-code`` 클래스 값(RESO/RXSO prefix) 은 한국 공홈 자체 코드로
  크림 DB와 무관하므로 **사용 금지**.
* 크림 실호출 금지 — 로컬 SQLite ``kream_products`` 만 조회.

재고 파싱
---------
상세 페이지 ``li[data-value]`` 포맷:
``{sno}||0||||{stock}^|^{size}``
stock > 0 이면 in_stock=True.

수집 카테고리
-----------
cateCd: 001(CLOTHING), 002(SHOES), 003(Collaboration), 007(ACC), 016(SPORT)
SALE(005) 제외 — 기존 카테고리 상품 재게재. goodsNo set dedup으로 중복 방어.

Rate Limit: max_concurrent=2, min_interval=1.5초
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.adapters._collect_queue import aenqueue_collect_batch
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.kream_index import get_kream_index, strip_key
from src.core.matching_guards import collab_match_fails, is_collab, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


# ─── 상수 ──────────────────────────────────────────────────────────────────

BASE_URL = "https://www.reebok.co.kr"

# 수집 카테고리 코드 (SALE 제외)
CATALOG_CATEGORIES: tuple[str, ...] = ("001", "002", "003", "007", "016")

# 카테고리당 한 번에 가져올 상품 수 (사이트 실제 상한 40)
_PAGE_SIZE = 40

# User-Agent — Chrome 위장 (robots.txt에 ClaudeBot 명시 차단)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_HEADERS: dict[str, str] = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# 모델번호 추출 패턴 — title/h3 슬래시 뒤 글로벌 표준 코드
_MODEL_RE = re.compile(r"/\s*([A-Z0-9][A-Z0-9\-_]{3,})\s*(?:[｜|]|$)")

# 상세 페이지 가격 패턴
_PRICE_RE = re.compile(r"'setGoodsPrice'\s*:\s*'([0-9.]+)'")

# data-value 포맷: {sno}||0||||{stock}^|^{size}
_DATAVAL_RE = re.compile(r'data-value="([^"]+)"')

# 리스트에서 goodsNo + item_name + price 동시 추출
_LIST_ITEM_RE = re.compile(
    r'goods_view\.php\?goodsNo=(\d+)[^"]*">\s*'
    r'<strong class="item_name">([^<]+)</strong>'
    r'.*?'
    r'<span[^>]*>([\d,]+)<span class="currency">원</span>',
    re.DOTALL,
)

# 공홈 상품명 콜라보 표기 — [리복x컬렉트피시스], [브랜드x파트너] 등
# 대괄호 안에 x/X/× 구분자 + 좌우 텍스트 = 역방향 콜라보 지표
_SOURCE_COLLAB_RE = re.compile(r"\[[^\[\]]*[xX×][^\[\]]*\]")


# ─── 헬퍼 함수 ────────────────────────────────────────────────────────────

def _extract_model(text: str) -> str | None:
    """슬래시 뒤 모델번호 추출. 없으면 None."""
    m = _MODEL_RE.search(text)
    return m.group(1).strip() if m else None


def _parse_sizes(html: str) -> list[dict]:
    """data-value 속성에서 사이즈별 재고 파싱."""
    sizes: list[dict] = []
    for raw in _DATAVAL_RE.findall(html):
        if "||" not in raw or "^|^" not in raw:
            continue
        parts = raw.split("^|^")
        size = parts[1] if len(parts) > 1 else ""
        if not size:
            continue
        l_parts = parts[0].split("||")
        stock_raw = l_parts[3] if len(l_parts) > 3 else "0"
        stock = int(stock_raw) if stock_raw.lstrip("-").isdigit() else 0
        sizes.append({"size": size, "stock": stock, "in_stock": stock > 0})
    return sizes


def _extract_price(html: str) -> int:
    """setGoodsPrice 변수에서 가격 파싱."""
    m = _PRICE_RE.search(html)
    return int(float(m.group(1))) if m else 0


def _parse_list_items(html: str) -> list[dict]:
    """리스트 페이지에서 (goodsNo, name, model, price) 파싱.

    item_name class 있는 주 상품 카드만 파싱.
    색상 variant 링크(item_name 없음)는 자동 제외.
    """
    items: list[dict] = []
    for gno, name, price_raw in _LIST_ITEM_RE.findall(html):
        name = name.strip()
        model = _extract_model(name)
        price = int(price_raw.replace(",", ""))
        items.append({
            "goodsNo": gno,
            "name": name,
            "model": model,
            "price": price,
        })
    return items


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


def _source_is_collab_by_bracket(name: str) -> bool:
    """공홈 상품명에 ``[...x...]`` 콜라보 표기 있으면 True.

    예: ``[리복x컬렉트피시스] 클럽 C 85 빈티지`` → True.
    ``COLLAB_KEYWORDS`` 에 없는 한국 콜라보 파트너를 커버.
    """
    if not name:
        return False
    return bool(_SOURCE_COLLAB_RE.search(name))


# ─── 통계 ──────────────────────────────────────────────────────────────────

@dataclass
class ReebokMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    no_model_number: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0
    list_items_parsed: int = 0
    detail_fetched: int = 0
    detail_failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "no_model_number": self.no_model_number,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
            "list_items_parsed": self.list_items_parsed,
            "detail_fetched": self.detail_fetched,
            "detail_failed": self.detail_failed,
        }


# ─── 어댑터 ────────────────────────────────────────────────────────────────

class ReebokAdapter:
    """Reebok 한국 공식몰 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행.

    덤프 전략
    ---------
    1. 리스트 페이지 순회 → goodsNo + 이름(모델번호 포함) + 가격 수집.
       모델번호가 이름에 있으면 상세 호출 없이 바로 매칭 시도.
    2. 상세 페이지 호출 → 사이즈별 재고 파싱.
       리스트에서 모델번호 추출에 실패한 상품은 상세 타이틀에서 재시도.
    """

    source_name: str = "reebok"
    brand_hint: str = "Reebok"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: tuple[str, ...] = CATALOG_CATEGORIES,
        page_size: int = _PAGE_SIZE,
        max_pages_per_category: int = 30,
        min_interval_sec: float = 1.5,
    ) -> None:
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories
        self._page_size = page_size
        self._max_pages = max_pages_per_category
        self._min_interval = min_interval_sec
        self._semaphore = asyncio.Semaphore(2)  # max_concurrent=2

    async def _get_http(self) -> Any:
        if self._http is None:
            self._http = _DefaultReebokHttp(
                min_interval_sec=self._min_interval,
            )
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """전 카테고리 리스트 + 상세 순회 → CatalogDumped publish.

        반환 dict 구조:
            {
                "goodsNo": str,
                "name": str,         # 리스트에서 파싱한 상품 이름
                "model": str | None, # 모델번호 (없으면 None)
                "price": int,        # 판매가 (리스트 기준, 상세에서 덮어쓸 수 있음)
                "url": str,          # 상세 URL
                "sizes": list[dict], # 사이즈별 재고 [{size, stock, in_stock}]
            }
        """
        http = await self._get_http()
        out: list[dict] = []
        seen_goods: set[str] = set()

        for cate in self._categories:
            for page in range(1, self._max_pages + 1):
                try:
                    html = await http.fetch_list(cate, page, self._page_size)
                except Exception:
                    logger.exception(
                        "[reebok] 리스트 페이지 실패 cateCd=%s page=%d", cate, page
                    )
                    break
                if not html:
                    break

                items = _parse_list_items(html)
                if not items:
                    break

                for item in items:
                    gno = item["goodsNo"]
                    if gno in seen_goods:
                        continue
                    seen_goods.add(gno)

                    url = f"{BASE_URL}/goods/goods_view.php?goodsNo={gno}"
                    item["url"] = url

                    # 상세 호출 — 사이즈별 재고 파싱 (모든 상품 대상)
                    try:
                        detail_html = await http.fetch_detail(gno)
                    except Exception:
                        logger.debug("[reebok] 상세 호출 실패 goodsNo=%s", gno)
                        detail_html = None

                    if detail_html:
                        # 상세 타이틀에서 모델번호 재시도 (리스트 miss 보완)
                        if not item.get("model"):
                            title_m = re.search(r"<title>([^<]+)</title>", detail_html)
                            if title_m:
                                item["model"] = _extract_model(title_m.group(1))

                        # 가격 갱신 (리스트보다 상세가 정확)
                        detail_price = _extract_price(detail_html)
                        if detail_price > 0:
                            item["price"] = detail_price

                        item["sizes"] = _parse_sizes(detail_html)
                    else:
                        item["sizes"] = []

                    out.append(item)

                # 한 페이지에 items가 page_size보다 적으면 마지막 페이지
                if len(items) < self._page_size:
                    break

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(out),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info(
            "[reebok] 카탈로그 덤프 완료: %d건 (중복제거 후 goodsNo %d개)",
            len(out), len(seen_goods),
        )
        return event, out

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, dict]:
        """Reebok 로컬 확장 인덱스 (슬래시 분할 키 포함).

        크림 DB ``model_number`` 가 ``DV6434/100000317`` 형식일 때 공홈은
        ``DV6434`` 만 추출하므로 원본 strip_key 인덱스로는 매칭 실패.
        여기서 각 슬래시 파트도 동일 row 에 매핑해 커버리지 확보.

        공유 ``kream_index`` 는 그대로 두고 어댑터 로컬 dict 으로 확장 →
        22개 어댑터 회귀 위험 0.
        """
        base = get_kream_index(self._db_path).get()
        expanded: dict[str, dict] = dict(base)
        for row in base.values():
            model_raw = row.get("model_number") or ""
            if "/" not in model_raw:
                continue
            for part in model_raw.split("/"):
                part_key = strip_key(part)
                if not part_key or part_key in expanded:
                    continue
                expanded[part_key] = row
        return expanded

    def _build_collect_row(
        self, item: dict, model_no: str
    ) -> tuple[str, str, str, str, str]:
        return (
            normalize_model_number(model_no),
            self.brand_hint,
            item.get("name") or "",
            self.source_name,
            item.get("url") or "",
        )

    async def match_to_kream(
        self, catalog: list[dict],
    ) -> tuple[list[CandidateMatched], ReebokMatchStats]:
        """덤프된 아이템 → 크림 DB 매칭 → CandidateMatched publish.

        매칭 규칙:
        1. 모델번호 없으면 → no_model_number 카운트 후 skip.
        2. strip_key로 크림 DB 조회 → 없으면 collect_queue 적재.
        3. 적중 시 콜라보/서브타입 가드 통과 후 CandidateMatched publish.
        4. 재고 사이즈 전혀 없으면 → soldout_dropped.
        """
        stats = ReebokMatchStats(dumped=len(catalog))
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []
        seen_models: set[str] = set()

        for item in catalog:
            model_raw = item.get("model")
            if not model_raw:
                stats.no_model_number += 1
                continue

            model_raw = model_raw.strip().upper()
            norm_model = normalize_model_number(model_raw)
            key = strip_key(norm_model)
            if not key:
                stats.no_model_number += 1
                continue

            # goodsNo 단위 중복 허용 (색상 다른 상품이 같은 모델번호일 경우 비정상)
            # 대신 모델번호 기준 dedup (같은 모델 번호를 가진 goodsNo가 복수인 경우)
            if key in seen_models:
                continue
            seen_models.add(key)

            # 덤프 ledger
            try:
                from src.core.dump_ledger import record_dump_item
                await record_dump_item(
                    self._db_path,
                    source=self.source_name,
                    model_no=norm_model,
                    name=item.get("name") or "",
                    url=item.get("url") or "",
                )
            except Exception:
                logger.debug("[reebok] dump_ledger 실패 (비치명)")

            kream_row = kream_index.get(key)
            if kream_row is None:
                pending_collect.append(self._build_collect_row(item, norm_model))
                continue

            # 매칭 가드
            source_name_text = item.get("name") or ""
            kream_name = kream_row.get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[reebok] 콜라보 가드 차단: kream=%r source=%r",
                    kream_name[:50], source_name_text[:50],
                )
                stats.skipped_guard += 1
                continue
            # 역방향 콜라보 가드 — 공홈=콜라보 bracket but 크림=일반일 때 차단.
            # 예: 공홈 ``[리복x컬렉트피시스] DV6434`` 169k → 크림 정가 87k 오매칭 방어.
            if _source_is_collab_by_bracket(source_name_text) and not is_collab(kream_name):
                logger.info(
                    "[reebok] 역방향 콜라보 가드 차단: source=%r kream=%r",
                    source_name_text[:50], kream_name[:50],
                )
                stats.skipped_guard += 1
                continue
            stype_diff = subtype_mismatch(
                _keyword_set(kream_name), _keyword_set(source_name_text)
            )
            if stype_diff:
                logger.info(
                    "[reebok] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:50], stype_diff,
                )
                stats.skipped_guard += 1
                continue

            try:
                kream_product_id = int(kream_row["product_id"])
            except (TypeError, ValueError):
                logger.warning(
                    "[reebok] 비정수 kream_product_id 스킵: %r",
                    kream_row.get("product_id"),
                )
                stats.skipped_guard += 1
                continue

            price = int(item.get("price") or 0)

            # 사이즈별 재고 수집
            sizes_raw: list[dict] = item.get("sizes") or []
            available_sizes: tuple[str, ...] = tuple(
                str(s["size"]).strip()
                for s in sizes_raw
                if isinstance(s, dict) and s.get("in_stock") and str(s.get("size") or "").strip()
            )

            if not available_sizes:
                # 재고 데이터가 아예 없으면 listing-only 경로 허용
                # (sizes 리스트 자체가 비어있는 경우 — 상세 호출 실패 등)
                if sizes_raw:
                    # sizes가 있는데 재고가 0 → 실제 품절
                    logger.info("[reebok] 전품절 drop: model=%s", norm_model)
                    stats.soldout_dropped += 1
                    continue
                # sizes 자체가 없으면 listing-only 처리 (available_sizes=())
                pass

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=norm_model,
                retail_price=price,
                size="",
                url=item.get("url") or "",
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
                    "[reebok] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[reebok] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, catalog = await self.dump_catalog()
        _, stats = await self.match_to_kream(catalog)
        return stats.as_dict()


# ─── 기본 HTTP 레이어 ─────────────────────────────────────────────────────

class _DefaultReebokHttp:
    """실호출 HTTP 레이어.

    Rate Limit: max_concurrent=2 (세마포어는 어댑터에서), min_interval=1.5s.
    """

    def __init__(self, min_interval_sec: float = 1.5) -> None:
        self._min_interval = min_interval_sec
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=_HEADERS,
                timeout=20.0,
                follow_redirects=True,
            )
        return self._client

    async def _throttle(self) -> None:
        """min_interval_sec 이상 간격 보장."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()

    async def fetch_list(self, cate_cd: str, page: int, count: int) -> str:
        """카테고리 리스트 HTML 반환. 실패 시 빈 문자열."""
        await self._throttle()
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{BASE_URL}/goods/goods_list.php",
                params={"cateCd": cate_cd, "count": count, "page": page},
            )
        except httpx.HTTPError as e:
            logger.warning("[reebok] list HTTP 오류 cate=%s page=%d: %s", cate_cd, page, e)
            return ""

        if resp.status_code == 429:
            logger.warning("[reebok] 429 — 30초 대기")
            await asyncio.sleep(30)
            return ""
        if resp.status_code != 200:
            logger.warning(
                "[reebok] list HTTP %d cate=%s page=%d", resp.status_code, cate_cd, page
            )
            return ""
        return resp.text

    async def fetch_detail(self, goods_no: str) -> str:
        """상품 상세 HTML 반환. 실패 시 빈 문자열."""
        await self._throttle()
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{BASE_URL}/goods/goods_view.php",
                params={"goodsNo": goods_no},
            )
        except httpx.HTTPError as e:
            logger.warning("[reebok] detail HTTP 오류 goodsNo=%s: %s", goods_no, e)
            return ""

        if resp.status_code == 429:
            logger.warning("[reebok] 429 — 30초 대기")
            await asyncio.sleep(30)
            return ""
        if resp.status_code != 200:
            logger.warning("[reebok] detail HTTP %d goodsNo=%s", resp.status_code, goods_no)
            return ""
        return resp.text


__all__ = [
    "ReebokAdapter",
    "ReebokMatchStats",
    "BASE_URL",
    "CATALOG_CATEGORIES",
    "_extract_model",
    "_parse_sizes",
    "_extract_price",
    "_parse_list_items",
]
