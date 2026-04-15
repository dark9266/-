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
    # 프로토콜 메서드: get_snapshot
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

        # 사이즈별 sell_now 최고가를 대표 sell_now 로
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

    KreamCrawler._merge_options_into_map 과 동일 원리로 SDUI
    parameters.option_key/price 쌍을 수집하되, 의존성 최소화를 위해
    이 모듈 내에서 단순 재귀 구현.
    """
    best: int | None = None

    def _walk(obj: Any) -> None:
        nonlocal best
        if isinstance(obj, dict):
            params = obj.get("parameters")
            if isinstance(params, dict):
                price = params.get("price")
                if isinstance(price, list) and price:
                    price = price[0]
                p_int = _to_int(price)
                if p_int and (best is None or p_int > best):
                    best = p_int
            # 레거시 필드도 스캔
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


def build_snapshot_fn(
    client: KreamDeltaClient,
) -> Callable[[int, str], Awaitable[dict[str, Any] | None]]:
    """V3Runtime `kream_snapshot_fn` (pid, size) → {sell_now_price, volume_7d} 어댑터.

    - 사이즈가 size_prices 에 존재하면 해당 sell_now_price 사용 (정확도 우선)
    - 사이즈가 비거나 "ALL" 이면 size_prices 중 **MIN sell_now_price** 를 보수
      기준으로 사용. 최저 사이즈로도 수익이 나야 거짓 알림을 막을 수 있다.
      (2026-04-14 사고: MAX 기준이 314건 거짓양성 발생시킴)
    - 매치 실패 시 None → candidate drop
    - KreamBudgetExceeded 는 그대로 전파
    """

    async def _snapshot(
        kream_product_id: int, size: str
    ) -> dict[str, Any] | None:
        snap = await client.get_snapshot(kream_product_id)
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

    return _snapshot


__all__ = [
    "DEFAULT_LIGHT_PURPOSE",
    "DEFAULT_RATE_LIMIT_SEC",
    "DEFAULT_SNAPSHOT_PURPOSE",
    "KreamDeltaClient",
    "build_snapshot_fn",
]
