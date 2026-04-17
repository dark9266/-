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

from src.adapters._collect_queue import aenqueue_collect_batch
from src.adapters._size_helpers import fetch_in_stock_sizes
from src.core.event_bus import CandidateMatched, CatalogDumped, EventBus
from src.core.matching_guards import collab_match_fails, subtype_mismatch
from src.matcher import normalize_model_number
from src.models.product import RetailProduct, RetailSizeInfo
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
    return f"{WEB_BASE}/products/detail/{product_id}"


# 한글 색상 → 영문 매핑 (크림 상품명에서 색상 추출용)
_KR_COLOR_MAP: dict[str, str] = {
    "블랙": "black", "화이트": "white", "블루": "blue", "레드": "red",
    "그린": "green", "네이비": "navy", "그레이": "grey", "베이지": "beige",
    "브라운": "brown", "옐로": "yellow", "핑크": "pink", "퍼플": "purple",
    "오렌지": "orange", "카키": "khaki", "아이보리": "ivory",
    "보이드": "void", "일렉트라": "electra", "다이너스티": "dynasty",
    "솔리튜드": "solitude", "소울소닉": "soulsonic", "블레이즈": "blaze",
    "모스": "moss", "문스톤": "moonstone", "캔버스": "canvas",
    "레벨": "revel", "루미나": "lumina", "메스머": "mesmer",
    "바이탈리티": "vitality", "스트라터스": "stratus", "아트모스": "atmos",
    "솔라라이즈": "solarize", "솔레스": "solace", "유콘": "yukon",
    "포리지": "forage", "세쿼이아": "sequoia", "에덴": "eden",
    "타츠": "tatsu", "위커": "wicker", "데이즈": "daze",
    "데이브레이크": "daybreak", "헤드워터스": "headwaters",
    "스포트라이트": "spotlight", "알펜글로우": "alpenglow",
    "포스포레센트": "phosphorescent", "로데스타": "lodestar",
}

# 아크테릭스 제품명에서 색상 부분을 추출할 때 제거할 비색상 토큰
_NON_COLOR_TOKENS: set[str] = {
    "아크테릭스", "arcteryx", "arc'teryx", "베타", "beta", "알파", "alpha",
    "감마", "gamma", "아톰", "atom", "세륨", "cerium", "솔라노", "solano",
    "자켓", "jacket", "재킷", "후디", "hoody", "hoodie", "팬츠", "pants",
    "쇼츠", "shorts", "조끼", "vest", "티셔츠", "tee", "shirt",
    "남성", "여성", "men", "women", "남자", "여자", "sl", "sv", "ar", "lt",
    "인센도", "incendo", "크래그", "crag", "맨티스", "mantis",
    "코튼", "cotton", "로고", "logo", "ss", "ls", "에어쉘", "airshell",
    "ePE", "epe", "x", "빔즈", "beams", "버드", "bird", "워드", "word",
    "9인치", "코어", "core", "pro", "프로", "인치", "inch",
    "w", "m", "배색", "반팔", "긴팔", "SL", "SV", "AR", "LT",
}


# 복합 한글 색상 → 영문 매핑 (2단어 이상, 단일보다 먼저 매칭)
_KR_COMPOUND_COLOR_MAP: dict[str, tuple[str, ...]] = {
    "블랙 사파이어": ("black", "sapphire"),
    "알파인 블루": ("alpine", "blue"),
    "알파인 로즈": ("alpine", "rose"),
    "다크 신차": ("dk", "shincha"),
    "핑크 글로우": ("pink", "glow"),
    "포리지 타츠": ("forage", "tatsu"),
    "캔버스 포리지": ("canvas", "forage"),
}


