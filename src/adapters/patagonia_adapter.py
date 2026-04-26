"""파타고니아 코리아 푸시 어댑터 (Phase 3 배치 6).

설계 원칙
----------
* 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
* HTTP 레이어는 외부 주입. 기본값은 `_DefaultPatagoniaHttp` (싱글톤
  크롤러 재사용). 테스트에서는 `fetch_catalog` 를 제공하는 mock 주입.
* 매칭 가드(콜라보/서브타입) 는 `src.core.matching_guards` 재사용.
* 크림 실호출 금지 — 로컬 SQLite 만 조회.

매칭 전략
----------
파타고니아 크림 DB 는 **5자리 스타일 코드**(예: ``24142``)로 저장되고,
한 크림 상품에 복수 코드(``85240/85241``)가 들어 있는 경우도 있다.
사이트 pcode(``44937R5``)에서 앞 5자리(``44937``)만 스타일 키로 뽑아
크림 DB 의 분리된 토큰 각각과 대조한다 (``_build_kream_index``).

사이즈는 풀 덤프 단계에서 옵션까지 내려오므로, 최초 매칭에서
사이즈별 재고 리스트까지 확보 가능. 현재는 사이즈 0 (리스팅 단계와
동일) 로 전파하고, 사이즈별 분해는 하류 소비자(오케스트레이터)에서
필요 시 재구성.
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from src.adapters._collect_queue import aenqueue_collect_batch
from src.core.db import sync_connect
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number

logger = logging.getLogger(__name__)


def _keyword_set(text: str) -> set[str]:
    """매칭 가드용 소문자 키워드 집합."""
    if not text:
        return set()
    return {tok for tok in re.split(r"[\s\-_/()]+", text.lower()) if tok}


# ----------------------------------------------------------------------
# 색상 사전 — 사이트 tooltip 영문 풀네임 (정규화된 대문자) → 크림 한글 색상 토큰
# ----------------------------------------------------------------------
# 정확성 1순위 원칙: 미등록 색상은 매칭 보류 (오매칭보다 미발행이 안전).
# 사전은 점진적 확장. 84702 14색상 + patagonia.co.kr 845건 빈출 50종 시작.
_PATA_COLOR_KO: dict[str, tuple[str, ...]] = {
    # 84702 (Down Sweater Hoody) 14색상
    "BLACK":              ("블랙",),
    "FORGE GREY":         ("포지 그레이", "포지그레이"),
    "PLUMMET PURPLE":     ("플럼멧 퍼플", "플럼퍼플"),
    "CASCADE GREEN":      ("케스케이드 그린",),
    "AMANITA RED":        ("아마니타 레드",),
    "SEABIRD GREY":       ("씨버드 그레이",),
    "NEW NAVY":           ("뉴 네이비",),
    "REDTAIL RUST":       ("레드테일 러스트",),
    "BASIN GREEN":        ("베이슨 그린",),
    "PINON GREEN":        ("피논 그린",),
    "CLEMENT BLUE":       ("클레멘트 블루",),
    "PINE NEEDLE GREEN":  ("파인 니들 그린",),
    "PASSAGE BLUE":       ("패시지 블루",),
    # patagonia 845건 빈출 추가
    "WHITE":              ("화이트",),
    "INK BLACK":          ("잉크 블랙",),
    "SMOLDER BLUE":       ("스몰더 블루",),
    "BLUE SAGE":          ("블루 세이지",),
    "WEATHERED STONE":    ("웨더드 스톤",),
    "UNDYED NATURAL":     ("언다이드 내추럴",),
    "BIRCH WHITE":        ("버치 화이트",),
    "WETLAND BLUE":       ("웨트랜드 블루",),
    "QUIET VIOLET":       ("콰이엇 바이올렛",),
    "MARLOW BROWN":       ("말로우 브라운",),
    "THIN ICE":           ("띤 아이스", "씬 아이스"),
    "RIVER ROCK GREEN":   ("리버 락 그린",),
    "ELLWOOD GREEN":      ("엘우드 그린",),
    "NOBLE GREY":         ("노블 그레이",),
    "BARNACLE BLUE":      ("바너클 블루",),
    "GUMTREE GREEN":      ("검트리 그린",),
    "THERMAL BLUE":       ("써멀 블루",),
    "WING GREY":          ("윙 그레이",),
    "CURRENT BLUE":       ("커런트 블루",),
    "AQUA STONE":         ("아쿠아 스톤",),
    "GRAZE GREEN":        ("그레이즈 그린",),
    "TENT GREEN":         ("텐트 그린",),
    "TIDEPOOL BLUE":      ("타이드풀 블루",),
    "BRISK PURPLE":       ("브리스크 퍼플",),
    "SUNKEN BLUE":        ("선큰 블루",),
    "WOOL WHITE":         ("울 화이트",),
    "MOMENT PINK":        ("모멘트 핑크",),
    "NATURAL":            ("내추럴", "내츄럴"),
    "DYNO WHITE":         ("다이노 화이트",),
    "PELICAN":            ("펠리칸",),
    "SHORE BLUE":         ("쇼어 블루",),
    "PEACH SHERBET":      ("피치 셔벗",),
    "OLD GROWTH GREEN":   ("올드 그로스 그린",),
    "DRIED VANILLA":      ("드라이드 바닐라",),
    "ORANGE PEEL":        ("오렌지 필",),
    "FEATHER GREY":       ("페더 그레이",),
    "LIGHT VIOLET":       ("라이트 바이올렛",),
    "HEIRLOOM PEACH":     ("헤어룸 피치",),
    "BUCKHORN GREEN":     ("벅혼 그린",),
    "DARK NATURAL":       ("다크 내추럴",),
    "TALON GOLD":         ("탤런 골드",),
    "BOBCAT BROWN":       ("밥캣 브라운",),
}


# tooltip 정규화 — 색상 사전 lookup 키 만들기
# - HTML escape 복원: &apos; → '
# - "X w/Y" (양면 색상) → "X" 만 사용
# - "디자인명: 색상명" (콜라보 패턴) → 콜론 뒤 색상만
# - 대문자화 + 양끝 공백 제거
_W_SLASH_RE = re.compile(r"\s+w/.*$", re.IGNORECASE)


def _normalize_color_tooltip(tooltip: str) -> str:
    """사이트 tooltip → 사전 lookup 정규화 키.

    HTML entities (`&apos;`, `&amp;`, `&quot;` 등) 일괄 복원 후 콜라보 디자인
    prefix(콜론 앞), 양면 색상 표기(``X w/Y``) 절단. 정확성 1순위 정책상
    표준 `html.unescape()` 사용 (`&apos;` 단일 처리는 `&amp;` 등 누락 위험).
    """
    if not tooltip:
        return ""
    s = html.unescape(tooltip).strip()
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    s = _W_SLASH_RE.sub("", s).strip()
    return s.upper()


def _color_tokens_for(tooltip: str) -> tuple[str, ...]:
    """사이트 tooltip → 크림 한글 색상 토큰 tuple. 미등록은 빈 tuple."""
    key = _normalize_color_tooltip(tooltip)
    return _PATA_COLOR_KO.get(key, ())


# 한글 색상 토큰 vocabulary (사전 모든 한글 값 평탄화) — single 매칭 시
# 크림 name 끝부분에서 색상 추출용.
_KR_COLOR_VOCAB: frozenset[str] = frozenset(
    kr for variants in _PATA_COLOR_KO.values() for kr in variants
)


def _extract_color_suffix(kream_name: str) -> str:
    """크림 상품명에서 색상 한글 부분 추출 (single 매칭 시 알림 표기용).

    예: "파타고니아 다운 스웨터 후디 포지 그레이" → "포지 그레이".
    사전 vocabulary 가운데 가장 긴 매칭 우선 (다중 토큰 정확 매칭).
    """
    if not kream_name:
        return ""
    # 가장 긴 토큰부터 검사 (포지그레이 같은 공백 없는 변형도 매칭)
    for kr in sorted(_KR_COLOR_VOCAB, key=len, reverse=True):
        if kream_name.endswith(kr):
            return kr
    return ""


@dataclass
class PatagoniaMatchStats:
    """매칭 파이프라인 통계."""

    dumped: int = 0
    soldout_dropped: int = 0
    no_model_number: int = 0
    matched: int = 0
    matched_by_color: int = 0
    ambiguous_color_unresolved: int = 0
    unknown_color_code: int = 0
    collected_to_queue: int = 0
    skipped_guard: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dumped": self.dumped,
            "soldout_dropped": self.soldout_dropped,
            "no_model_number": self.no_model_number,
            "matched": self.matched,
            "matched_by_color": self.matched_by_color,
            "ambiguous_color_unresolved": self.ambiguous_color_unresolved,
            "unknown_color_code": self.unknown_color_code,
            "collected_to_queue": self.collected_to_queue,
            "skipped_guard": self.skipped_guard,
        }


class PatagoniaAdapter:
    """파타고니아 카탈로그 덤프 + 크림 DB 매칭 + 이벤트 발행 어댑터."""

    source_name: str = "patagonia"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        http_client: Any = None,
        *,
        categories: tuple[str, ...] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. `CatalogDumped`·`CandidateMatched` 를 publish.
        db_path:
            크림 DB SQLite 경로. `kream_products`·`kream_collect_queue`
            테이블 조회·적재.
        http_client:
            HTTP 레이어. `fetch_catalog(categories: tuple[str,...]|None)
            -> list[dict]` 메서드만 제공하면 된다. 기본값 None →
            내부 싱글톤 `patagonia_crawler` 재사용.
        categories:
            덤프할 카테고리 cid 튜플. 기본 None → 크롤러 기본값.
        """
        self._bus = bus
        self._db_path = db_path
        self._http = http_client
        self._categories = categories

    # ------------------------------------------------------------------
    # HTTP 레이어 — 지연 로드 (테스트 시 실모듈 import 회피 가능)
    # ------------------------------------------------------------------
    async def _get_http(self) -> Any:
        if self._http is not None:
            return self._http
        from src.crawlers.patagonia import patagonia_crawler

        self._http = _DefaultPatagoniaHttp(patagonia_crawler)
        return self._http

    # ------------------------------------------------------------------
    # 1) 카탈로그 덤프
    # ------------------------------------------------------------------
    async def dump_catalog(self) -> tuple[CatalogDumped, list[dict]]:
        """전체 카탈로그 덤프 → CatalogDumped publish."""
        http = await self._get_http()
        try:
            catalog = await http.fetch_catalog(self._categories)
        except Exception:
            logger.exception("[patagonia] 카탈로그 덤프 실패")
            catalog = []

        catalog = list(catalog or [])
        event = CatalogDumped(
            source=self.source_name,
            product_count=len(catalog),
            dumped_at=time.time(),
        )
        await self._bus.publish(event)
        logger.info("[patagonia] 카탈로그 덤프 완료: %d건", len(catalog))
        return event, catalog

    # ------------------------------------------------------------------
    # 2) 크림 DB 매칭
    # ------------------------------------------------------------------
    def _load_kream_index_grouped(self) -> dict[str, list[dict]]:
        """크림 DB → {5자리 style_code: [색상별 row, ...]}.

        한 크림 상품에 복수 코드(``85240/85241``)가 있는 경우 각 코드를
        인덱스에 등록한다. 같은 style_code 에 다수 색상 row 가 있으면
        모두 유지 (이전 setdefault 단일 등록 버그 — 84702 14개 색상 중
        첫 등록만 남던 오매칭 원인 제거).
        """
        from src.crawlers.patagonia import split_kream_model_numbers

        with sync_connect(self._db_path, read_only=True) as conn:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number "
                "FROM kream_products WHERE model_number != ''"
            ).fetchall()

        index: dict[str, list[dict]] = {}
        for row in rows:
            codes = split_kream_model_numbers(row["model_number"] or "")
            if not codes:
                continue
            drow = dict(row)
            for code in codes:
                index.setdefault(code, []).append(drow)
        return index

    # 색상 매칭 휴리스틱 — 시즌 중복 등록 (동일 색상 한글명 N개 row) 케이스
    # 보존을 위해 hits ≤ AMBIGUOUS_THRESHOLD 면 모두 발행. dedup_key 가
    # (kream_product_id, ...) 라 자연 분리됨. 임계 초과는 ambiguous 보류
    # (정확성 1순위).
    AMBIGUOUS_THRESHOLD: int = 2

    @staticmethod
    def _resolve_color_rows(
        candidates: list[dict],
        color_tooltip: str,
    ) -> tuple[list[dict], str, str]:
        """style 매칭 후 색상별 정확한 row 리스트.

        Returns
        -------
        (rows, color_kr, status) — status 는 "exact"/"single"/"ambiguous"/"unknown".
        - rows=[] 이면 매칭 보류 (ambiguous 또는 unknown).
        - rows 길이 ≥ 1 이면 모두 발행 (시즌 중복 케이스 1~AMBIGUOUS_THRESHOLD).
        - color_kr 은 알림 표기용 (한글 색상명, 미정 시 "").
        """
        if not candidates:
            return [], "", "unknown"

        # 후보 1개면 색상 해석 불필요 — 단일 매칭
        if len(candidates) == 1:
            return [candidates[0]], "", "single"

        # 사이트 tooltip → 한글 토큰 변환
        kr_tokens = _color_tokens_for(color_tooltip)
        if not kr_tokens:
            # 사전 미등록 — 정확성 1순위로 매칭 보류
            return [], "", "unknown"

        # 부분일치 — 어떤 토큰이 크림 name 에 포함되는 row 만 후보
        hits: list[dict] = [
            row
            for row in candidates
            if any(tok in (row.get("name") or "") for tok in kr_tokens)
        ]
        if not hits:
            return [], "", "unknown"
        if len(hits) <= PatagoniaAdapter.AMBIGUOUS_THRESHOLD:
            return hits, kr_tokens[0], "exact"
        return [], "", "ambiguous"

    def _build_collect_row(
        self, item: dict, style_code: str
    ) -> tuple[str, str, str, str, str]:
        """미등재 신상 → batch flush 용 row 튜플."""
        return (
            normalize_model_number(style_code),
            "Patagonia",
            item.get("name") or item.get("name_kr") or "",
            self.source_name,
            item.get("url") or "",
        )

    @staticmethod
    def _group_sizes_by_color(item: dict) -> dict[str, list[dict]]:
        """item.sizes ([{size, color, in_stock, stock}, ...]) → {color_code: [size_row, ...]}.

        crawler 의 parse_sizes_from_options 가 size 행마다 color 코드를
        보존하고 있어 그대로 그룹화. 색상 코드 없는 size 는 빈 키 ""로 묶임.
        """
        groups: dict[str, list[dict]] = {}
        for s in item.get("sizes") or []:
            if not isinstance(s, dict):
                continue
            code = (s.get("color") or "").strip().upper()
            groups.setdefault(code, []).append(s)
        return groups

    async def match_to_kream(
        self, catalog: list[dict]
    ) -> tuple[list[CandidateMatched], PatagoniaMatchStats]:
        """덤프된 아이템 → 크림 DB 매칭 → CandidateMatched publish.

        색상별 fan-out: 사이트 product 1개 안의 색상 variant 마다 (1) 색상별
        in-stock 사이즈 추출 (2) `_resolve_color_row` 로 정확한 kream row 선택
        (3) `CandidateMatched` 별개 발행. 매칭 보류(ambiguous/unknown) 색상은
        발행하지 않음 — 정확성 1순위.
        """
        stats = PatagoniaMatchStats(dumped=len(catalog))
        kream_index = self._load_kream_index_grouped()
        matched: list[CandidateMatched] = []
        pending_collect: list[tuple[str, str, str, str, str]] = []
        seen_styles_for_collect: set[str] = set()

        for item in catalog:
            if item.get("is_sold_out"):
                stats.soldout_dropped += 1
                continue

            style_code = (item.get("style_code") or "").strip().upper()
            if not style_code:
                stats.no_model_number += 1
                continue

            # 덤프 ledger — 매칭 전 전수 기록 (오프라인 분석용)
            try:
                from src.core.dump_ledger import record_dump_item
                await record_dump_item(
                    self._db_path,
                    source=self.source_name,
                    model_no=style_code,
                    name=item.get("name") or item.get("name_kr") or "",
                    url=item.get("url") or "",
                )
            except Exception:
                logger.debug("[patagonia] dump_ledger 실패 (비치명)")

            kream_candidates = kream_index.get(style_code)
            if not kream_candidates:
                # collect 큐는 style 단위 1회만
                if style_code not in seen_styles_for_collect:
                    seen_styles_for_collect.add(style_code)
                    pending_collect.append(self._build_collect_row(item, style_code))
                continue

            # 매칭 가드 — 크림 이름 vs 소싱 이름 키워드 비교 (color 무관)
            source_name_text = item.get("name") or item.get("name_kr") or ""
            # 가드 적용 시 첫 row 의 name 만 사용 (style 동일 → 모델명 동일 가정)
            kream_name = kream_candidates[0].get("name") or ""
            if collab_match_fails(kream_name, source_name_text):
                logger.info(
                    "[patagonia] 콜라보 가드 차단: kream=%r source=%r",
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
                    "[patagonia] 서브타입 가드 차단: source=%r extra=%s",
                    source_name_text[:40],
                    stype_diff,
                )
                stats.skipped_guard += 1
                continue

            # 색상별 사이즈 그룹화
            sizes_by_color = self._group_sizes_by_color(item)
            color_variants = item.get("color_variants") or []
            # color_code → tooltip lookup (메타에서)
            tooltip_by_code: dict[str, str] = {
                str(cv.get("code") or "").strip().upper(): str(cv.get("tooltip") or "")
                for cv in color_variants
                if isinstance(cv, dict) and cv.get("code")
            }

            price = int(item.get("price") or 0)

            for color_code, size_rows in sizes_by_color.items():
                # in-stock 사이즈만 추출
                available_sizes: tuple[str, ...] = tuple(
                    str(s.get("size") or "").strip()
                    for s in size_rows
                    if s.get("in_stock") and (s.get("size") or "").strip()
                )
                if not available_sizes:
                    continue  # 색상 단위 품절 — 발행 X (전체 dropped 통계는 product 단위로만)

                # 색상 → kream row 정확한 선택 (시즌 중복 시 ≤2개 모두 발행)
                tooltip = tooltip_by_code.get(color_code, "")
                rows, color_kr, status = self._resolve_color_rows(kream_candidates, tooltip)
                if status == "ambiguous":
                    stats.ambiguous_color_unresolved += 1
                    logger.info(
                        "[patagonia] 색상 ambiguous 보류 style=%s color=%s "
                        "tooltip=%r candidates=%d",
                        style_code, color_code, tooltip, len(kream_candidates),
                    )
                    continue
                if status == "unknown" or not rows:
                    stats.unknown_color_code += 1
                    logger.info(
                        "[patagonia] 색상 사전 미등록 보류 style=%s color=%s tooltip=%r",
                        style_code, color_code, tooltip,
                    )
                    continue

                # color_name 결정: exact 일 때 한글, single 일 때 row.name 끝에서 추출
                for row in rows:
                    try:
                        kream_product_id = int(row["product_id"])
                    except (TypeError, ValueError):
                        logger.warning(
                            "[patagonia] 비정수 kream_product_id 스킵: %r",
                            row.get("product_id"),
                        )
                        stats.skipped_guard += 1
                        continue

                    if color_kr:
                        name_for_alert = color_kr
                    else:
                        name_for_alert = _extract_color_suffix(row.get("name") or "")

                    candidate = CandidateMatched(
                        source=self.source_name,
                        kream_product_id=kream_product_id,
                        model_no=normalize_model_number(style_code),
                        retail_price=price,
                        size="",
                        url=item.get("url") or "",
                        available_sizes=available_sizes,
                        color_name=name_for_alert,
                    )
                    await self._bus.publish(candidate)
                    matched.append(candidate)
                    stats.matched += 1
                    if status == "exact":
                        stats.matched_by_color += 1

        if pending_collect:
            try:
                inserted = await aenqueue_collect_batch(self._db_path, pending_collect)
                stats.collected_to_queue += inserted
            except Exception:
                logger.warning(
                    "[patagonia] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[patagonia] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    # ------------------------------------------------------------------
    # 3) 단발 사이클 — dump → match
    # ------------------------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """덤프 + 매칭 한 사이클. 통계 dict 반환."""
        _, catalog = await self.dump_catalog()
        _, stats = await self.match_to_kream(catalog)
        return stats.as_dict()


class _DefaultPatagoniaHttp:
    """기본 덤퍼 — 기존 `patagonia_crawler` 싱글톤을 재사용."""

    def __init__(self, crawler: Any) -> None:
        self._crawler = crawler

    async def fetch_catalog(
        self, categories: tuple[str, ...] | None
    ) -> list[dict]:
        return await self._crawler.fetch_catalog(categories)


__all__ = [
    "PatagoniaAdapter",
    "PatagoniaMatchStats",
    "_PATA_COLOR_KO",
    "_color_tokens_for",
    "_extract_color_suffix",
    "_normalize_color_tooltip",
]
