"""Stussy 한국 공식몰 (kr.stussy.com) 푸시 어댑터.

stussy-kr.myshopify.com Shopify 기반. `/products.json?limit=250&page=N`
페이지네이션으로 전체 카탈로그 덤프 (총 ~750 products / 4 pages).

매칭 핵심
---------
* Shopify variant.sku 형식: ``<digit_prefix>-<color4>-<size>``
  - 예: ``1975000-BLAC-S``, ``117273-BLAC-M``, ``1311061-SHAW-EA``
* 크림 model_number 는 digit prefix 그대로 (`1975000`) 또는 `/M` 같은
  접미가 붙음 (`1975000/M`). 동일 prefix 에 색상이 다른 여러 행이 존재.
* 매칭은 **digit prefix → kream rows** 인덱스. 다중 후보 시 색상으로 1차
  disambiguation 시도, 실패 시 ambiguous 통계로만 카운트하고 매칭 보류
  (정확도 우선).

크림 호출 0건. 로컬 SQLite 만 조회.
"""

from __future__ import annotations

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


BASE_URL = "https://kr.stussy.com"

# Stussy SKU 패턴 — 6~7 digit prefix 로 시작.
_STUSSY_SKU_PREFIX_RE = re.compile(r"^(\d{5,8})")

# 크림 model_number 에서 digit prefix 추출.
_KREAM_DIGIT_PREFIX_RE = re.compile(r"^(\d{5,8})")

# 영문 색상 토큰 → 한글 색상 토큰 매핑 (Stussy 카탈로그 빈출).
# variant.option1 (`Black`, `Pigment Dyed Bone`, `Light Grey`) 토큰화 후
# 각 토큰을 매핑해 kream goodsName 에 매칭.
_COLOR_KO: dict[str, tuple[str, ...]] = {
    "BLACK":   ("블랙",),
    "WHITE":   ("화이트",),
    "NATURAL": ("내츄럴", "네추럴", "내추럴"),
    "BONE":    ("본",),
    "SAND":    ("샌드",),
    "OLIVE":   ("올리브",),
    "NAVY":    ("네이비",),
    "RED":     ("레드",),
    "BROWN":   ("브라운",),
    "PINK":    ("핑크",),
    "BLUE":    ("블루",),
    "CREAM":   ("크림",),
    "CHARCOAL":("차콜", "챠콜"),
    "GREY":    ("그레이",),
    "GRAY":    ("그레이",),
    "KHAKI":   ("카키",),
    "TAN":     ("탄",),
    "ORANGE":  ("오렌지",),
    "PURPLE":  ("퍼플",),
    "GOLD":    ("골드",),
    "SILVER":  ("실버",),
    "MUSTARD": ("머스타드",),
    "FOREST":  ("포레스트",),
    "AQUA":    ("아쿠아",),
    "YELLOW":  ("옐로우", "옐로"),
    "GREEN":   ("그린",),
    "TURQUOISE": ("터콰이즈",),
    "MAROON":  ("마룬",),
    "INDIGO":  ("인디고",),
    "WASHED":  ("워시드",),
    "PIGMENT": ("피그먼트",),
    "DYED":    ("다이드",),
    "HEATHER": ("헤더",),
    "STONE":   ("스톤",),
    "EGGSHELL":("에그쉘",),
    "ASH":     ("애쉬",),
    "LIGHT":   ("라이트",),
    "DARK":    ("다크",),
    "DUSTY":   ("더스티",),
    "BURGUNDY": ("버건디",),
    "TEAL":    ("틸",),
    "SHADOW":  ("쉐도우", "섀도우"),
    "DUNE":    ("듄",),
    "MIDNIGHT":("미드나잇",),
    "VINTAGE": ("빈티지",),
    "FADED":   ("페이디드", "페이디"),
    "OFF":     ("오프",),
    "PALE":    ("페일",),
}


@dataclass
class StussyMatchStats:
    dumped: int = 0
    soldout_dropped: int = 0
    no_prefix: int = 0
    matched: int = 0
    matched_by_color: int = 0
    ambiguous_unresolved: int = 0
    collected_to_queue: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "no_prefix": self.no_prefix,
            "matched": self.matched,
            "matched_by_color": self.matched_by_color,
            "ambiguous_unresolved": self.ambiguous_unresolved,
            "collected_to_queue": self.collected_to_queue,
        }


def _build_url(handle: str) -> str:
    return f"{BASE_URL}/products/{handle}" if handle else BASE_URL


def _color_tokens_kr(english_color: str) -> set[str]:
    """영문 색상 문자열을 토큰화 → 한글 토큰 집합 반환."""
    if not english_color:
        return set()
    out: set[str] = set()
    for tok in re.split(r"[\s/_\-]+", english_color.upper()):
        if not tok:
            continue
        for kr in _COLOR_KO.get(tok, ()):
            out.add(kr)
    return out


