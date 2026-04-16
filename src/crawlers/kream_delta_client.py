"""KreamDeltaClient — 델타 워처용 실크림 클라이언트 (Phase 3 파일럿).

목적:
    `src/adapters/kream_delta_watcher.py` 가 기대하는
    `KreamDeltaClientProtocol` (fetch_light / get_snapshot) 을
    실제 크림 API 호출로 구현.

설계 원칙:
    - 기존 `src.crawlers.kream.KreamCrawler` 는 건드리지 않는다.
      이 모듈은 **얇은 래퍼**로서 crawler 의 인증·세션·재시도·
      `kream_budget` 배선을 그대로 재사용한다.
    - 직접 HTTP 호출은 하지 않는다. 반드시 crawler 의 `_request`
      (또는 동등한 request_fn) 을 경유 → 일일 캡·계측 자동 통과.
    - 읽기 전용 GET 만 사용. POST/PUT/DELETE 금지.
    - 404 = 신규 상품 (거래량 0) → 예외 없이 빈 dict 로 처리.
    - 429 → crawler `_request` 가 내부적으로 재시도(최대 3회).
      여기서는 None 반환 시 빈 결과로 흡수.
    - `KreamBudgetExceeded` 는 그대로 위로 전파 → 어댑터가
      파이프라인 중단.

API 전략 (의사결정 근거):
    크림은 ID 리스트 기반 배치 조회 엔드포인트를 노출하지 않는다
    (`/api/p/e/search/products` 는 keyword 기반, ID 필터 없음).
    따라서 fetch_light 는 **개별 루프**가 현실적이며, 호출 수를
    최소화하기 위해:

    1) `fetch_light` → `/api/p/options/display?picker_type=selling`
       id 당 1회. buying 은 생략 (즉시판매가만 필요).
       호출 간 `rate_limit_sec` (기본 1.5초) 딜레이.
       → N 개 대상 → N 호출 (이전 hot watcher 대비 여전히 대폭 절감.
       델타 엔진이 변경된 것만 snapshot 으로 넘기므로 상세 호출 N << targets).
    2) `get_snapshot` → 기존 `crawler.get_full_product_info()` 위임.
       내부에서 HTML NUXT + options/display (2회) 까지 수행하지만,
       델타 감지 이후 변경된 극소수만 호출하므로 허용.

테스트:
    전부 mock. 실호출 금지. request_fn / crawler 를 주입받는
    생성자 시그니처로 의존성 역전 → 테스트에서 fake 주입.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from src.core.kream_budget import KreamBudgetExceeded, kream_purpose
from src.models.product import KreamProduct

logger = logging.getLogger(__name__)


# crawler._request 호환 시그니처 (최소한만).
RequestFn = Callable[..., Awaitable[Any]]


class _CrawlerLike(Protocol):
    """실전 주입 대상 — `KreamCrawler` 호환 최소 인터페이스."""

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = ...,
        params: dict | None = ...,
        max_retries: int = ...,
        parse_json: bool = ...,
        purpose: str = ...,
    ) -> Any: ...

    async def get_full_product_info(
        self, product_id: str, *, include_buy_price: bool = ...
    ) -> KreamProduct | None: ...


DEFAULT_RATE_LIMIT_SEC: float = 1.5
DEFAULT_LIGHT_PURPOSE: str = "delta_light"
DEFAULT_SNAPSHOT_PURPOSE: str = "delta_snapshot"

# 크림 센티넬 차단: bid_count=0 일 때 크림이 placeholder 가격(9,990,000 / 5,500,000 등)을
# sell_now_price 로 반환 → 허위 차익 알림 유발. 합리적 최대치 초과 시 실제 호가 없음으로 간주.
KREAM_SELL_NOW_MAX: int = 8_000_000


class KreamDeltaClient:
    """델타 워처용 크림 클라이언트.

    주입 옵션 (택일):
        - crawler: `KreamCrawler` 인스턴스 (실전)
        - request_fn + snapshot_fn: 저수준 콜러블 (테스트/고급)

    두 경로 모두 최종적으로 `kream_budget` wrapper 를 통과한
    호출로 이어지게 한다.
    """

    def __init__(
        self,
        crawler: _CrawlerLike | None = None,
        *,
        request_fn: RequestFn | None = None,
        snapshot_fn: (Callable[[str], Awaitable[KreamProduct | None]] | None) = None,
        rate_limit_sec: float = DEFAULT_RATE_LIMIT_SEC,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if crawler is None and (request_fn is None or snapshot_fn is None):
            raise ValueError("crawler 또는 (request_fn+snapshot_fn) 중 하나는 필수")
        self._crawler = crawler
        self._request_fn = request_fn
        self._snapshot_fn = snapshot_fn
        self._rate_limit_sec = max(0.0, float(rate_limit_sec))
        self._sleep = sleep_fn or asyncio.sleep

    # ------------------------------------------------------------------
    # 저수준 위임 helper
    # ------------------------------------------------------------------
    async def _call_request(
        self,
        endpoint: str,
        *,
        params: dict | None = None,
        purpose: str = DEFAULT_LIGHT_PURPOSE,
    ) -> Any:
        """주입된 request_fn → crawler._request 순으로 위임.

        KreamBudgetExceeded 는 명시적으로 재포착·전파 (문서화 목적).
        """
        try:
            if self._request_fn is not None:
                return await self._request_fn(
                    "GET",
                    endpoint,
                    params=params,
                    parse_json=True,
                    purpose=purpose,
                )
            assert self._crawler is not None  # 생성자에서 보장
            return await self._crawler._request(
                "GET",
                endpoint,
                params=params,
                parse_json=True,
                purpose=purpose,
            )
        except KreamBudgetExceeded:
            # 일일 캡 초과 → 파이프라인 중단 신호로 위로 전파
            raise

    async def _call_snapshot(self, product_id: str) -> KreamProduct | None:
        if self._snapshot_fn is not None:
            return await self._snapshot_fn(product_id)
        assert self._crawler is not None
        # 델타 스냅샷은 sell_now_price 만 필요 → buying 호출 생략 (호출 1건 절감).
        # `get_full_product_info` 는 내부 _request 호출의 purpose 를 ambient
        # contextvar 로 상속하므로 여기서 명시 태깅해야 캡 분포에 잡힌다.
        with kream_purpose(DEFAULT_SNAPSHOT_PURPOSE):
            try:
                return await self._crawler.get_full_product_info(
                    product_id, include_buy_price=False
                )
            except TypeError:
                # 구형 crawler 호환 — include_buy_price kwarg 미지원 시 폴백
                return await self._crawler.get_full_product_info(product_id)

    # ------------------------------------------------------------------
    # 프로토콜 메서드: fetch_light
    # ------------------------------------------------------------------
    async def fetch_light(self, product_ids: list[int]) -> list[dict[str, Any]]:
        """경량 메타 조회 — id 당 options/display(selling) 1회.

        반환:
            [{"product_id": int, "sell_now_price": int|None}, ...]

        - 404 → 빈 엔트리 없이 스킵 (거래량 0 신규).
        - 응답 None (429/500 재시도 소진 또는 기타) → 스킵.
        - 개별 예외는 로그만 남기고 계속 (루프 격리).
        - `KreamBudgetExceeded` 만 즉시 상위 전파.
        """
        results: list[dict[str, Any]] = []
        if not product_ids:
            return results

        for idx, pid in enumerate(product_ids):
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                logger.warning("[kream_delta_client] 비정수 pid 스킵: %r", pid)
                continue

            if idx > 0 and self._rate_limit_sec > 0:
                await self._sleep(self._rate_limit_sec)

            try:
                data = await self._call_request(
                    "/api/p/options/display",
                    params={
                        "product_id": str(pid_int),
                        "picker_type": "selling",
                    },
                    purpose=DEFAULT_LIGHT_PURPOSE,
                )
            except KreamBudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 — 격리
                logger.warning(
                    "[kream_delta_client] fetch_light 예외 pid=%s err=%s",
                    pid_int,
                    exc,
                )
                continue

            if data is None:
                # 404/재시도 실패 — 신규 상품이거나 일시 오류. 빈 dict 로 남김.
                results.append({"product_id": pid_int, "sell_now_price": None})
                continue

            sell_now = _extract_best_sell_now(data)
            results.append({"product_id": pid_int, "sell_now_price": sell_now})

        return results

    # ------------------------------------------------------------------
    # 프로토콜 메서드: get_snapshot (풀 — 델타워처 변경분용)
    # ------------------------------------------------------------------
    async def get_snapshot(self, product_id: int) -> dict[str, Any]:
        """상세 스냅샷 — 델타 변경분 검증/알림 준비용.

        반환 필드 (빈 dict 가능 = 404 또는 조회 실패):
            product_id, model_number, brand, name,
            sell_now_price, top_size, volume_7d, volume_30d,
            size_prices: [{size, sell_now_price, buy_now_price}...]
        """
        try:
            pid_int = int(product_id)
        except (TypeError, ValueError):
            logger.warning("[kream_delta_client] 비정수 snapshot pid=%r", product_id)
            return {}

        try:
            product = await self._call_snapshot(str(pid_int))
        except KreamBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — 격리
            logger.warning(
                "[kream_delta_client] get_snapshot 예외 pid=%s err=%s",
                pid_int,
                exc,
            )
            return {}

        if product is None:
            return {}

        return _product_to_dict(product, pid_int)

    # ------------------------------------------------------------------
    # 경량 스냅샷: HTML pinia 1회 (API 3~5 → 1 절감)
    # ------------------------------------------------------------------
    async def get_snapshot_light(self, product_id: int) -> dict[str, Any]:
        """경량 스냅샷 — HTML pinia 파싱 1회로 sell_now + volume 추출.

        get_full_product_info (HTML+options/display+screens = 3~5 API) 대신
        HTML 1회에서 __NUXT_DATA__ pinia 로 사이즈별 sell_now + 거래량 추출.
        수익 검증에 필요한 최소 데이터만 반환.

        crawler 미주입(테스트) 시 get_snapshot 으로 폴백.
        """
        try:
            pid_int = int(product_id)
        except (TypeError, ValueError):
            logger.warning("[kream_delta_client] 비정수 snapshot_light pid=%r", product_id)
            return {}

        crawler = self._crawler
        if crawler is None:
            return await self.get_snapshot(pid_int)

        try:
            with kream_purpose(DEFAULT_SNAPSHOT_PURPOSE):
                html = await crawler._request(
                    "GET",
                    f"/products/{pid_int}",
                    parse_json=False,
                    purpose=DEFAULT_SNAPSHOT_PURPOSE,
                )
        except KreamBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[kream_delta_client] snapshot_light HTML 실패 pid=%s err=%s",
                pid_int, exc,
            )
            return {}

        if not html:
            return {}

        try:
            data = crawler._extract_page_data(html)
            if not data:
                return {}
            product = crawler._find_product_in_data(data, str(pid_int))
            if not product:
                product = crawler._parse_product_from_meta(html, str(pid_int))
            if not product:
                return {}
            size_prices = crawler._find_prices_in_data(data, str(pid_int))
            product.size_prices = size_prices
            trade_result = crawler._find_trades_in_data(data, str(pid_int))
            if trade_result:
                product.volume_7d = trade_result.get("volume_7d", 0)
                product.volume_30d = trade_result.get("volume_30d", 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[kream_delta_client] snapshot_light 파싱 실패 pid=%s err=%s",
                pid_int, exc,
            )
            return {}

        return _product_to_dict(product, pid_int)


# ─── 공통 변환 helper ────────────────────────────────────


def _product_to_dict(product: KreamProduct, pid_int: int) -> dict[str, Any]:
    best_size, best_price = _pick_best_sell(product)
    sizes_out: list[dict[str, Any]] = []
    for sp in getattr(product, "size_prices", []) or []:
        sizes_out.append(
            {
                "size": getattr(sp, "size", ""),
                "sell_now_price": getattr(sp, "sell_now_price", None),
                "buy_now_price": getattr(sp, "buy_now_price", None),
            }
        )
    return {
        "product_id": pid_int,
        "model_number": getattr(product, "model_number", "") or "",
        "brand": getattr(product, "brand", "") or "",
        "name": getattr(product, "name", "") or "",
        "sell_now_price": best_price,
        "top_size": best_size or "ALL",
        "volume_7d": int(getattr(product, "volume_7d", 0) or 0),
        "volume_30d": int(getattr(product, "volume_30d", 0) or 0),
        "size_prices": sizes_out,
    }


# ─── 순수 파싱 helper ────────────────────────────────────


def _extract_best_sell_now(data: Any) -> int | None:
    """options/display 응답에서 최고 sell_now_price 추출.

    SDUI ``parameters.price`` 는 "구매입찰 추천 시작가"이므로 sell_now 로
    사용하면 50-80% 부풀려짐. 레거시 필드(``sell_now_price``,
    ``sellNowPrice``)만 신뢰한다.
    """
    best: int | None = None

    def _walk(obj: Any) -> None:
        nonlocal best
        if isinstance(obj, dict):
            for key in ("sell_now_price", "sellNowPrice"):
                v = obj.get(key)
                v_int = _to_int(v)
                if v_int and (best is None or v_int > best):
                    best = v_int
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)
    return best


def _pick_best_sell(product: KreamProduct) -> tuple[str, int | None]:
    """size_prices 중 sell_now_price **최저가**를 대표로.

    최고가 기준은 최소 1개 사이즈만 잘 팔려도 수익 알림이 터져 거짓양성을 유발한다.
    최저가는 "최악의 사이즈로 팔아도 수익이 나는가" 관점이라 보수적 검증에 안전하다.
    snapshot_fn (size="" 브랜치) 도 MIN 을 사용해 의미가 일치한다.
    """
    best_size = ""
    best_price: int | None = None
    for sp in getattr(product, "size_prices", []) or []:
        price = getattr(sp, "sell_now_price", None)
        if price is None:
            continue
        try:
            price_int = int(price)
        except (TypeError, ValueError):
            continue
        if price_int <= 0:
            continue
        if best_price is None or price_int < best_price:
            best_price = price_int
            best_size = str(getattr(sp, "size", "") or "")
    return best_size, best_price


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits) if digits else None
    return None


SNAPSHOT_CACHE_TTL_SEC: float = 1800.0  # 30분


def build_snapshot_fn(
    client: KreamDeltaClient,
    *,
    cache_ttl_sec: float = SNAPSHOT_CACHE_TTL_SEC,
) -> Callable[[int, str], Awaitable[dict[str, Any] | None]]:
    """V3Runtime `kream_snapshot_fn` (pid, size) → {sell_now_price, volume_7d} 어댑터.

    경량 경로 (2026-04-16):
        기존 ``get_snapshot`` → ``get_full_product_info`` (HTML+options/display+screens
        = 3~5 API/상품) 대신, ``get_snapshot_light`` (HTML pinia 1회) 로 교체.
        sell_now_price + volume_7d 모두 pinia 에서 추출. pinia 거래 내역은
        최대 5건이라 volume 은 하한(floor)이지만 게이트(≥1) 판정에 충분.

    TTL 캐시 (30분):
        같은 product_id 를 30분 내 재조회하지 않음. 22개 어댑터에서 동일
        상품이 중복 후보로 올라와도 캐시 히트 → API 0회.

    - 사이즈가 size_prices 에 존재하면 해당 sell_now_price 사용 (정확도 우선)
    - 사이즈가 비거나 "ALL" 이면 size_prices 중 **MIN sell_now_price** 를 보수
      기준으로 사용. 최저 사이즈로도 수익이 나야 거짓 알림을 막을 수 있다.
      (2026-04-14 사고: MAX 기준이 314건 거짓양성 발생시킴)
    - 매치 실패 시 None → candidate drop
    - KreamBudgetExceeded 는 그대로 전파
    """
    _cache: dict[int, tuple[dict[str, Any], float]] = {}

    async def _get_cached_snapshot(pid: int) -> dict[str, Any] | None:
        now = time.monotonic()
        cached = _cache.get(pid)
        if cached is not None:
            snap, ts = cached
            if now - ts < cache_ttl_sec:
                return snap
            del _cache[pid]

        if hasattr(client, "get_snapshot_light"):
            snap = await client.get_snapshot_light(pid)
        else:
            snap = await client.get_snapshot(pid)
        if snap:
            _cache[pid] = (snap, now)
        return snap

    async def _snapshot(
        kream_product_id: int, size: str
    ) -> dict[str, Any] | None:
        snap = await _get_cached_snapshot(kream_product_id)
        if not snap:
            return None
        volume_7d = int(snap.get("volume_7d") or 0)
        size_norm = (size or "").strip()

        size_prices_raw = snap.get("size_prices") or []

        def _norm_sp(sp: dict) -> dict | None:
            price = sp.get("sell_now_price")
            if not price:
                return None
            try:
                p_int = int(price)
            except (TypeError, ValueError):
                return None
            if p_int <= 0:
                return None
            if p_int >= KREAM_SELL_NOW_MAX:
                logger.warning(
                    "[v3] 크림 sell_now 센티넬 차단: pid_size=%s price=%d",
                    sp.get("size"),
                    p_int,
                )
                return None
            return {
                "size": str(sp.get("size") or "").strip(),
                "sell_now_price": p_int,
                "buy_now_price": sp.get("buy_now_price"),
            }

        normalized = [x for x in (_norm_sp(sp) for sp in size_prices_raw) if x]

        if not size_norm or size_norm.upper() == "ALL":
            if not normalized:
                return None
            prices = [sp["sell_now_price"] for sp in normalized]
            return {
                "sell_now_price": min(prices),
                "volume_7d": volume_7d,
                "size_prices": normalized,
            }

        for sp in normalized:
            if sp["size"] == size_norm:
                return {
                    "sell_now_price": sp["sell_now_price"],
                    "volume_7d": volume_7d,
                    "size_prices": [sp],
                }
        return None

    def _has_cache(pid: int) -> bool:
        """캐시에 유효한 스냅샷이 있는지 확인 (쓰로틀 스킵 판정용)."""
        cached = _cache.get(pid)
        if cached is None:
            return False
        _, ts = cached
        return (time.monotonic() - ts) < cache_ttl_sec

    _snapshot.has_cache = _has_cache  # type: ignore[attr-defined]
    return _snapshot


__all__ = [
    "DEFAULT_LIGHT_PURPOSE",
    "DEFAULT_RATE_LIMIT_SEC",
    "DEFAULT_SNAPSHOT_PURPOSE",
    "SNAPSHOT_CACHE_TTL_SEC",
    "KreamDeltaClient",
    "build_snapshot_fn",
]
