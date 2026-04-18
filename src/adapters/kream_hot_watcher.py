"""크림 hot 감시 어댑터 — 축 ② (Phase 2.5).

v3 양방향 감지 전략 중 "크림발" 축 전용 어댑터.

설계 배경:
    축 ① (소싱처발): 소싱처 카탈로그 덤프 → 매칭 → 수익 (무신사 어댑터 등)
    축 ② (크림발)  : 크림 hot 거래량 감시 → 가격/거래량 급등 → 후보 → 수익

기존 `src/tier2_monitor.py` 는 watchlist 기반 알림 연결형이다.
이 어댑터는 kream_products 테이블의 hot (volume_7d >= 5) 전체를 대상으로
60초 간격으로 폴링해서 가격·거래량 급등 시점을 포착하고
`CandidateMatched` 이벤트만 발행한다. 알림·수익 계산은 orchestrator 체인이
담당한다.

설계 원칙:
    - 어댑터는 producer 전용. orchestrator 를 직접 참조하지 않는다.
    - Discord 직접 알림 금지 — bus.publish(CandidateMatched) 만 호출.
    - 크림 실호출은 주입된 `kream_client` 를 통해서만 — 테스트에서는 mock.
    - 개별 상품 예외는 폴링 루프를 중단시키지 않는다 (격리).
    - 크림 자체가 원천이므로 콜라보/서브타입/가격 sanity 가드는 적용 X.
    - 소싱처 가격을 모르므로 `retail_price` 는 크림 sell_now 로 세팅
      (수익 consumer 가 실제 소싱처 재고 확인 후 수익 계산).

향후 (Phase 2.6+) :
    - call_throttle 게이트와 연결
    - scheduler 런타임 기동 (tier2_monitor 안정화 후 교체)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.core.db import sync_connect
from src.core.event_bus import CandidateMatched, EventBus
from src.core.kream_budget import KreamBudgetExceeded, kream_purpose

logger = logging.getLogger(__name__)


# ─── 임계값 기본값 ─────────────────────────────────────────
DEFAULT_PRICE_DELTA_PCT: float = 5.0  # ±5% 이상 변동 시 급등 판정
DEFAULT_VOLUME_SPIKE_RATIO: float = 2.0  # 직전 대비 2배 이상이면 급등
DEFAULT_POLL_INTERVAL_SEC: int = 60
DEFAULT_HOT_VOLUME_THRESHOLD: int = 5


# ─── 크림 클라이언트 인터페이스 ────────────────────────────
class KreamClientProtocol(Protocol):
    """어댑터가 기대하는 크림 스냅샷 조회 인터페이스.

    실제 구현체는 `src.crawlers.kream.kream_crawler` 래핑이 될 예정이나
    Phase 2.5 에서는 mock 전용 — 런타임 기동 X.
    """

    async def get_snapshot(self, product_id: int) -> dict | None:
        """상품의 현재 sell_now 최저가와 7일 거래량을 반환.

        Returns
        -------
        dict | None
            ``{"sell_now_price": int, "volume_7d": int, "top_size": str}``
            조회 실패 시 None.
        """
        ...


@dataclass
class _Snapshot:
    """상품별 baseline 스냅샷 (메모리 내)."""

    sell_now_price: int
    volume_7d: int
    top_size: str
    recorded_at: float


@dataclass
class PollStats:
    """한 사이클 폴링 결과."""

    polled: int = 0
    spiked: int = 0
    published: int = 0
    errors: int = 0
    baseline_initialized: int = 0
    missing_snapshot: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "polled": self.polled,
            "spiked": self.spiked,
            "published": self.published,
            "errors": self.errors,
            "baseline_initialized": self.baseline_initialized,
            "missing_snapshot": self.missing_snapshot,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class KreamHotWatcher:
    """크림 hot 상품 주기 폴링 → 급등 감지 → CandidateMatched 발행."""

    source_name: str = "kream_hot"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        kream_client: KreamClientProtocol | None = None,
        *,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        price_delta_pct: float = DEFAULT_PRICE_DELTA_PCT,
        volume_spike_ratio: float = DEFAULT_VOLUME_SPIKE_RATIO,
        hot_volume_threshold: int = DEFAULT_HOT_VOLUME_THRESHOLD,
    ) -> None:
        """
        Parameters
        ----------
        bus:
            이벤트 버스. 급등 감지 시 `CandidateMatched` publish.
        db_path:
            크림 로컬 DB. `kream_products` 테이블 조회 (읽기 전용).
        kream_client:
            크림 스냅샷 조회자. Phase 2.5 에서는 mock 필수 (실호출 금지).
        poll_interval_sec:
            run_forever 사이클 간격.
        price_delta_pct:
            가격 변동 임계값 — 절댓값 % 기준.
        volume_spike_ratio:
            거래량 급등 배율 (직전 대비).
        hot_volume_threshold:
            hot 판정용 volume_7d 임계값 (기본 5).
        """
        self._bus = bus
        self._db_path = db_path
        self._client = kream_client
        self._poll_interval_sec = poll_interval_sec
        self._price_delta_pct = price_delta_pct
        self._volume_spike_ratio = volume_spike_ratio
        self._hot_volume_threshold = hot_volume_threshold

        self._baselines: dict[int, _Snapshot] = {}
        self._stop_event = asyncio.Event()
        self._running: bool = False

    # ------------------------------------------------------------------
    # DB — hot 상품 로드
    # ------------------------------------------------------------------
    async def load_hot_products(self) -> list[dict[str, Any]]:
        """kream_products 에서 hot(volume_7d >= threshold) 전체 로드.

        블로킹 sqlite3 를 executor 로 돌린다 (어댑터 수준 단발).
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._load_hot_products_sync
        )

    def _load_hot_products_sync(self) -> list[dict[str, Any]]:
        with sync_connect(self._db_path, read_only=True) as conn:
            rows = conn.execute(
                "SELECT product_id, name, brand, model_number, volume_7d "
                "FROM kream_products "
                "WHERE volume_7d >= ? "
                "ORDER BY volume_7d DESC",
                (self._hot_volume_threshold,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 급등 판정
    # ------------------------------------------------------------------
    def _is_price_spike(self, prev: int, curr: int) -> bool:
        if prev <= 0 or curr <= 0:
            return False
        delta_pct = abs(curr - prev) / prev * 100.0
        return delta_pct >= self._price_delta_pct

    def _is_volume_spike(self, prev: int, curr: int) -> bool:
        if prev <= 0:
            # baseline 0 이면 판정 불가 (무한대 회피)
            return False
        return curr >= prev * self._volume_spike_ratio

    # ------------------------------------------------------------------
    # 1 사이클 폴링
    # ------------------------------------------------------------------
    async def poll_once(self) -> PollStats:
        """hot 전체 1회 폴링."""
        with kream_purpose("hot_watcher"):
            return await self._poll_once_inner()

    async def _poll_once_inner(self) -> PollStats:
        stats = PollStats()
        client = self._client
        if client is None:
            logger.warning(
                "[kream_hot] kream_client 미주입 — 폴링 스킵 (Phase 2.5 mock 전용)"
            )
            stats.finished_at = time.time()
            return stats

        try:
            hot_rows = await self.load_hot_products()
        except Exception:
            logger.exception("[kream_hot] hot 로드 실패")
            stats.errors += 1
            stats.finished_at = time.time()
            return stats

        for row in hot_rows:
            stats.polled += 1
            await self._process_one(row, client, stats)

        stats.finished_at = time.time()
        logger.info("[kream_hot] 폴링 완료: %s", stats.as_dict())
        return stats

    async def _process_one(
        self,
        row: dict[str, Any],
        client: KreamClientProtocol,
        stats: PollStats,
    ) -> None:
        """개별 상품 폴링 — 예외 격리."""
        raw_pid = row.get("product_id")
        try:
            product_id = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            logger.warning("[kream_hot] 비정수 product_id 스킵: %r", raw_pid)
            stats.errors += 1
            return
        if product_id is None:
            stats.errors += 1
            return

        try:
            snapshot = await client.get_snapshot(product_id)
        except KreamBudgetExceeded:
            logger.debug("[kream_hot] 캡 고갈 — snapshot 스킵: pid=%s", product_id)
            return
        except Exception:
            logger.exception(
                "[kream_hot] 스냅샷 조회 예외: pid=%s", product_id
            )
            stats.errors += 1
            return

        if not snapshot:
            stats.missing_snapshot += 1
            return

        try:
            curr_price = int(snapshot.get("sell_now_price") or 0)
            curr_volume = int(snapshot.get("volume_7d") or 0)
            top_size = str(snapshot.get("top_size") or "ALL")
        except (TypeError, ValueError):
            logger.warning(
                "[kream_hot] 스냅샷 파싱 실패: pid=%s data=%r",
                product_id,
                snapshot,
            )
            stats.errors += 1
            return

        if curr_price <= 0:
            stats.missing_snapshot += 1
            return

        baseline = self._baselines.get(product_id)
        if baseline is None:
            self._baselines[product_id] = _Snapshot(
                sell_now_price=curr_price,
                volume_7d=curr_volume,
                top_size=top_size,
                recorded_at=time.time(),
            )
            stats.baseline_initialized += 1
            return

        price_spiked = self._is_price_spike(baseline.sell_now_price, curr_price)
        volume_spiked = self._is_volume_spike(baseline.volume_7d, curr_volume)

        if not (price_spiked or volume_spiked):
            # baseline 갱신 — 완만한 드리프트 흡수 (최신화)
            baseline.sell_now_price = curr_price
            baseline.volume_7d = curr_volume
            baseline.top_size = top_size
            baseline.recorded_at = time.time()
            return

        stats.spiked += 1
        model_no = str(row.get("model_number") or "")
        url = f"https://kream.co.kr/products/{product_id}"

        candidate = CandidateMatched(
            source=self.source_name,
            kream_product_id=product_id,
            model_no=model_no,
            retail_price=curr_price,
            size=top_size,
            url=url,
        )
        try:
            await self._bus.publish(candidate)
            stats.published += 1
        except Exception:
            logger.exception(
                "[kream_hot] publish 실패: pid=%s", product_id
            )
            stats.errors += 1
            return

        # 급등 포착 후 baseline 을 현재로 리셋 → 동일 레벨 연속 재발행 방지
        self._baselines[product_id] = _Snapshot(
            sell_now_price=curr_price,
            volume_7d=curr_volume,
            top_size=top_size,
            recorded_at=time.time(),
        )
        logger.info(
            "[kream_hot] 급등 감지·publish: pid=%s model=%s price=%s vol=%s "
            "(price_spike=%s vol_spike=%s)",
            product_id,
            model_no,
            curr_price,
            curr_volume,
            price_spiked,
            volume_spiked,
        )

    # ------------------------------------------------------------------
    # 무한 루프
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        """stop() 호출 전까지 poll_once 를 poll_interval_sec 간격으로 반복."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        try:
            while not self._stop_event.is_set():
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[kream_hot] poll_once 예외 — 루프 지속")
                # 인터럽트 가능 대기
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval_sec,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False

    async def stop(self) -> None:
        """run_forever 를 종료시킨다."""
        self._stop_event.set()


__all__ = [
    "DEFAULT_HOT_VOLUME_THRESHOLD",
    "DEFAULT_POLL_INTERVAL_SEC",
    "DEFAULT_PRICE_DELTA_PCT",
    "DEFAULT_VOLUME_SPIKE_RATIO",
    "KreamClientProtocol",
    "KreamHotWatcher",
    "PollStats",
]