class StussyAdapter:
    """Stussy 한국 공식몰 카탈로그 덤프 + 크림 DB 매칭."""

    source_name: str = "stussy"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        max_pages: int = 8,
        page_size: int = 250,
    ) -> None:
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._max_pages = max_pages
        self._page_size = page_size

    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        self._http = _DefaultStussyHttp()
        return self._http

    # --------------------------------------------------------------
    # 1) 카탈로그 덤프
    # --------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        http = await self._get_http()
        variants_out: list[dict] = []

        for page in range(1, self._max_pages + 1):
            try:
                products = await http.fetch_products_page(
                    page=page, limit=self._page_size
                )
            except Exception:
                logger.exception("[stussy] products.json 덤프 실패 page=%d", page)
                break
            if not products:
                break

            for prod in products:
                handle = (prod.get("handle") or "").strip()
                title = prod.get("title") or ""
                vendor = prod.get("vendor") or ""
                for v in prod.get("variants") or []:
                    sku = (v.get("sku") or "").strip().upper()
                    option1 = (v.get("option1") or "").strip()  # color
                    option2 = (v.get("option2") or "").strip()  # size
                    price_raw = v.get("price") or "0"
                    try:
                        price = int(float(price_raw))
                    except (TypeError, ValueError):
                        price = 0
                    variants_out.append({
                        "handle": handle,
                        "title": title,
                        "vendor": vendor,
                        "sku": sku,
                        "color": option1,
                        "size": option2,
                        "price": price,
                        "available": bool(v.get("available", False)),
                    })

            if len(products) < self._page_size:
                break

        event = CatalogDumped(
            source=self.source_name,
            product_count=len(variants_out),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[stussy] 카탈로그 덤프 완료: %d variants", len(variants_out))
        return event, variants_out

    # --------------------------------------------------------------
    # 2) 크림 DB 인덱스 — digit prefix 다중 인덱스
    # --------------------------------------------------------------
    def _load_kream_prefix_index(self) -> dict[str, list[dict]]:
        """크림 Stussy 행을 digit prefix 키로 다중 인덱스."""
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number "
                "FROM kream_products "
                "WHERE brand = 'Stussy' AND model_number != ''"
            ).fetchall()
        finally:
            conn.close()
        index: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            mn = (row["model_number"] or "").strip()
            m = _KREAM_DIGIT_PREFIX_RE.match(mn)
            if not m:
                continue
            index[m.group(1)].append(dict(row))
        return index

    # --------------------------------------------------------------
    # 3) 매칭
    # --------------------------------------------------------------
    async def match_to_kream(
        self, variants: list[dict]
    ) -> tuple[list[CandidateMatched], StussyMatchStats]:
        stats = StussyMatchStats(dumped=len(variants))
        kream_index = self._load_kream_prefix_index()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []
        seen_skus: set[str] = set()

        for item in variants:
            if not item.get("available", False):
                stats.soldout_dropped += 1
                continue

            sku = (item.get("sku") or "").strip().upper()
            m = _STUSSY_SKU_PREFIX_RE.match(sku)
            if not m:
                stats.no_prefix += 1
                continue
            prefix = m.group(1)

            # variant 단위 dedup (SKU + 동일 prefix 만 중복 매칭 방지)
            if sku in seen_skus:
                continue
            seen_skus.add(sku)

            candidates = kream_index.get(prefix)
            if not candidates:
                pending_collect.append(self._build_collect_row(item, prefix))
                continue

            chosen: dict | None = None
            matched_via_color = False
            if len(candidates) == 1:
                chosen = candidates[0]
            else:
                # 색상 disambiguation
                color_tokens = _color_tokens_kr(item.get("color") or "")
                if color_tokens:
                    color_hits = [
                        c for c in candidates
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
                model_no=normalize_model_number(prefix),
                retail_price=int(item.get("price") or 0),
                size=item.get("size") or "",
                url=_build_url(item.get("handle") or ""),
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1
            if matched_via_color:
                stats.matched_by_color += 1

        if pending_collect:
            try:
                inserted = enqueue_collect_batch(self._db_path, pending_collect)
                stats.collected_to_queue += inserted
            except Exception:
                logger.warning(
                    "[stussy] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[stussy] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    def _build_collect_row(
        self, item: dict, prefix: str
    ) -> tuple[str, str, str, str, str]:
        return (
            normalize_model_number(prefix),
            "Stussy",
            item.get("title") or "",
            self.source_name,
            _build_url(item.get("handle") or ""),
        )

    async def run_once(self) -> dict[str, int]:
        _, variants = await self.dump_catalog()
        _, stats = await self.match_to_kream(variants)
        return stats.as_dict()


class _DefaultStussyHttp:
    """기본 HTTP 레이어 — Shopify products.json 페이지네이션."""

    _HEADERS = {
        "Accept": "application/json",
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
                timeout=15.0,
                follow_redirects=True,
            )
        return self._client

    async def fetch_products_page(
        self, page: int, limit: int = 250
    ) -> list[dict]:
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{BASE_URL}/products.json",
                params={"limit": limit, "page": page},
            )
        except httpx.HTTPError as e:
            logger.warning("[stussy] products.json HTTP 오류 page=%d: %s", page, e)
            return []
        if resp.status_code != 200:
            logger.warning(
                "[stussy] products.json HTTP %d page=%d", resp.status_code, page
            )
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return data.get("products") or []


__all__ = ["StussyAdapter", "StussyMatchStats", "BASE_URL"]