def _extract_color_tokens(kream_name: str) -> set[str]:
    """크림 상품명에서 색상 관련 토큰만 추출.

    1차: 복합 한글 색상(2단어+) 우선 매칭 → 영문 변환
    2차: 단일 한글 색상 → 영문 변환
    3차: 영문 토큰 중 비색상 토큰 제거
    """
    name_lower = kream_name.lower()
    tokens: set[str] = set()
    consumed: set[str] = set()
    # 복합 색상 우선
    for kr, en_tuple in _KR_COMPOUND_COLOR_MAP.items():
        if kr in name_lower:
            tokens.update(en_tuple)
            # 복합 색상의 개별 한글 파트를 consumed 처리하여 단일 매칭 방지
            for part in kr.split():
                consumed.add(part)
    # 단일 색상
    for kr, en in _KR_COLOR_MAP.items():
        if kr in name_lower and kr not in consumed:
            tokens.add(en)
    # 영문 토큰 추가 (비색상 필터)
    raw = set(re.findall(r"[a-z]+", name_lower))
    non_lower = {t.lower() for t in _NON_COLOR_TOKENS}
    tokens.update(raw - non_lower)
    return tokens


def _match_color_to_kream(
    kream_name: str, color_values: list[dict],
) -> int | None:
    """크림 상품명에서 색상 토큰을 추출하고 arcteryx 색상과 매칭.

    Returns 매칭된 color value 의 id, 없으면 None.
    동점 시 토큰 수가 적은(더 구체적인) 색상 우선.
    """
    kream_tokens = _extract_color_tokens(kream_name)

    best_id: int | None = None
    best_overlap = 0
    best_arc_len = 999  # 동점 시 토큰 수 적은 것 우선
    for cv in color_values:
        if not isinstance(cv, dict):
            continue
        arc_color = str(cv.get("value") or "").lower()
        arc_tokens = set(re.split(r"[\s/]+", arc_color)) - {""}
        overlap = len(kream_tokens & arc_tokens)
        if overlap > best_overlap or (
            overlap == best_overlap and overlap > 0 and len(arc_tokens) < best_arc_len
        ):
            best_overlap = overlap
            best_id = cv.get("id")
            best_arc_len = len(arc_tokens)
    return best_id if best_overlap >= 1 else None


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
        # product_id → model_no 인메모리 캐시. 리스팅 응답에 모델번호가 없어
        # 상품당 options API 1회 호출이 강제되는데(~2s/req), 2회차 사이클부터는
        # 동일 상품이 대부분이라 캐시만 있으면 HTTP 0건으로 끝난다.
        self._model_cache: dict[Any, str] = {}

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

    def _load_kream_style_index(self) -> dict[str, dict]:
        """크림 Arc'teryx 엔트리의 슬래시/쉼표 분리 style number 인덱스.

        크림 DB 는 Arc'teryx 상품의 model_number 를 style number 들을
        슬래시로 잇는 조합(예: ``28412/6057/9829/10358/10403``) 으로 저장한다.
        각 청크는 아크테릭스 ERP SKU 뒤 5자리와 일치하므로(`ABQSU10358` →
        `10358`), style 단위 인덱스를 만들어 SKU 기반 역참조에 쓴다.
        """
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number "
                "FROM kream_products "
                "WHERE brand LIKE '%arc%' OR brand LIKE '%아크%'"
            ).fetchall()
        finally:
            conn.close()
        style_index: dict[str, dict] = {}
        for row in rows:
            mn = row["model_number"] or ""
            for chunk in re.split(r"[/,;]", mn):
                chunk = chunk.strip()
                if chunk.isdigit():
                    style_index.setdefault(chunk, dict(row))
        return style_index

    @staticmethod
    def _extract_style_from_sku(sku: str) -> str:
        """ERP SKU(예: ``ABQSU10358``) → 트레일링 숫자 style(``10358``).

        아크테릭스 공식몰 SKU 는 ``[A-Z]+\\d{4,5}`` 형태. 선행 0 이 있으면
        제거해 크림 style 인덱스 키와 맞춘다.
        """
        if not sku:
            return ""
        m = re.match(r"^[A-Z]+(\d{4,6})$", sku.strip().upper())
        if not m:
            return ""
        try:
            return str(int(m.group(1)))
        except ValueError:
            return ""

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
        cached = self._model_cache.get(pid)
        if cached is not None:
            return cached
        try:
            data = await http._options_raw(product_id=pid)
        except Exception:
            logger.exception("[arcteryx] 옵션 조회 실패: id=%s", pid)
            self._model_cache[pid] = ""
            return ""
        if not isinstance(data, dict):
            self._model_cache[pid] = ""
            return ""
        model = _extract_model_from_options(data)
        self._model_cache[pid] = model
        return model

    async def match_to_kream(
        self, products: list[dict]
    ) -> tuple[list[CandidateMatched], ArcteryxMatchStats]:
        """덤프된 아이템 → 크림 DB 매칭 → CandidateMatched publish."""
        stats = ArcteryxMatchStats(dumped=len(products))
        kream_index = self._load_kream_index()
        kream_style_index = self._load_kream_style_index()
        matched: list[CandidateMatched] = []
        http = await self._get_http()
        pending_collect: list[tuple[str, str, str, str, str]] = []

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

            # 1차: stripped key 직접 매칭 (크림이 ERP SKU 를 그대로 쓰는 극소수
            # 경우 대비). 2차: 트레일링 style number 역참조 — 크림 Arc'teryx
            # 엔트리의 슬래시 조합과 exact intersect.
            kream_row = kream_index.get(key)
            if kream_row is None:
                style_no = self._extract_style_from_sku(model_no)
                if style_no:
                    kream_row = kream_style_index.get(style_no)
            if kream_row is None:
                # 미등재 신상 → 배치 버퍼에 쌓고 사이클 끝에서 한 번에 flush.
                # 행 단위 INSERT 가 19개 어댑터 동시 쓰기에서 DB 락을 유발하는
                # 문제를 완화.
                pid = item.get("product_id")
                pending_collect.append((
                    normalize_model_number(model_no),
                    "Arc'teryx",
                    item.get("product_name") or "",
                    self.source_name,
                    _build_url(pid) if pid is not None else "",
                ))
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

            # PDP 실재고 사이즈 — 색상별 필터 (크로스컬러 거짓양성 방지)
            http = await self._get_http()
            available_sizes = await self._fetch_color_aware_sizes(
                http, str(pid or ""), kream_name,
            )
            if not available_sizes:
                logger.info(
                    "[arcteryx] PDP 재고 없음 drop: pid=%s model=%s",
                    pid, model_no,
                )
                stats.soldout_dropped += 1
                continue

            candidate = CandidateMatched(
                source=self.source_name,
                kream_product_id=kream_product_id,
                model_no=normalize_model_number(model_no),
                retail_price=price,
                size="",  # 리스팅 단계엔 사이즈 정보 없음 — 수익 consumer 가 보강
                url=url,
                available_sizes=available_sizes,
            )
            await self._bus.publish(candidate)
            matched.append(candidate)
            stats.matched += 1

        # 사이클 끝 — 미등재 신상을 한 번에 flush. DB 락 경합 최소화.
        if pending_collect:
            try:
                inserted = await aenqueue_collect_batch(self._db_path, pending_collect)
                stats.collected_to_queue += inserted
            except Exception:
                logger.warning(
                    "[arcteryx] collect_queue 배치 flush 실패: n=%d",
                    len(pending_collect),
                )

        logger.info("[arcteryx] 매칭 완료: %s", stats.as_dict())
        return matched, stats

    async def _fetch_color_aware_sizes(
        self,
        http: Any,
        product_id: str,
        kream_name: str,
    ) -> tuple[str, ...]:
        """색상 인식 사이즈 조회 — 크림 상품명의 색상과 매칭되는 arcteryx 색상만.

        크로스컬러 거짓양성 방지: arcteryx 1개 상품 = 크림 N개 색상별 상품.
        크림 "24K 블랙"에 SOULSONIC 색상의 재고가 섞이는 것을 차단.
        """
        if not product_id:
            return ()
        try:
            data = await http._options_raw(product_id=product_id)
        except Exception as exc:
            logger.warning("[arcteryx] 옵션 조회 실패 pid=%s: %s", product_id, exc)
            return ()
        if not data:
            return ()

        options = data.get("options") or []

        # Level 1: 색상 목록 + 크림 이름 매칭
        color_values: list[dict] = []
        for opt in options:
            if opt.get("level") == 1:
                color_values = opt.get("values") or []
                break

        target_color_id: int | None = None
        if color_values:
            target_color_id = _match_color_to_kream(kream_name, color_values)
            if target_color_id is not None:
                # 매칭된 색상이 SOLDOUT 이면 → 전체 drop
                for cv in color_values:
                    if cv.get("id") == target_color_id:
                        if cv.get("sale_state") != "ON":
                            logger.info(
                                "[arcteryx] 색상 SOLDOUT drop: pid=%s color=%s kream=%s",
                                product_id, cv.get("value"), kream_name[:30],
                            )
                            return ()
                        break

        # Level 2: 타겟 색상의 사이즈만 수집
        sizes: list[str] = []
        seen: set[str] = set()
        for opt in options:
            if opt.get("level") != 2:
                continue
            for val in opt.get("values") or []:
                if not isinstance(val, dict):
                    continue
                size_val = str(val.get("value") or "").strip()
                if not size_val or size_val in seen:
                    continue
                # 색상 필터: target_color_id 가 있으면 해당 색상만
                if target_color_id is not None:
                    pids = val.get("parent_ids") or []
                    if target_color_id not in pids:
                        continue
                in_stock = (
                    val.get("sale_state") == "ON"
                    and bool(val.get("is_orderable", False))
                    and int(val.get("stock") or 0) > 0
                )
                if not in_stock:
                    continue
                seen.add(size_val)
                sizes.append(size_val)

        return tuple(sizes)

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

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """PDP 사이즈 조회 — options API → 재고 있는 사이즈 RetailProduct 반환.

        멀티컬러 상품에서 SOLDOUT 색상의 사이즈를 제외한다.
        Level 1 color 의 sale_state=ON 인 색상 ID 만 허용하고,
        Level 2 size 의 parent_ids 가 허용 색상을 포함할 때만 재고로 인정.
        """
        data = await self._options_raw(product_id=product_id)
        if not data:
            return None
        options = data.get("options") or []
        model_number = ""
        # Level 1: 판매 중인 색상 ID 수집
        active_color_ids: set[int] = set()
        for option in options:
            if option.get("level") == 1:
                model_number = option.get("code") or ""
                if not model_number:
                    values = option.get("values") or []
                    if values and isinstance(values[0], dict):
                        model_number = str(values[0].get("value") or "")
                for val in option.get("values") or []:
                    if isinstance(val, dict) and val.get("sale_state") == "ON":
                        cid = val.get("id")
                        if cid is not None:
                            active_color_ids.add(int(cid))
        # Level 2: 활성 색상에 속한 재고 사이즈만 수집
        sizes: list[RetailSizeInfo] = []
        seen_sizes: set[str] = set()
        for option in options:
            if option.get("level") != 2:
                continue
            for val in option.get("values") or []:
                if not isinstance(val, dict):
                    continue
                size_val = str(val.get("value") or "").strip()
                if not size_val or size_val in seen_sizes:
                    continue
                # parent_ids 로 색상 필터 — 활성 색상에 속하지 않으면 스킵
                pids = val.get("parent_ids") or []
                if active_color_ids and not any(
                    pid in active_color_ids for pid in pids
                ):
                    continue
                in_stock = (
                    val.get("sale_state") == "ON"
                    and bool(val.get("is_orderable", False))
                    and int(val.get("stock") or 0) > 0
                )
                if not in_stock:
                    continue
                sell_price = int(val.get("sell_price") or 0)
                seen_sizes.add(size_val)
                sizes.append(RetailSizeInfo(
                    size=size_val,
                    price=sell_price,
                    original_price=sell_price,
                    in_stock=True,
                ))
        if not sizes:
            return None
        return RetailProduct(
            source="arcteryx",
            product_id=product_id,
            name="",
            model_number=model_number,
            brand="Arc'teryx",
            url=_build_url(product_id),
            sizes=sizes,
        )


__all__ = [
    "ArcteryxAdapter",
    "ArcteryxMatchStats",
    "DEFAULT_CATEGORIES",
]
