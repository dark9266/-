"""컨버스 코리아 (converse.co.kr) 푸시 어댑터.

Cafe24 + magneto edge 기반 한국 공식몰. 카탈로그가 작아 (≈570 SKU)
sitemap.xml 로 전체 product URL 을 한 번에 회수한 뒤, 각 detail 페이지를
순차 GET 해 ``<div class="ma_product_code">XXX</div>`` 모델번호와
og:title / og:price 메타를 파싱한다.

매칭 핵심
---------
* 컨버스 모델번호 패턴: 영문 1자(A/M/W/Y) + 5~6자리 영숫자 + ``C`` 종결.
  (e.g. ``A20645C``, ``M9166C``, ``162050C``) — 크림 ``brand='Converse'``
  풀 (~2,190 행) 과 그대로 exact 매칭된다. 일부 크림 모델번호는
  ``M3310C/M3310`` 같은 슬래시 결합형이라 인덱스 시 슬래시 분리도 함께 키로 등록.
* 다중 후보 → variant 색상 (현재는 og:title 이 한국어 색상까지 포함)
  토큰으로 disambiguation. 실패 시 ambiguous 로만 카운트하고 매칭 보류.

크림 호출 0건. 로컬 SQLite 만 조회.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx

from src.adapters._collect_queue import aenqueue_collect_batch
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


BASE_URL = "https://www.converse.co.kr"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

# sitemap product URL: ``…/product/<slug>/<product_no>/``
_SITEMAP_PRODUCT_RE = re.compile(r"<loc>([^<]*?/product/[^<]+?/(\d+)/)</loc>")

# detail HTML 의 모델번호 컨테이너 — 우선순위 1
_PRODUCT_CODE_DIV_RE = re.compile(
    r'<div[^>]*class="[^"]*ma_product_code[^"]*"[^>]*>\s*([A-Z0-9/_-]{4,32})\s*</div>',
    re.IGNORECASE,
)
# 백업 추출 — 모델번호 패턴 (A20645C / M9160C / 162050C / 10025459-A03 …)
_FALLBACK_MODEL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b([A-Z]\d{5,6}C)\b"),  # A20645C, M9160C
    re.compile(r"\b(\d{6}C)\b"),  # 162050C
    re.compile(r"\b(\d{8}-A\d{2})\b"),  # 10025459-A03
)

_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
    re.IGNORECASE,
)
_OG_PRICE_RE = re.compile(
    r'<meta[^>]+property="product:price[^"]*"[^>]+content="([0-9.]+)"',
    re.IGNORECASE,
)
_META_PRICE_AMOUNT_RE = re.compile(
    r'<meta[^>]+property="product:price:amount"[^>]+content="([0-9.]+)"',
    re.IGNORECASE,
)
# 품절 마커: 상세 페이지 우상단 "SOLDOUT" 배지 / 구매버튼 비활성화
_SOLDOUT_MARKERS = ("displaynone_btn", "soldout_msg", "품절")

# 영문 색상 → 한글 색상 토큰 (stussy 어댑터와 동일 사전에서 컨버스 빈출만 추림)
_COLOR_KO: dict[str, tuple[str, ...]] = {
    "BLACK": ("블랙",),
    "WHITE": ("화이트",),
    "NAVY": ("네이비",),
    "RED": ("레드",),
    "BLUE": ("블루",),
    "GREEN": ("그린",),
    "GREY": ("그레이",),
    "GRAY": ("그레이",),
    "BROWN": ("브라운",),
    "PINK": ("핑크",),
    "BEIGE": ("베이지",),
    "CREAM": ("크림",),
    "IVORY": ("아이보리",),
    "OLIVE": ("올리브",),
    "KHAKI": ("카키",),
    "ORANGE": ("오렌지",),
    "YELLOW": ("옐로우", "옐로"),
    "PURPLE": ("퍼플",),
    "MAROON": ("마룬",),
    "BURGUNDY": ("버건디",),
    "MINT": ("민트",),
    "MUSTARD": ("머스타드",),
    "CHARCOAL": ("차콜", "챠콜"),
    "SAND": ("샌드",),
    "BONE": ("본",),
    "VINTAGE": ("빈티지",),
    "PARCHMENT": ("파치먼트",),
    "STANDARD": ("스탠다드",),
}


@dataclass
class ConverseMatchStats:
    dumped: int = 0
    no_model_number: int = 0
    soldout_dropped: int = 0
    matched: int = 0
    matched_by_color: int = 0
    ambiguous_unresolved: int = 0
    collected_to_queue: int = 0
    fetch_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "no_model_number": self.no_model_number,
            "soldout_dropped": self.soldout_dropped,
            "matched": self.matched,
            "matched_by_color": self.matched_by_color,
            "ambiguous_unresolved": self.ambiguous_unresolved,
            "collected_to_queue": self.collected_to_queue,
            "fetch_errors": self.fetch_errors,
        }


# 한글 색상 토큰 집합 (영문 사전의 모든 한글 매핑을 평탄화 — 양방향 매칭용).
_KR_COLOR_VOCAB: frozenset[str] = frozenset(
    kr for variants in _COLOR_KO.values() for kr in variants
)


def _color_tokens_kr(text: str) -> set[str]:
    """제품명/색상 문자열에서 한글 색상 토큰 집합 추출.

    1) 영문 토큰은 ``_COLOR_KO`` 사전으로 한글 변환
    2) 입력 자체에 한글 색상 단어가 포함돼 있으면 그대로 추가
       (컨버스 detail 페이지 og:title 은 한국어가 기본 — "런스타 샌들 블랙")
    """
    if not text:
        return set()
    out: set[str] = set()
    for tok in re.split(r"[\s/_\-]+", text.upper()):
        if not tok:
            continue
        for kr in _COLOR_KO.get(tok, ()):
            out.add(kr)
    # 한글 색상 직접 검색
    for kr in _KR_COLOR_VOCAB:
        if kr in text:
            out.add(kr)
    return out


def _extract_model_number(html: str) -> str:
    """detail HTML 에서 컨버스 모델번호 추출."""
    m = _PRODUCT_CODE_DIV_RE.search(html)
    if m:
        cand = m.group(1).strip().upper()
        # 추가 검증: 컨버스 모델번호 형태인지
        for r in _FALLBACK_MODEL_RES:
            if r.search(cand):
                return cand
        # 검증 실패해도 div 의 값은 신뢰 (사이트 자체가 모델번호 컨테이너로 사용)
        if 4 <= len(cand) <= 32:
            return cand
    # fallback: 본문에서 패턴 검색
    for r in _FALLBACK_MODEL_RES:
        m2 = r.search(html)
        if m2:
            return m2.group(1).upper()
    return ""


def _extract_price(html: str) -> int:
    for r in (_META_PRICE_AMOUNT_RE, _OG_PRICE_RE):
        m = r.search(html)
        if m:
            try:
                return int(float(m.group(1)))
            except (TypeError, ValueError):
                pass
    return 0


def _extract_title(html: str) -> str:
    m = _OG_TITLE_RE.search(html)
    return m.group(1).strip() if m else ""


def _is_soldout(html: str) -> bool:
    """detail HTML 에서 품절 여부 휴리스틱.

    Cafe24 는 품절 시 ``soldout_msg`` 컨테이너가 표시되거나 구매 버튼이
    ``displaynone_btn`` 으로 숨겨진다. 부분 매칭 휴리스틱이라 false negative
    가 있을 수 있으므로 어댑터의 매칭은 기본적으로 모든 dumped 를 대상으로 하고
    품절은 통계상 표기만 한다 (dumped 에서는 제외하지 않음).
    """
    lower = html.lower()
    return any(mk in lower for mk in ("soldout_msg", "soldoutbg")) and "품절" in html


class ConverseAdapter:
    """컨버스 코리아 카탈로그 덤프 + 크림 DB 매칭 어댑터."""

    source_name: str = "converse"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        max_products: int = 800,
        request_interval_sec: float = 1.0,
    ) -> None:
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._max_products = max_products
        self._request_interval_sec = request_interval_sec

    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        self._http = _DefaultConverseHttp()
        return self._http

    # --------------------------------------------------------------
    # 1) 카탈로그 덤프 (sitemap → detail 순회)
    # --------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        http = await self._get_http()
        items: list[dict] = []

        try:
            sitemap = await http.fetch_sitemap()
        except Exception:
            logger.exception("[converse] sitemap.xml 조회 실패")
            sitemap = ""

        product_urls = self._parse_sitemap_products(sitemap)
        if not product_urls:
            logger.warning("[converse] sitemap 에서 product URL 을 찾지 못함")
        if self._max_products and len(product_urls) > self._max_products:
            product_urls = product_urls[: self._max_products]

        fetch_errors = 0
        for idx, (url, product_no) in enumerate(product_urls):
            if idx > 0 and self._request_interval_sec > 0:
                try:
                    await asyncio.sleep(self._request_interval_sec)
                except Exception:
                    pass
            try:
                html = await http.fetch_detail(url)
            except Exception as e:
                fetch_errors += 1
                logger.debug(
                    "[converse] detail 조회 실패 product_no=%s err=%s",
                    product_no,
                    e,
                )
                continue
            if not html:
                fetch_errors += 1
                continue

            model_no = _extract_model_number(html)
            title = _extract_title(html)
            price = _extract_price(html)
            soldout = _is_soldout(html)
            items.append(
                {
                    "product_no": product_no,
                    "url": url,
                    "model_number": model_no,
                    "title": title,
                    "price": price,
                    "soldout": soldout,
                }
            )

        # fetch_errors 통계는 매칭 단계에서 stats 로 이전 (튜플 외부에 임시 보관)
        self._last_fetch_errors = fetch_errors
        event = CatalogDumped(
            source=self.source_name,
            product_count=len(items),
            dumped_at=time.time(),
        )
        try:
            await self._bus.publish(event)
        except Exception:
            logger.exception("[converse] CatalogDumped publish 실패")
        logger.info(
            "[converse] 카탈로그 덤프 완료: %d products (sitemap urls=%d, fetch_errors=%d)",
            len(items),
            len(product_urls),
            fetch_errors,
        )
        return event, items

    @staticmethod
    def _parse_sitemap_products(xml: str) -> list[tuple[str, str]]:
        if not xml:
            return []
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in _SITEMAP_PRODUCT_RE.finditer(xml):
            url = m.group(1).strip()
            pno = m.group(2)
            if pno in seen:
                continue
            seen.add(pno)
            out.append((url, pno))
        return out

    # --------------------------------------------------------------
    # 2) 크림 DB 인덱스 — Converse 풀의 모델번호 다중 인덱스
    # --------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, list[dict]]:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number "
                "FROM kream_products "
                "WHERE brand = 'Converse' AND model_number != ''"
            ).fetchall()
        finally:
            conn.close()
        index: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            mn = (row["model_number"] or "").strip().upper()
            if not mn:
                continue
            # 슬래시 결합형 (e.g. "M3310C/M3310") → 각 토큰 모두 인덱스
            for token in re.split(r"[\s/]+", mn):
                token = token.strip()
                if not token:
                    continue
                index[normalize_model_number(token)].append(dict(row))
        return index

    # --------------------------------------------------------------
    # 3) 매칭
    # --------------------------------------------------------------
    async def match_to_kream(
        self, items: list[dict]
    ) -> tuple[list[CandidateMatched], ConverseMatchStats]:
        stats = ConverseMatchStats(dumped=len(items))
        stats.fetch_errors = getattr(self, "_last_fetch_errors", 0)
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []
        seen_models: set[str] = set()

        for item in items:
            raw_model = (item.get("model_number") or "").strip()
            if not raw_model:
                stats.no_model_number += 1
                continue

            if item.get("soldout"):
                stats.soldout_dropped += 1
                # 품절은 후보에서 제외 — 정확성 우선

            norm = normalize_model_number(raw_model)
            if norm in seen_models:
                continue
            seen_models.add(norm)

            candidates = kream_index.get(norm)
            if not candidates:
                # 슬래시 결합형 입력 처리 (e.g. ``150206C/A08796C``)
                if "/" in norm:
                    for tok in norm.split("/"):
                        tok = tok.strip()
                        if tok and tok in kream_index:
                            candidates = kream_index[tok]
                            break
            if not candidates:
                if not item.get("soldout"):
                    pending_collect.append(self._build_collect_row(item, norm))
                continue

            if item.get("soldout"):
                # 매칭은 가능하지만 후보 발행은 보류 (재고 0 → 후속 hot 폴링이 처리)
                continue

            chosen: dict | None = None
            matched_via_color = False
            if len(candidates) == 1:
                chosen = candidates[0]
            else:
                color_tokens = _color_tokens_kr(item.get("title") or "")
                if color_tokens:
                    color_hits = [
                        c
                        for c in candidates
                        if any(tok in (c.get("name") or "") for tok in color_tokens)
                    ]
                    if len(color_hits) == 1:
                        chosen = color_hits[0]
                        matched_via_color = True
                if chosen is None:
                    stats.ambiguous_unresolved += 1
                    continue

            try:
                kream_product_id = int(chosen["product_id"])
            except (TypeError, ValueError):
                stats.ambiguous_unresolved += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=norm,
                retail_price=int(item.get("price") or 0),
                size="",
                url=item.get("url") or BASE_URL,
            )
            try:
                await self._bus.publish(candidate)
            except Exception:
                logger.exception("[converse] CandidateMatched publish 실패")
                continue
            matched.append(candidate)
            stats.matched += 1
            if matched_via_color:
                stats.matched_by_color += 1

        if pending_collect:
            try:
                inserted = await aenqueue_collect_batch(self._db_path, pending_collect)
                stats.collected_to_queue += inserted
            except Exception as e:
                logger.warning(
                    "[converse] collect_queue 배치 flush 실패: n=%d type=%s msg=%s",
                    len(pending_collect),
                    type(e).__name__,
                    str(e)[:200],
                )

        logger.info("[converse] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    def _build_collect_row(self, item: dict, norm_model: str) -> tuple[str, str, str, str, str]:
        return (
            norm_model,
            "Converse",
            item.get("title") or "",
            self.source_name,
            item.get("url") or BASE_URL,
        )

    async def run_once(self) -> dict[str, int]:
        try:
            _, items = await self.dump_catalog()
            _, stats = await self.match_to_kream(items)
            return stats.as_dict()
        except Exception:
            logger.exception("[converse] run_once 실패")
            return ConverseMatchStats().as_dict()


class _DefaultConverseHttp:
    """기본 HTTP 레이어 — sitemap.xml + detail HTML GET."""

    _HEADERS = {
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Safari/605.1.15"
        ),
    }

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self._HEADERS,
                timeout=20.0,
                follow_redirects=True,
            )
        return self._client

    async def fetch_sitemap(self) -> str:
        client = await self._get_client()
        try:
            resp = await client.get(SITEMAP_URL)
        except httpx.HTTPError as e:
            logger.warning("[converse] sitemap HTTP 오류: %s", e)
            return ""
        if resp.status_code != 200:
            logger.warning("[converse] sitemap HTTP %d", resp.status_code)
            return ""
        return resp.text

    async def fetch_detail(self, url: str) -> str:
        client = await self._get_client()
        try:
            resp = await client.get(url)
        except httpx.HTTPError as e:
            logger.debug("[converse] detail HTTP 오류 url=%s: %s", url, e)
            return ""
        if resp.status_code != 200:
            return ""
        return resp.text


__all__ = ["ConverseAdapter", "ConverseMatchStats", "BASE_URL", "SITEMAP_URL"]
