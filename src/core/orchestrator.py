"""오케스트레이터 — 이벤트 파이프라인 중앙 조율 (Phase 2.3c).

역할:
    1) event_bus 의 3개 체인 스텝을 consumer task 로 기동
       - CatalogDumped   → CandidateMatched 다수 (fanout)
       - CandidateMatched → ProfitFound 단건 (throttle gate)
       - ProfitFound     → AlertSent 단건 (dedup)
    2) consumer 진입 시 먼저 checkpoint_store 에 record → 처리 → mark_consumed
       (throttle 거부는 mark_deferred — recover 때 재시도 대상)
    3) CandidateMatched 단계는 record 이후 call_throttle.acquire() 로 레이트 제한
       - 실패 시 해당 ckpt 를 deferred 로 남김 (이벤트 영구 손실 방지)
    4) ProfitFound → AlertSent 단계에서 `alert_sent` 테이블 UNIQUE(checkpoint_id)
       기반 dedup — 재시작 후 중복 처리 시에도 알림 1회만
    5) recover() 는 pending+deferred 체크포인트를 직접 내부 처리 메서드에 주입
       (bus 경유 X → replay race 제거). 체인을 끝까지 몰아 완주시킨다.

설계 원칙:
    - handler 는 사용자가 주입 (DI). 예외는 orchestrator 가 흡수 → 체인 중단 없음
    - 진입점 2개, 처리 로직 1개: bus consumer loop 와 recover 가 동일한
      내부 `_process_*` 를 호출 → 결정적
    - async only, 순환 참조 금지
    - stop() 은 모든 consumer task 를 취소하고 완료까지 대기
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiosqlite

from src.core.call_throttle import CallThrottle
from src.core.checkpoint_store import (
    MAX_REPLAY_ATTEMPTS,
    CheckpointStore,
)
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

# recover() 시 candidate 단계 replay 하드 캡 — 크림 실계정 일일 캡 보호.
# 초과분은 stale 처리되고 다음 어댑터 사이클에서 자연 재생성된다.
RECOVER_CANDIDATE_CAP = 50

# 알림 쿨다운 윈도우 (초) — 동일 (pid, signal) 은 이 구간 내 1회만 발송.
# 시그널 업그레이드(예: 매수→강력매수) 는 escape 허용.
ALERT_COOLDOWN_SECONDS = 6 * 3600

# 시그널 rank — 업그레이드 비교용. 값이 클수록 강함.
_SIGNAL_RANK: dict[str, int] = {
    "비추천": 0,
    "관망": 1,
    "매수": 2,
    "강력매수": 3,
}

_ALERT_SENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id INTEGER UNIQUE NOT NULL,
    kream_product_id INTEGER NOT NULL,
    signal TEXT NOT NULL,
    fired_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_sent_kream ON alert_sent(kream_product_id);
"""


