"""v3 런타임 부트스트랩 — 오케스트레이터 + 어댑터 통합 (Phase 2.6).

Phase 2.2~2.5 에서 쌓인 부품(event_bus / checkpoint_store / call_throttle /
orchestrator / musinsa_adapter / kream_hot_watcher) 을 **단일 진입점**으로
묶어 실제 봇 프로세스에 병렬 기동한다.

하드 제약 (Phase 2.6):
    - 기본 OFF (`V3Runtime(..., enabled=False)`) — 실사용자가 수동 ON 해야 기동
    - 기존 v2 루프(scheduler.py / tier2_monitor / continuous_scanner /
      tier1_scanner / reverse_scanner) 는 **건드리지 않는다**. v3 는 병행 운영.
    - 알림 발송은 **외부 채널로 직접 보내지 않는다**. 병행 기간에는
      `V3AlertLogger` 로 JSONL + `alert_sent` 테이블 기록만 한다.
      (실제 사용자 채널 발송은 기존 v2 가 담당)
    - 이 파일에서 외부 메시징 클라이언트를 import 하면 안 된다 — 테스트가
      파일 소스 grep 으로 검증한다.
    - 크림 실호출 금지 — candidate_handler 의 크림 스냅샷 조회 함수는 DI.
      테스트에서는 mock 주입, 기본값은 `enabled=False` 라 자동으로 실호출 X.

이중 방어 크림 호출 보호:
    1) 하드 캡 : `src/core/kream_budget.py` (일 10,000 회) — kream wrapper 배선
    2) 소프트  : `CallThrottle` — orchestrator candidate 단계에서 이미 게이트
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from src.adapters.kream_delta_watcher import KreamDeltaWatcher
from src.adapters.kream_hot_watcher import KreamHotWatcher
from src.adapters.musinsa_adapter import MusinsaAdapter
from src.core.call_throttle import CallThrottle
from src.core.checkpoint_store import CheckpointStore
from src.core.event_bus import (
    CandidateMatched,
    CatalogDumped,
    EventBus,
    ProfitFound,
)
from src.core.orchestrator import Orchestrator
from src.core.v3_alert_logger import V3AlertLogger, build_profit_handler
from src.models.product import Signal
from src.profit_calculator import calculate_size_profit, determine_signal

logger = logging.getLogger(__name__)


# 타입 별칭
KreamSnapshotFn = Callable[[int, str], Awaitable[dict | None]]
"""(kream_product_id, size) → {sell_now_price, volume_7d} | None."""


# 알림 하드 플로어 (config 기본값과 동일 기준)
_MIN_PROFIT_DEFAULT = 10_000
_MIN_ROI_DEFAULT = 5.0
_MIN_VOLUME_7D_DEFAULT = 1


class V3Runtime:
    """v3 런타임 단일 진입점.

    생명주기:
        runtime = V3Runtime(db_path, enabled=True, ...)
        await runtime.start()   # recover + consumer 기동 + 어댑터 태스크
        ...
        await runtime.stop()    # 어댑터 stop → orchestrator stop → ckpt close
    """

    def __init__(
        self,
        db_path: str,
        *,
        enabled: bool,
        musinsa_interval_sec: int = 1800,
        hot_poll_interval_sec: int = 60,
        throttle_rate_per_min: float = 15.0,
        throttle_burst: int = 20,
        alert_log_path: str | None = None,
        kream_snapshot_fn: KreamSnapshotFn | None = None,
        kream_delta_client: Any | None = None,
        musinsa_adapter: MusinsaAdapter | None = None,
        hot_watcher: KreamHotWatcher | None = None,
        delta_watcher: KreamDeltaWatcher | None = None,
        min_profit: int = _MIN_PROFIT_DEFAULT,
        min_roi: float = _MIN_ROI_DEFAULT,
        min_volume_7d: int = _MIN_VOLUME_7D_DEFAULT,
    ) -> None:
        """
        Parameters
        ----------
        db_path: 크림 로컬 SQLite 경로.
        enabled: False 면 `start()` 가 즉시 return. 기본값 False 가 안전.
        musinsa_interval_sec: 무신사 어댑터 run_once 주기.
        hot_poll_interval_sec: 크림 hot 감시 폴링 주기.
        throttle_rate_per_min: CallThrottle 분당 허용 호출 수.
        throttle_burst: CallThrottle 버킷 최대치.
        alert_log_path: v3 JSONL 로그 경로. 기본값 None → config.settings 참조.
        kream_snapshot_fn: 후보 단계에서 크림 sell_now 스냅샷 조회. DI.
            (None 이면 실호출 없이 후보는 drop — 테스트/초기 안전용)
        kream_delta_client: 주입 시 hot_watcher 대신 KreamDeltaWatcher 사용
            (187k→4.3k 캡 해소 경로). None 이면 기존 KreamHotWatcher 유지.
        musinsa_adapter / hot_watcher / delta_watcher: 테스트 mock 주입용.
        min_profit / min_roi / min_volume_7d: 알림 하드 플로어. 이하면 drop.
        """
        self._db_path = db_path
        self._enabled = enabled
        self._musinsa_interval_sec = musinsa_interval_sec
        self._hot_poll_interval_sec = hot_poll_interval_sec
        self._throttle_rate_per_min = throttle_rate_per_min
        self._throttle_burst = throttle_burst
        self._alert_log_path = alert_log_path
        self._kream_snapshot_fn = kream_snapshot_fn
        self._kream_delta_client = kream_delta_client

        self._musinsa_adapter_override = musinsa_adapter
        self._hot_watcher_override = hot_watcher
        self._delta_watcher_override = delta_watcher

        self._min_profit = min_profit
        self._min_roi = min_roi
        self._min_volume_7d = min_volume_7d

        self._bus: EventBus | None = None
        self._checkpoints: CheckpointStore | None = None
        self._throttle: CallThrottle | None = None
        self._orchestrator: Orchestrator | None = None
        self._alert_logger: V3AlertLogger | None = None
        self._musinsa: MusinsaAdapter | None = None
        self._hot: KreamHotWatcher | None = None
        self._delta: KreamDeltaWatcher | None = None

        self._adapter_tasks: list[asyncio.Task[None]] = []
        self._started: bool = False

    # ------------------------------------------------------------------
    # 핸들러: catalog — 무신사 어댑터의 match_to_kream 결과를 async iter 로 변환
    # ------------------------------------------------------------------
    def _build_catalog_handler(
        self,
    ) -> Callable[[CatalogDumped], AsyncIterator[CandidateMatched]]:
        async def _handler(
            event: CatalogDumped,
        ) -> AsyncIterator[CandidateMatched]:
            # 어댑터가 이미 bus.publish(CandidateMatched) 로 직접 발행하므로
            # 이 handler 는 추가 후보를 생성하지 않는다. CatalogDumped 체크포인트
            # 자체는 orchestrator 가 record/mark 해 준다.
            # async generator 를 만들기 위해 unreachable yield 포함.
            return
            yield  # type: ignore[unreachable]  # noqa: B901

        return _handler

    # ------------------------------------------------------------------
    # 핸들러: candidate — 크림 sell_now 조회 + profit 계산
    # ------------------------------------------------------------------
    def _build_candidate_handler(
        self,
    ) -> Callable[[CandidateMatched], Awaitable[ProfitFound | None]]:
        snapshot_fn = self._kream_snapshot_fn
        min_profit = self._min_profit
        min_roi = self._min_roi
        min_volume_7d = self._min_volume_7d

        async def _handler(event: CandidateMatched) -> ProfitFound | None:
            # kream_hot 어댑터 발 이벤트는 이미 크림 sell_now 가 retail_price
            # 슬롯에 실려 있음(설계상). 소싱처 발(무신사 등)은 retail_price 가
            # 실제 소싱처 가격이므로 별도 조회 필요.
            if event.source == "kream_hot":
                # 크림 hot 감시는 소싱처 가격을 모름 → 수익 계산 보류
                # (Phase 4 에서 소싱처 재고 교차 로직으로 보강 예정)
                logger.debug(
                    "[v3] kream_hot 후보 — 소싱처 가격 미확정으로 drop: pid=%s",
                    event.kream_product_id,
                )
                return None

            if snapshot_fn is None:
                logger.debug(
                    "[v3] kream_snapshot_fn 미주입 — candidate drop: pid=%s",
                    event.kream_product_id,
                )
                return None

            try:
                snapshot = await snapshot_fn(event.kream_product_id, event.size)
            except Exception:
                logger.exception(
                    "[v3] 크림 스냅샷 조회 실패: pid=%s", event.kream_product_id
                )
                return None

            if not snapshot:
                return None

            try:
                kream_sell_price = int(snapshot.get("sell_now_price") or 0)
                volume_7d = int(snapshot.get("volume_7d") or 0)
            except (TypeError, ValueError):
                logger.warning(
                    "[v3] 스냅샷 파싱 실패: pid=%s data=%r",
                    event.kream_product_id,
                    snapshot,
                )
                return None

            if kream_sell_price <= 0 or event.retail_price <= 0:
                return None

            result = calculate_size_profit(
                retail_price=event.retail_price,
                kream_sell_price=kream_sell_price,
                in_stock=True,
                bid_count=0,
            )

            # 하드 플로어 검증
            if result.net_profit < min_profit:
                return None
            if result.roi < min_roi:
                return None
            if volume_7d < min_volume_7d:
                return None

            signal: Signal = determine_signal(result.net_profit, volume_7d)

            return ProfitFound(
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
                size=event.size,
                retail_price=event.retail_price,
                kream_sell_price=kream_sell_price,
                net_profit=result.net_profit,
                roi=result.roi,
                signal=signal.value,
                volume_7d=volume_7d,
                url=event.url,
            )

        return _handler

    # ------------------------------------------------------------------
    # 생명주기
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """부트스트랩. ``enabled=False`` 면 즉시 return."""
        if not self._enabled:
            logger.info("[v3] V3Runtime 비활성 — start() 스킵")
            return
        if self._started:
            return
        self._started = True

        logger.info("[v3] V3Runtime 기동")
        self._bus = EventBus()
        self._checkpoints = CheckpointStore(self._db_path)
        await self._checkpoints.init()
        self._throttle = CallThrottle(
            rate_per_min=self._throttle_rate_per_min,
            burst=self._throttle_burst,
        )
        self._orchestrator = Orchestrator(
            bus=self._bus,
            checkpoints=self._checkpoints,
            throttle=self._throttle,
        )
        self._orchestrator.on_catalog_dumped(self._build_catalog_handler())
        self._orchestrator.on_candidate_matched(self._build_candidate_handler())

        log_path = self._alert_log_path
        if log_path is None:
            # lazy config import 로 테스트 격리 보존
            from src.config import settings

            log_path = settings.v3_alert_log_path
        self._alert_logger = V3AlertLogger(self._db_path, log_path)
        self._orchestrator.on_profit_found(build_profit_handler(self._alert_logger))

        await self._orchestrator.start()
        await self._orchestrator.recover()

        # 어댑터 — 테스트 override 없으면 기본 생성
        if self._musinsa_adapter_override is not None:
            self._musinsa = self._musinsa_adapter_override
        else:
            self._musinsa = MusinsaAdapter(self._bus, self._db_path)

        # 크림 감시 경로 선택: delta_watcher override > kream_delta_client 주입 > hot_watcher
        use_delta = (
            self._delta_watcher_override is not None
            or self._kream_delta_client is not None
        )

        if use_delta:
            if self._delta_watcher_override is not None:
                self._delta = self._delta_watcher_override
            else:
                self._delta = KreamDeltaWatcher(
                    bus=self._bus,
                    db_path=self._db_path,
                    kream_client=self._kream_delta_client,
                    poll_interval_sec=self._hot_poll_interval_sec,
                )
            watcher_task = asyncio.create_task(
                self._delta.run_forever(), name="v3.kream_delta"
            )
            watcher_label = "delta"
        else:
            if self._hot_watcher_override is not None:
                self._hot = self._hot_watcher_override
            else:
                self._hot = KreamHotWatcher(
                    bus=self._bus,
                    db_path=self._db_path,
                    poll_interval_sec=self._hot_poll_interval_sec,
                )
            watcher_task = asyncio.create_task(
                self._hot.run_forever(), name="v3.kream_hot"
            )
            watcher_label = "hot"

        self._adapter_tasks = [
            asyncio.create_task(
                self._musinsa_loop(), name="v3.musinsa_loop"
            ),
            watcher_task,
        ]
        logger.info(
            "[v3] V3Runtime 기동 완료 — musinsa_interval=%ss watcher=%s interval=%ss",
            self._musinsa_interval_sec,
            watcher_label,
            self._hot_poll_interval_sec,
        )

    async def _musinsa_loop(self) -> None:
        """무신사 어댑터 주기 실행 루프 — 예외는 루프 지속."""
        assert self._musinsa is not None
        while True:
            try:
                await self._musinsa.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[v3] musinsa run_once 예외 — 루프 지속")
            try:
                await asyncio.sleep(self._musinsa_interval_sec)
            except asyncio.CancelledError:
                raise

    async def stop(self) -> None:
        """모든 태스크 취소 + 대기 + 리소스 해제."""
        if not self._started:
            return
        self._started = False
        logger.info("[v3] V3Runtime 종료")

        if self._hot is not None:
            try:
                await self._hot.stop()
            except Exception:
                logger.exception("[v3] hot watcher stop 실패")

        if self._delta is not None:
            try:
                await self._delta.stop()
            except Exception:
                logger.exception("[v3] delta watcher stop 실패")

        for task in self._adapter_tasks:
            task.cancel()
        await asyncio.gather(*self._adapter_tasks, return_exceptions=True)
        self._adapter_tasks = []

        if self._orchestrator is not None:
            try:
                await self._orchestrator.stop()
            except Exception:
                logger.exception("[v3] orchestrator stop 실패")

        if self._checkpoints is not None:
            try:
                await self._checkpoints.close()
            except Exception:
                logger.exception("[v3] checkpoints close 실패")

    def stats(self) -> dict[str, Any]:
        """런타임 현황 스냅샷."""
        return {
            "enabled": self._enabled,
            "started": self._started,
            "ts": time.time(),
            "orchestrator": (
                self._orchestrator.stats() if self._orchestrator else None
            ),
            "adapter_tasks": len(self._adapter_tasks),
        }


async def _safe_start_v3(runtime: V3Runtime) -> bool:
    """`main.py` 용 기동 헬퍼 — 예외를 흡수해 v2 가 죽지 않게 보장.

    Returns
    -------
    bool
        기동 성공 여부. False 면 v3 비활성 상태로 간주해야 한다.
    """
    try:
        await runtime.start()
        return True
    except Exception:
        logger.exception("[v3] V3Runtime 기동 실패 — v2 단독 운영 지속")
        return False


__all__ = ["V3Runtime", "_safe_start_v3"]
