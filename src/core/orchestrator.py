"""오케스트레이터 — 이벤트 파이프라인 중앙 조율 (Phase 2.3c).

역할:
    1) event_bus 의 3개 체인 스텝을 consumer task 로 기동
       - CatalogDumped   → CandidateMatched 다수 (fanout)
       - CandidateMatched → ProfitFound 단건 (throttle gate)
       - ProfitFound     → AlertSent 단건
    2) 각 스텝 진입 직전 checkpoint_store 에 record → 성공 시 mark_consumed
    3) CandidateMatched 단계 진입 전 call_throttle.acquire() 통과 필수
       - 실패 시 해당 후보 drop + deny 카운트 증가
    4) recover() 호출 시 checkpoint_store.replay 로 미처리 이벤트를 버스에
       재주입 (replay 된 이벤트는 다시 record 하지 않음)

설계 원칙:
    - handler 는 사용자가 주입 (DI). 예외는 orchestrator 가 흡수 → 체인 중단 없음
    - async only, 순환 참조 금지 (event_bus / checkpoint_store / call_throttle 만 의존)
    - stop() 은 모든 consumer task 를 취소하고 완료까지 대기
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from src.core.call_throttle import CallThrottle
from src.core.checkpoint_store import CheckpointStore
from src.core.event_bus import (
    AlertSent,
    CandidateMatched,
    CatalogDumped,
    Event,
    EventBus,
    ProfitFound,
)

logger = logging.getLogger(__name__)


CatalogHandler = Callable[[CatalogDumped], AsyncIterator[CandidateMatched]]
CandidateHandler = Callable[[CandidateMatched], Awaitable[ProfitFound | None]]
ProfitHandler = Callable[[ProfitFound], Awaitable[AlertSent | None]]


_CONSUMER_CATALOG = "catalog"
_CONSUMER_CANDIDATE = "candidate"
_CONSUMER_PROFIT = "profit"


class Orchestrator:
    """이벤트 드리븐 파이프라인 오케스트레이터."""

    def __init__(
        self,
        bus: EventBus,
        checkpoints: CheckpointStore,
        throttle: CallThrottle,
    ) -> None:
        self._bus = bus
        self._checkpoints = checkpoints
        self._throttle = throttle

        self._catalog_handler: CatalogHandler | None = None
        self._candidate_handler: CandidateHandler | None = None
        self._profit_handler: ProfitHandler | None = None

        self._tasks: list[asyncio.Task[None]] = []
        self._running: bool = False
        self._replaying: bool = False

        # 통계 카운터
        self._stats: dict[str, int] = {
            "catalog_processed": 0,
            "catalog_failed": 0,
            "candidate_processed": 0,
            "candidate_failed": 0,
            "candidate_dropped_throttle": 0,
            "profit_processed": 0,
            "profit_failed": 0,
        }

    # ------------------------------------------------------------------
    # handler 등록
    # ------------------------------------------------------------------
    def on_catalog_dumped(self, fn: CatalogHandler) -> None:
        """CatalogDumped handler 등록. 중복 등록 시 ValueError."""
        if self._catalog_handler is not None:
            raise ValueError("on_catalog_dumped handler already registered")
        self._catalog_handler = fn

    def on_candidate_matched(self, fn: CandidateHandler) -> None:
        """CandidateMatched handler 등록. throttle 통과 필수."""
        if self._candidate_handler is not None:
            raise ValueError("on_candidate_matched handler already registered")
        self._candidate_handler = fn

    def on_profit_found(self, fn: ProfitHandler) -> None:
        """ProfitFound handler 등록."""
        if self._profit_handler is not None:
            raise ValueError("on_profit_found handler already registered")
        self._profit_handler = fn

    # ------------------------------------------------------------------
    # 생명주기
    # ------------------------------------------------------------------
    async def recover(self) -> None:
        """checkpoint 에 남은 미처리 이벤트를 버스에 재주입.

        `_replaying=True` 플래그 중에는 consumer 가 record 를 건너뛴다.
        재주입이 끝나면 플래그를 해제하고 일반 흐름으로 복귀한다.
        """
        self._replaying = True
        try:
            for consumer in (_CONSUMER_CATALOG, _CONSUMER_CANDIDATE, _CONSUMER_PROFIT):
                async for _ckpt_id, event in self._checkpoints.replay(consumer):
                    # 버스에 재주입 — 해당 타입 구독자가 있으면 큐로 들어간다.
                    await self._bus.publish(event)
        finally:
            # replay 단계가 끝나면 일반 flow 로 전환.
            # (recover → start 순서일 때, start 이전에 flag 해제해도 무방.
            #  단, recover 를 start 이후에 호출하는 경우를 위해 여기서 해제.)
            self._replaying = False

    async def start(self) -> None:
        """3개 consumer task 기동."""
        if self._running:
            return
        self._running = True

        catalog_queue = self._bus.subscribe(CatalogDumped)
        candidate_queue = self._bus.subscribe(CandidateMatched)
        profit_queue = self._bus.subscribe(ProfitFound)

        self._tasks = [
            asyncio.create_task(
                self._catalog_loop(catalog_queue), name="orchestrator.catalog"
            ),
            asyncio.create_task(
                self._candidate_loop(candidate_queue), name="orchestrator.candidate"
            ),
            asyncio.create_task(
                self._profit_loop(profit_queue), name="orchestrator.profit"
            ),
        ]

    async def stop(self) -> None:
        """모든 consumer task 취소 + 대기."""
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        # 취소 전파 대기
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def stats(self) -> dict[str, Any]:
        """단계별 처리/실패/drop 카운트 + throttle 상태."""
        return {
            **self._stats,
            "running": self._running,
            "throttle": self._throttle.stats(),
        }

    # ------------------------------------------------------------------
    # 내부 루프
    # ------------------------------------------------------------------
    async def _record(self, event: Event, consumer: str) -> int | None:
        """replay 중이 아닐 때만 checkpoint 기록. 기록한 id 반환."""
        if self._replaying:
            return None
        try:
            return await self._checkpoints.record(event, consumer=consumer)
        except Exception:
            logger.exception("checkpoint record 실패: consumer=%s", consumer)
            return None

    async def _mark(self, checkpoint_id: int | None) -> None:
        if checkpoint_id is None:
            return
        try:
            await self._checkpoints.mark_consumed(checkpoint_id)
        except Exception:
            logger.exception("checkpoint mark_consumed 실패: id=%s", checkpoint_id)

    async def _catalog_loop(self, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            if not isinstance(event, CatalogDumped):
                continue
            ckpt_id = await self._record(event, _CONSUMER_CATALOG)
            handler = self._catalog_handler
            if handler is None:
                logger.warning("catalog handler 미등록 — 이벤트 drop")
                await self._mark(ckpt_id)
                continue
            try:
                async for candidate in handler(event):
                    await self._bus.publish(candidate)
                self._stats["catalog_processed"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self._stats["catalog_failed"] += 1
                logger.exception(
                    "catalog handler 예외: source=%s", getattr(event, "source", "?")
                )
            await self._mark(ckpt_id)

    async def _candidate_loop(self, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            if not isinstance(event, CandidateMatched):
                continue
            # throttle 게이트 — 진입 전에 반드시 소비
            allowed = await self._throttle.acquire()
            if not allowed:
                self._stats["candidate_dropped_throttle"] += 1
                logger.info(
                    "candidate throttle drop: model_no=%s", event.model_no
                )
                continue

            ckpt_id = await self._record(event, _CONSUMER_CANDIDATE)
            handler = self._candidate_handler
            if handler is None:
                logger.warning("candidate handler 미등록 — 이벤트 drop")
                await self._mark(ckpt_id)
                continue
            try:
                result = await handler(event)
                if result is not None:
                    await self._bus.publish(result)
                self._stats["candidate_processed"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self._stats["candidate_failed"] += 1
                logger.exception(
                    "candidate handler 예외: model_no=%s", event.model_no
                )
            await self._mark(ckpt_id)

    async def _profit_loop(self, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            if not isinstance(event, ProfitFound):
                continue
            ckpt_id = await self._record(event, _CONSUMER_PROFIT)
            handler = self._profit_handler
            if handler is None:
                logger.warning("profit handler 미등록 — 이벤트 drop")
                await self._mark(ckpt_id)
                continue
            try:
                result = await handler(event)
                if result is not None:
                    await self._bus.publish(result)
                self._stats["profit_processed"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self._stats["profit_failed"] += 1
                logger.exception(
                    "profit handler 예외: model_no=%s", event.model_no
                )
            await self._mark(ckpt_id)


__all__ = ["Orchestrator"]