class Orchestrator:
    """이벤트 드리븐 파이프라인 오케스트레이터."""

    def __init__(
        self,
        bus: EventBus,
        checkpoints: CheckpointStore,
        throttle: CallThrottle,
        *,
        recover_candidate_cap: int | None = None,
    ) -> None:
        self._bus = bus
        self._checkpoints = checkpoints
        self._throttle = throttle
        self._recover_candidate_cap = (
            recover_candidate_cap
            if recover_candidate_cap is not None
            else RECOVER_CANDIDATE_CAP
        )

        self._catalog_handler: CatalogHandler | None = None
        self._candidate_handler: CandidateHandler | None = None
        self._profit_handler: ProfitHandler | None = None

        self._tasks: list[asyncio.Task[None]] = []
        self._running: bool = False
        self._alert_schema_ready: bool = False

        # 통계 카운터
        self._stats: dict[str, int] = {
            "catalog_processed": 0,
            "catalog_failed": 0,
            "candidate_processed": 0,
            "candidate_failed": 0,
            "candidate_dropped_throttle": 0,  # 하위 호환 (deferred 카운트와 동치)
            "candidate_deferred": 0,
            "profit_processed": 0,
            "profit_failed": 0,
            "alert_duplicated": 0,
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
    async def _ensure_alert_schema(self) -> None:
        if self._alert_schema_ready:
            return
        db = self._checkpoints._require_db()  # noqa: SLF001
        await db.executescript(_ALERT_SENT_SCHEMA)
        await db.commit()
        self._alert_schema_ready = True

    async def start(self) -> None:
        """3개 consumer task 기동."""
        if self._running:
            return
        self._running = True
        await self._ensure_alert_schema()

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
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def recover(self) -> None:
        """pending+deferred 체크포인트를 직접 내부 처리 메서드로 재주입.

        반드시 `start()` 이후 호출해야 한다 (alert_sent 스키마 + consumer
        subscribe 상태가 필요). 그렇지 않으면 RuntimeError.

        bus publish 를 거치지 않으므로 replay/normal 경로 간 레이스가 없다.
        각 이벤트에 대해 체인을 끝까지 몰아 완주시킨다 (매치 → 수익 → 알림).
        """
        if not self._running:
            raise RuntimeError("Orchestrator.recover() must be called after start()")
        await self._ensure_alert_schema()

        # 체인 역순으로 처리: profit → candidate → catalog
        # (이전 세션이 profit 단계까지 와 있었으면 그대로 alert 까지 완주시키고,
        # candidate 단계 건은 새로 profit→alert 까지 타게 한다.)
        async for ckpt_id, event in self._checkpoints.replay(_CONSUMER_PROFIT):
            attempts = await self._checkpoints.increment_attempts(ckpt_id)
            if attempts > MAX_REPLAY_ATTEMPTS:
                await self._checkpoints.mark_failed(ckpt_id, "replay_attempts")
                logger.error(
                    "profit replay attempts 초과: id=%s", ckpt_id
                )
                continue
            assert isinstance(event, ProfitFound)
            await self._process_profit(event, ckpt_id)

        # candidate recover 는 하드 캡 적용 — 크림 스냅샷 조회 폭주 차단.
        # 초과분은 stale 처리하여 다음 어댑터 사이클에서 자연 재생성한다.
        candidate_replayed = 0
        candidate_stale = 0
        async for ckpt_id, event in self._checkpoints.replay(_CONSUMER_CANDIDATE):
            if candidate_replayed >= self._recover_candidate_cap:
                await self._checkpoints.mark_failed(ckpt_id, "recover_cap")
                candidate_stale += 1
                continue
            attempts = await self._checkpoints.increment_attempts(ckpt_id)
            if attempts > MAX_REPLAY_ATTEMPTS:
                await self._checkpoints.mark_failed(ckpt_id, "replay_attempts")
                logger.error(
                    "candidate replay attempts 초과: id=%s", ckpt_id
                )
                continue
            assert isinstance(event, CandidateMatched)
            await self._process_candidate(event, ckpt_id)
            candidate_replayed += 1
        if candidate_stale:
            logger.warning(
                "candidate recover 캡 초과 — stale drop: count=%d "
                "(cap=%d, 크림 캡 보호)",
                candidate_stale,
                self._recover_candidate_cap,
            )

        async for ckpt_id, event in self._checkpoints.replay(_CONSUMER_CATALOG):
            attempts = await self._checkpoints.increment_attempts(ckpt_id)
            if attempts > MAX_REPLAY_ATTEMPTS:
                await self._checkpoints.mark_failed(ckpt_id, "replay_attempts")
                logger.error(
                    "catalog replay attempts 초과: id=%s", ckpt_id
                )
                continue
            assert isinstance(event, CatalogDumped)
            await self._process_catalog(event, ckpt_id)

    def stats(self) -> dict[str, Any]:
        """단계별 처리/실패/drop 카운트 + throttle 상태."""
        return {
            **self._stats,
            "running": self._running,
            "throttle": self._throttle.stats(),
        }

    # ------------------------------------------------------------------
    # 체크포인트 헬퍼
    # ------------------------------------------------------------------
    async def _record(self, event: Event, consumer: str) -> int | None:
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

    async def _defer(self, checkpoint_id: int | None, reason: str) -> None:
        if checkpoint_id is None:
            return
        try:
            await self._checkpoints.mark_deferred(checkpoint_id, reason)
        except Exception:
            logger.exception("checkpoint mark_deferred 실패: id=%s", checkpoint_id)

    # ------------------------------------------------------------------
    # 처리 메서드 — bus consumer 와 recover 가 공유
    # ------------------------------------------------------------------
    async def _process_catalog(
        self, event: CatalogDumped, ckpt_id: int | None
    ) -> None:
        handler = self._catalog_handler
        if handler is None:
            logger.warning("catalog handler 미등록 — 이벤트 drop")
            await self._mark(ckpt_id)
            return
        produced: list[CandidateMatched] = []
        try:
            async for candidate in handler(event):
                produced.append(candidate)
            self._stats["catalog_processed"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stats["catalog_failed"] += 1
            logger.exception(
                "catalog handler 예외: source=%s", getattr(event, "source", "?")
            )
            await self._mark(ckpt_id)
            return
        # handler 성공 시점에 원본 ckpt 닫기
        await self._mark(ckpt_id)
        # 각 candidate 를 직접 처리 (bus 우회로 결정성 확보)
        for candidate in produced:
            cand_ckpt = await self._record(candidate, _CONSUMER_CANDIDATE)
            await self._process_candidate(candidate, cand_ckpt)

    async def _process_candidate(
        self, event: CandidateMatched, ckpt_id: int | None
    ) -> None:
        # throttle 게이트 — ckpt 는 이미 record 된 상태에서 체크
        # Stage 0 fix (2026-04-14): non-blocking acquire() 가 첫 버스트 후 전량
        # deferred 락을 발생시켜 candidate 단계 영구 정지. 짧은 wait 로 스파이크 흡수.
        allowed = await self._throttle.acquire_wait(timeout=2.0)
        if not allowed:
            self._stats["candidate_dropped_throttle"] += 1
            self._stats["candidate_deferred"] += 1
            logger.info(
                "candidate throttle deferred: model_no=%s", event.model_no
            )
            await self._defer(ckpt_id, "throttle_exhausted")
            return

        handler = self._candidate_handler
        if handler is None:
            logger.warning("candidate handler 미등록 — 이벤트 drop")
            await self._mark(ckpt_id)
            return
        result: ProfitFound | None = None
        try:
            result = await handler(event)
            self._stats["candidate_processed"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stats["candidate_failed"] += 1
            logger.exception(
                "candidate handler 예외: model_no=%s", event.model_no
            )
            await self._mark(ckpt_id)
            return
        await self._mark(ckpt_id)
        if result is not None:
            profit_ckpt = await self._record(result, _CONSUMER_PROFIT)
            await self._process_profit(result, profit_ckpt)

    async def _process_profit(
        self, event: ProfitFound, ckpt_id: int | None
    ) -> None:
        handler = self._profit_handler
        if handler is None:
            logger.warning("profit handler 미등록 — 이벤트 drop")
            await self._mark(ckpt_id)
            return

        # dedup: checkpoint_id 가 있어야 의미 있음. 없으면 일단 best-effort.
        if ckpt_id is not None:
            inserted = await self._try_reserve_alert(ckpt_id, event)
            if not inserted:
                self._stats["alert_duplicated"] += 1
                logger.info(
                    "alert dedup — 이미 발송됨: ckpt_id=%s model_no=%s",
                    ckpt_id,
                    event.model_no,
                )
                await self._mark(ckpt_id)
                return

        try:
            result = await handler(event)
            self._stats["profit_processed"] += 1
            if result is not None and ckpt_id is not None:
                # 예약된 행에 실제 alert_id 기록 (없어도 동작은 무방)
                await self._finalize_alert_row(ckpt_id, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stats["profit_failed"] += 1
            logger.exception(
                "profit handler 예외: model_no=%s", event.model_no
            )
            # handler 예외 시 예약 롤백 → 재시도 가능하게
            if ckpt_id is not None:
                await self._rollback_alert_reservation(ckpt_id)
            await self._mark(ckpt_id)
            return
        await self._mark(ckpt_id)

    async def _try_reserve_alert(
        self, ckpt_id: int, event: ProfitFound
    ) -> bool:
        """alert_sent 에 예약. 6h 쿨다운 내 동일 (pid, signal) 은 차단.

        - checkpoint_id UNIQUE → 같은 ckpt 재처리 방어
        - 쿨다운 윈도우 → 프로세스 재시작/델타 재발화로 동일 상품 반복 알림 방어
        - 시그널 업그레이드(예: 매수 → 강력매수) → escape 허용

        2026-04-15 사고: checkpoint_id UNIQUE 단독은 이벤트 리플레이/재생성
        경로에서 매번 새 ckpt_id 를 할당받아 dedup 이 전혀 동작하지 않았다.
        338건 중 168건이 6h 내 동일 pid 재발화.
        """
        db = self._checkpoints._require_db()  # noqa: SLF001

        # 쿨다운 윈도우 내 최근 알림 조회 — fired_at=0(예약중) 은 제외
        import time as _time

        cutoff = _time.time() - ALERT_COOLDOWN_SECONDS
        recent_cur = await db.execute(
            "SELECT signal FROM alert_sent "
            "WHERE kream_product_id = ? AND fired_at > ? "
            "ORDER BY fired_at DESC LIMIT 1",
            (event.kream_product_id, cutoff),
        )
        recent = await recent_cur.fetchone()
        await recent_cur.close()

        if recent is not None:
            old_rank = _SIGNAL_RANK.get(recent[0], 0)
            new_rank = _SIGNAL_RANK.get(event.signal, 0)
            if new_rank <= old_rank:
                # 쿨다운 미충족 + 업그레이드 아님 → 중복 차단
                return False
            # 업그레이드 → escape 허용 (아래 INSERT 로 계속)

        cur = await db.execute(
            "INSERT OR IGNORE INTO alert_sent "
            "(checkpoint_id, kream_product_id, signal, fired_at) "
            "VALUES (?, ?, ?, 0)",
            (ckpt_id, event.kream_product_id, event.signal),
        )
        await db.commit()
        inserted = (cur.rowcount or 0) > 0
        await cur.close()
        return inserted

    async def _finalize_alert_row(
        self, ckpt_id: int, alert: AlertSent
    ) -> None:
        db = self._checkpoints._require_db()  # noqa: SLF001
        await db.execute(
            "UPDATE alert_sent SET fired_at = ? WHERE checkpoint_id = ?",
            (alert.fired_at, ckpt_id),
        )
        await db.commit()

    async def _rollback_alert_reservation(self, ckpt_id: int) -> None:
        try:
            db = self._checkpoints._require_db()  # noqa: SLF001
            await db.execute(
                "DELETE FROM alert_sent WHERE checkpoint_id = ? AND fired_at = 0",
                (ckpt_id,),
            )
            await db.commit()
        except aiosqlite.Error:
            logger.exception("alert 예약 롤백 실패: ckpt_id=%s", ckpt_id)

    # ------------------------------------------------------------------
    # bus consumer 루프 — 신규 이벤트 진입점
    # ------------------------------------------------------------------
    async def _catalog_loop(self, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            if not isinstance(event, CatalogDumped):
                logger.warning(
                    "catalog_loop: 예상 외 타입 drop: %s",
                    type(event).__name__,
                )
                continue
            ckpt_id = await self._record(event, _CONSUMER_CATALOG)
            await self._process_catalog(event, ckpt_id)

    async def _candidate_loop(self, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            if not isinstance(event, CandidateMatched):
                logger.warning(
                    "candidate_loop: 예상 외 타입 drop: %s",
                    type(event).__name__,
                )
                continue
            ckpt_id = await self._record(event, _CONSUMER_CANDIDATE)
            await self._process_candidate(event, ckpt_id)

    async def _profit_loop(self, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            if not isinstance(event, ProfitFound):
                logger.warning(
                    "profit_loop: 예상 외 타입 drop: %s",
                    type(event).__name__,
                )
                continue
            ckpt_id = await self._record(event, _CONSUMER_PROFIT)
            await self._process_profit(event, ckpt_id)


__all__ = ["Orchestrator"]
