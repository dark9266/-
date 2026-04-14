r"""Carhartt WIP 글로벌 공식몰 (carhartt-wip.com) 푸시 어댑터.

한국 공식몰(carhartt-wip.co.kr)은 **WORKSOUT 이 운영하는 사이트**라 내부 SKU
포맷(``CAAACOJAJL00040002``)이 브랜드 style-number (``I033112-00E-02``) 와
전혀 다르다 — 크림 ``brand='Carhartt WIP'`` 풀 (≈1,984 행) 과 매칭 불가.

그래서 글로벌 EU 스토어 (`www.carhartt-wip.com/en-gb`) 를 사용한다. SSR HTML
+ JSON-LD 구조라 httpx 로 충분하고, 제품 상세 페이지에 **브랜드 공식
style-number 가 전 색상 variant 로 노출**된다:

    {"sku": "I034871_25", "name": "OG Single Knee Pant", "price": "165", ...}
    I034871_01_01 / I034871_01_4Q / I034871_01_UR ...

언더스코어 → 대시 정규화 후 크림 model_number 와 exact 매칭된다
(크림: ``I034871-01-02``, ``I034871-01-UR``, ``I033112-00E-02`` …).

카탈로그 덤프 경로
------------------
1. ``/sitemap.xml`` → 로케일별 sitemap index
2. ``/en-gb/product/sitemap.xml`` → ~900 product URL
3. 각 URL HTML GET → JSON-LD ``sku`` + 본문 ``I\d{6}_XX(_XX)?`` 변형 전부 수집

한 product page 가 여러 색상 variant 를 포함하므로 ``extracted_variants`` 는
평탄화 (product_url, variant_code) 튜플 리스트로 취급한다.

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

from src.adapters._collect_queue import enqueue_collect_batch
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


BASE_URL = "https://www.carhartt-wip.com"
LOCALE_PATH = "/en-gb"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap.xml"
PRODUCT_SITEMAP_URL = f"{BASE_URL}{LOCALE_PATH}/product/sitemap.xml"

# sitemap `<loc>…/p/<slug>-<product_no></loc>`
_SITEMAP_LOC_RE = re.compile(r"<loc>([^<]+/p/[^<]+)</loc>", re.IGNORECASE)

# 본문 전체에서 style-number 변형 수집.
# 실제 관측 패턴:
#   I034871_25             (JSON-LD sku — color index)
#   I034871_01_01          (3 파트 — 크림 포맷과 동일)
#   I034871_01_4Q / _UR    (사이즈/워시 코드가 영문 포함)
#   I023083_HZ_01          (알파+숫자 코드)
_STYLE_CODE_RE = re.compile(r"\b([IA]\d{6}_[A-Z0-9]{1,4}(?:_[A-Z0-9]{1,4})?)\b")

# JSON-LD 메인 필드
_LD_SKU_RE = re.compile(r'"sku"\s*:\s*"([^"]+)"')
_LD_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
_LD_PRICE_RE = re.compile(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)')
_LD_AVAIL_RE = re.compile(r'"availability"\s*:\s*"[^"]*?/(\w+)"')
_LD_CURR_RE = re.compile(r'"priceCurrency"\s*:\s*"([A-Z]{3})"')


@dataclass
class CarharttMatchStats:
    dumped: int = 0
    variants_total: int = 0
    no_model_number: int = 0
    soldout_dropped: int = 0
    matched: int = 0
    collected_to_queue: int = 0
    fetch_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "variants_total": self.variants_total,
            "no_model_number": self.no_model_number,
            "soldout_dropped": self.soldout_dropped,
            "matched": self.matched,
            "collected_to_queue": self.collected_to_queue,
            "fetch_errors": self.fetch_errors,
        }


def _extract_variant_codes(html: str) -> list[str]:
    """상세 HTML 에서 모든 style-number variant 수집 (순서 보존, dedup).

    sku (주 variant) 를 먼저 앞에 오도록 정렬한다.
    """
    if not html:
        return []
    out: list[str] = []
    seen: set[str] = set()

    sku_m = _LD_SKU_RE.search(html)
    if sku_m:
        sku = sku_m.group(1).strip().upper()
        if _STYLE_CODE_RE.fullmatch(sku):
            seen.add(sku)
            out.append(sku)

    for m in _STYLE_CODE_RE.finditer(html):
        code = m.group(1).upper()
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _extract_name(html: str) -> str:
    m = _LD_NAME_RE.search(html)
    return m.group(1).strip() if m else ""


def _extract_price(html: str) -> int:
    m = _LD_PRICE_RE.search(html)
    if not m:
        return 0
    try:
        return int(float(m.group(1)))
    except (TypeError, ValueError):
        return 0


def _extract_currency(html: str) -> str:
    m = _LD_CURR_RE.search(html)
    return m.group(1).strip().upper() if m else ""


def _is_soldout(html: str) -> bool:
    """JSON-LD availability 가 OutOfStock/SoldOut 이면 품절."""
    m = _LD_AVAIL_RE.search(html or "")
    if not m:
        return False
    token = m.group(1).lower()
    return token in ("outofstock", "soldout", "discontinued")


def _normalize_variant(code: str) -> str:
    """``I034871_01_01`` → ``I034871-01-01`` (크림 모델번호 포맷).

    크림 model_number 정규화(`normalize_model_number`) 는 대시를 유지하고
    공백/슬래시만 처리하므로, 언더스코어를 미리 대시로 치환해 동일 키를 만든다.
    """
    return normalize_model_number(code.replace("_", "-"))


class CarharttAdapter:
    """Carhartt WIP 글로벌 스토어 카탈로그 덤프 + 크림 DB 매칭."""

    source_name: str = "carhartt"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        max_products: int = 1000,
        request_interval_sec: float = 1.5,
    ) -> None:
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._max_products = max_products
        self._request_interval_sec = request_interval_sec
        self._last_fetch_errors: int = 0

    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        self._http = _DefaultCarharttHttp()
        return self._http

    # --------------------------------------------------------------
    # 1) 카탈로그 덤프
    # --------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        http = await self._get_http()
        items: list[dict] = []

        try:
            sitemap_xml = await http.fetch_sitemap()
        except Exception:
            logger.exception("[carhartt] sitemap 조회 실패")
            sitemap_xml = ""

        product_urls = self._parse_sitemap(sitemap_xml)
        if not product_urls:
            logger.warning("[carhartt] product sitemap 비어있음")
        if self._max_products and len(product_urls) > self._max_products:
            product_urls = product_urls[: self._max_products]

        fetch_errors = 0
        for idx, url in enumerate(product_urls):
            if idx > 0 and self._request_interval_sec > 0:
                try:
                    await asyncio.sleep(self._request_interval_sec)
                except Exception:
                    pass
            try:
                html = await http.fetch_detail(url)
            except Exception as e:
                fetch_errors += 1
                logger.debug("[carhartt] detail 조회 실패 url=%s err=%s", url, e)
                continue
            if not html:
                fetch_errors += 1
                continue

            variants = _extract_variant_codes(html)
            name = _extract_name(html)
            price = _extract_price(html)
            currency = _extract_currency(html)
            soldout = _is_soldout(html)
            items.append(
                {
                    "url": url,
                    "variants": variants,
                    "name": name,
                    "price": price,
                    "currency": currency,
                    "soldout": soldout,
                }
            )

        self._last_fetch_errors = fetch_errors
        event = CatalogDumped(
            source=self.source_name,
            product_count=len(items),
            dumped_at=time.time(),
        )
        try:
            await self._bus.publish(event)
        except Exception:
            logger.exception("[carhartt] CatalogDumped publish 실패")
        logger.info(
            "[carhartt] 카탈로그 덤프 완료: %d products (urls=%d, fetch_errors=%d)",
            len(items),
            len(product_urls),
            fetch_errors,
        )
        return event, items

    @staticmethod
    def _parse_sitemap(xml: str) -> list[str]:
        if not xml:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for m in _SITEMAP_LOC_RE.finditer(xml):
            url = m.group(1).strip()
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    # --------------------------------------------------------------
    # 2) 크림 DB 인덱스
    # --------------------------------------------------------------
    def _load_kream_index(self) -> dict[str, list[dict]]:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number "
                "FROM kream_products "
                "WHERE brand = 'Carhartt WIP' AND model_number != ''"
            ).fetchall()
        finally:
            conn.close()
        index: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            mn = (row["model_number"] or "").strip().upper()
            if not mn:
                continue
            # 슬래시 결합형 (e.g. ``I006288/I031468-89-XX``) → 각 토큰 인덱스
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
    ) -> tuple[list[CandidateMatched], CarharttMatchStats]:
        stats = CarharttMatchStats(dumped=len(items))
        stats.fetch_errors = self._last_fetch_errors
        kream_index = self._load_kream_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []
        seen_norms: set[str] = set()

        for item in items:
            variants = item.get("variants") or []
            if not variants:
                stats.no_model_number += 1
                continue

            url = item.get("url") or f"{BASE_URL}{LOCALE_PATH}"
            name = item.get("name") or ""
            # 글로벌 EU 가격은 GBP/EUR — retail_price 는 수익 계산용이므로
            # 실제 한국 소비자 가격이 아니면 0 으로 두고 크림 sell_now 매칭만
            # 책임진다. (price 는 참고용, 후속 raw 정보는 collect_queue 에 이름으로)
            stats.variants_total += len(variants)

            for code in variants:
                norm = _normalize_variant(code)
                if not norm or norm in seen_norms:
                    continue
                seen_norms.add(norm)

                candidates = kream_index.get(norm)
                if not candidates:
                    # prefix 만 일치하는 경우는 무시 — exact 매칭만 사용
                    if not item.get("soldout"):
                        pending_collect.append(
                            (
                                norm,
                                "Carhartt WIP",
                                name,
                                self.source_name,
                                url,
                            )
                        )
                    continue

                if item.get("soldout"):
                    stats.soldout_dropped += 1
                    continue

                # 글로벌 style-number 는 크림에서 동일 토큰이 보통 1 행 — 다중이면
                # 콜라보/서브타입 엣지라 첫 후보 채택 후 동률은 ambiguous 로 스킵
                if len(candidates) > 1:
                    # 동일 정규화 키에 여러 행이 걸리는 경우는 거의 없음
                    # (슬래시 결합형에 의해 발생). 그냥 첫번째 채택.
                    pass
                chosen = candidates[0]
                try:
                    kream_product_id = int(chosen["product_id"])
                except (TypeError, ValueError):
                    continue

                candidate = CandidateMatched(
                    source=self.source_name,
                    kream_product_id=kream_product_id,
                    model_no=norm,
                    retail_price=int(item.get("price") or 0),
                    size="",
                    url=url,
                )
                try:
                    await self._bus.publish(candidate)
                except Exception:
                    logger.exception("[carhartt] CandidateMatched publish 실패")
                    continue
                matched.append(candidate)
                stats.matched += 1

        if pending_collect:
            try:
                inserted = enqueue_collect_batch(self._db_path, pending_collect)
                stats.collected_to_queue += inserted
            except Exception as e:
                logger.warning(
                    "[carhartt] collect_queue 배치 flush 실패: n=%d type=%s msg=%s",
                    len(pending_collect),
                    type(e).__name__,
                    str(e)[:200],
                )

        logger.info("[carhartt] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    async def run_once(self) -> dict[str, int]:
        try:
            _, items = await self.dump_catalog()
            _, stats = await self.match_to_kream(items)
            return stats.as_dict()
        except Exception:
            logger.exception("[carhartt] run_once 실패")
            return CarharttMatchStats().as_dict()


class _DefaultCarharttHttp:
    """기본 HTTP 레이어 — sitemap + detail HTML GET."""

    _HEADERS = {
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-GB,en;q=0.9,ko;q=0.8",
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
                timeout=25.0,
                follow_redirects=True,
            )
        return self._client

    async def fetch_sitemap(self) -> str:
        client = await self._get_client()
        try:
            resp = await client.get(PRODUCT_SITEMAP_URL)
        except httpx.HTTPError as e:
            logger.warning("[carhartt] product sitemap HTTP 오류: %s", e)
            return ""
        if resp.status_code != 200:
            logger.warning("[carhartt] product sitemap HTTP %d", resp.status_code)
            return ""
        return resp.text

    async def fetch_detail(self, url: str) -> str:
        client = await self._get_client()
        try:
            resp = await client.get(url)
        except httpx.HTTPError as e:
            logger.debug("[carhartt] detail HTTP 오류 url=%s: %s", url, e)
            return ""
        if resp.status_code != 200:
            return ""
        return resp.text


__all__ = [
    "CarharttAdapter",
    "CarharttMatchStats",
    "BASE_URL",
    "LOCALE_PATH",
    "PRODUCT_SITEMAP_URL",
]
