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
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiosqlite

from src.core.alert_outcome import record_alert
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

# Candidate dedup 윈도우 — 동일 (kream_product_id, retail_price) 조합이
# 이 창 안에서 이미 handler 를 성공적으로 거쳤다면 재진입 차단.
# 실측: 15,582 block / 3,226 unique pid = 4.83x 재처리 (동일 카탈로그에서
# 같은 상품이 어댑터 사이클마다 반복 통과). 6h 는 ALERT_COOLDOWN 과
# 정렬 — 가격 변동(retail_price 차이) 시에는 composite key 가 달라
# 자연스럽게 재평가된다.
DEDUP_WINDOW_SECONDS = 6 * 3600

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

# 감사 테이블 — 어댑터가 매칭시킨 후보를 영속화. 사후 분석/false positive
# 조사에 사용. 22 어댑터 전부에 대해 중앙 choke point 에서 기록되므로
# 어댑터별 코드 수정 불필요. UNIQUE(source, product_id) 로 자연 dedup.
_RETAIL_PRODUCTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS retail_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    model_number TEXT NOT NULL,
    brand TEXT DEFAULT '',
    url TEXT DEFAULT '',
    image_url TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, product_id)
);
CREATE INDEX IF NOT EXISTS idx_retail_model ON retail_products(model_number);
"""

# Decision Ledger — 파이프라인 결정 사후 감사.
# 드롭/통과 양쪽 전부 기록해서 false positive 추적, 하드 플로어 튜닝,
# 소싱처별 드롭 사유 분포 분석에 사용.
_DECISION_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT DEFAULT '',
    kream_product_id INTEGER DEFAULT 0,
    model_no TEXT DEFAULT '',
    extra TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log(ts);
CREATE INDEX IF NOT EXISTS idx_decision_log_pid ON decision_log(kream_product_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_reason ON decision_log(reason);
"""

# Candidate dedup — 같은 (pid, retail_price) 가 DEDUP_WINDOW_SECONDS 내
# 이미 handler 를 성공적으로 통과했는지 판정. composite PK 로 가격 변동
# 시에는 자연 재평가.
_CANDIDATE_DEDUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS kream_candidate_dedup (
    kream_product_id INTEGER NOT NULL,
    retail_price INTEGER NOT NULL,
    last_processed_at REAL NOT NULL,
    last_outcome TEXT NOT NULL,
    last_source TEXT DEFAULT '',
    PRIMARY KEY (kream_product_id, retail_price)
);
CREATE INDEX IF NOT EXISTS idx_kream_candidate_dedup_time
    ON kream_candidate_dedup(last_processed_at);
"""

# 허용된 decision 값 — 상수로 못 박아 오타/자유문 삽입 방지.
DECISION_PASS = "pass"
DECISION_BLOCK = "block"

# 표준 reason 코드 — 대시보드 집계용 (자유문이 섞이면 통계가 깨진다).
REASON_SENTINEL_PRICE = "sentinel_price"         # sell_now 9,990,000 차단
REASON_SIZE_INTERSECTION_EMPTY = "size_intersection_empty"
REASON_SNAPSHOT_EMPTY = "snapshot_empty"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_PROFIT_FLOOR = "profit_floor"
REASON_ROI_FLOOR = "roi_floor"
REASON_VOLUME_FLOOR = "volume_floor"
REASON_COOLDOWN = "cooldown"
REASON_SIGNAL_UPGRADE = "signal_upgrade"
REASON_PROFIT_EMITTED = "profit_emitted"
REASON_ALERT_SENT = "alert_sent"
REASON_THROTTLE = "throttle_exhausted"
REASON_HANDLER_MISSING = "handler_missing"
REASON_HANDLER_EXCEPTION = "handler_exception"
REASON_DEDUP_CHECKPOINT = "dedup_checkpoint"
REASON_DEDUP_RECENT = "dedup_recent"
REASON_PREFILTER_UNPROFITABLE = "prefilter_unprofitable"
REASON_PREFILTER_LOW_VOLUME = "prefilter_low_volume"

# Pre-filter KREAM 가격 상승 여유 — 마지막 관측 sell_now_price 가 이만큼
# 상승해도 min_profit 미달이면 차단. 일반 변동성(±20%) 고려.
PREFILTER_UPSIDE_BUFFER_RATIO = 0.20

# 거래량 게이트 기본값. handler 의 min_volume_7d 와 동일 (runtime.py:84).
# 숨은 보석 정책상 1 고정 — 변경 금지 (CLAUDE.md INVARIANT).
PREFILTER_MIN_VOLUME_7D_DEFAULT = 1


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
        self._snapshot_fn: Any = None

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
            "candidate_dedup_skipped": 0,
            "candidate_prefilter_blocked": 0,
            "candidate_prefilter_low_volume": 0,
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

    def on_candidate_matched(
        self, fn: CandidateHandler, *, snapshot_fn: Any = None,
    ) -> None:
        """CandidateMatched handler 등록. throttle 통과 필수."""
        if self._candidate_handler is not None:
            raise ValueError("on_candidate_matched handler already registered")
        self._candidate_handler = fn
        self._snapshot_fn = snapshot_fn

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
        await db.executescript(_RETAIL_PRODUCTS_SCHEMA)
        await db.executescript(_DECISION_LOG_SCHEMA)
        await db.executescript(_CANDIDATE_DEDUP_SCHEMA)
        await db.commit()
        self._alert_schema_ready = True

    async def log_decision(
        self,
        stage: str,
        decision: str,
        reason: str,
        *,
        source: str = "",
        kream_product_id: int = 0,
        model_no: str = "",
        extra: str = "",
    ) -> None:
        """Decision Ledger 쓰기 — 비치명 실패 흡수.

        파이프라인 어느 단계든 호출 가능. DB 락 등으로 실패해도 메인
        흐름을 차단해서는 안 되므로 예외는 debug 로그로 흘린다.
        """
        try:
            import asyncio as _asyncio
            import sqlite3 as _sqlite3
            import time as _time
            db = self._checkpoints._require_db()  # noqa: SLF001
            last_err: Exception | None = None
            for delay in (0.0, 0.05, 0.15, 0.4, 1.0):
                if delay > 0:
                    await _asyncio.sleep(delay)
                try:
                    await db.execute(
                        """INSERT INTO decision_log
                            (ts, stage, decision, reason, source, kream_product_id, model_no, extra)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            _time.time(),
                            stage,
                            decision,
                            reason,
                            source,
                            kream_product_id,
                            model_no,
                            extra,
                        ),
                    )
                    await db.commit()
                    last_err = None
                    break
                except _sqlite3.OperationalError as e:
                    if "locked" not in str(e).lower():
                        raise
                    last_err = e
            if last_err is not None:
                raise last_err
        except aiosqlite.Error:
            logger.warning(
                "decision_log 쓰기 실패 (비치명): stage=%s reason=%s",
                stage,
                reason,
                exc_info=True,
            )
        except Exception:
            logger.warning(
                "decision_log 쓰기 unexpected 예외: stage=%s reason=%s",
                stage,
                reason,
                exc_info=True,
            )

    async def _is_dedup_recent(
        self, kream_product_id: int, retail_price: int
    ) -> bool:
        """(pid, retail_price) 가 DEDUP_WINDOW_SECONDS 내 이미 처리됐는지.

        DB 장애 시 False 반환 (fail-open) — dedup 은 보조 게이트이므로
        장애로 파이프라인 전체가 멈추면 안 된다.
        """
        try:
            import time as _time
            db = self._checkpoints._require_db()  # noqa: SLF001
            cutoff = _time.time() - DEDUP_WINDOW_SECONDS
            async with db.execute(
                "SELECT 1 FROM kream_candidate_dedup "
                "WHERE kream_product_id = ? AND retail_price = ? "
                "AND last_processed_at >= ? LIMIT 1",
                (kream_product_id, retail_price, cutoff),
            ) as cur:
                row = await cur.fetchone()
            return row is not None
        except aiosqlite.Error:
            logger.debug(
                "candidate_dedup 조회 실패 (비치명) — fail-open: pid=%s",
                kream_product_id,
            )
            return False

    async def _prefilter_volume_blocks(self, event: CandidateMatched) -> bool:
        """`kream_products.volume_7d` 가 게이트 미만이면 차단.

        handler 가 어차피 거부할 저거래 후보를 throttle/snapshot 호출 이전에
        잘라내 예산 절약 (adidas 12k+ throttle_exhausted root cause).

        fail-open:
            - row 없음 (DB 미수집 신상) → 통과 (handler 가 실시간 fetch 후 판정)
            - DB 장애 → 통과 (기존 파이프라인 유지)

        주의: stale DB 가능성 있어 row 있고 volume_7d=0 인 경우만 strict
        차단. handler 의 실시간 게이트와 결과 동치 (둘 다 거래량 0 거부).
        """
        from src.config import settings

        min_volume = getattr(
            settings, "alert_min_volume_7d", PREFILTER_MIN_VOLUME_7D_DEFAULT
        )
        if min_volume <= 0:
            return False
        try:
            db = self._checkpoints._require_db()  # noqa: SLF001
            async with db.execute(
                "SELECT volume_7d FROM kream_products WHERE product_id = ?",
                (str(event.kream_product_id),),
            ) as cur:
                row = await cur.fetchone()
        except aiosqlite.Error:
            return False
        if not row:
            return False
        try:
            volume_7d = int(row[0] or 0)
        except (TypeError, ValueError):
            return False
        return volume_7d < min_volume

    async def _prefilter_blocks(
        self, event: CandidateMatched
    ) -> tuple[bool, int]:
        """로컬 kream_price_history 로 tentative profit 산정, 손실이면 True.

        throttle/snapshot 이전에 실행 → API 호출 0. KREAM 가격 상승 여유
        (PREFILTER_UPSIDE_BUFFER_RATIO) 를 더해도 min_profit 미달이면 차단.

        fail-open: 데이터 없거나 테이블 누락/DB 장애 시 (False, 0) —
        기존 파이프라인 유지.

        Returns:
            (block, tentative_net_profit)
        """
        if event.retail_price <= 0:
            return False, 0
        try:
            db = self._checkpoints._require_db()  # noqa: SLF001
            async with db.execute(
                "SELECT sell_now_price FROM kream_price_history "
                "WHERE product_id = ? AND sell_now_price > 0 "
                "ORDER BY fetched_at DESC LIMIT 1",
                (str(event.kream_product_id),),
            ) as cur:
                row = await cur.fetchone()
        except aiosqlite.Error:
            return False, 0
        if not row:
            return False, 0
        try:
            last_sell = int(row[0] or 0)
        except (TypeError, ValueError):
            return False, 0
        if last_sell <= 0:
            return False, 0

        from src.config import settings
        from src.profit_calculator import apply_catch_to_buy_price, calculate_kream_fees

        # Phase 1.5 — Chrome 확장 catch 적용 (prefilter 도 catch 후 평가).
        # candidate_handler 보다 먼저 호출되므로 prefilter 가 정가 기준으로
        # 미리 차단하면 catch hook 무력화 → prefilter 도 동일하게 catch 적용.
        catch_price = await apply_catch_to_buy_price(
            sourcing=event.source,
            native_id=event.model_no,
            color_code=event.color_name or "NONE",
            size_code="NONE",
            original_price=event.retail_price,
        )
        effective_retail = catch_price.buy_price

        fees = calculate_kream_fees(last_sell)
        tentative = last_sell - effective_retail - fees["total_fees"]
        upside = int(last_sell * PREFILTER_UPSIDE_BUFFER_RATIO)
        min_profit = getattr(settings, "alert_min_profit", 10_000)
        if tentative + upside < min_profit:
            return True, tentative
        return False, tentative

    async def _record_dedup(
        self, event: CandidateMatched, outcome: str
    ) -> None:
        """Handler 성공 시 dedup 기록. 예외/deferred 경로에서는 호출 X."""
        try:
            import time as _time
            db = self._checkpoints._require_db()  # noqa: SLF001
            await db.execute(
                """INSERT INTO kream_candidate_dedup
                    (kream_product_id, retail_price, last_processed_at,
                     last_outcome, last_source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(kream_product_id, retail_price) DO UPDATE SET
                    last_processed_at = excluded.last_processed_at,
                    last_outcome = excluded.last_outcome,
                    last_source = excluded.last_source""",
                (
                    int(event.kream_product_id),
                    int(event.retail_price),
                    _time.time(),
                    outcome,
                    event.source or "",
                ),
            )
            await db.commit()
        except aiosqlite.Error:
            logger.debug(
                "candidate_dedup 기록 실패 (비치명): pid=%s",
                event.kream_product_id,
            )

    async def _persist_retail_product(self, event: CandidateMatched) -> None:
        """감사 목적으로 매칭된 후보를 retail_products 에 영속화.

        CandidateMatched 는 name/brand 를 담지 않으므로 model_no 를 name 및
        product_id 대체값으로 사용. 풍부한 메타는 향후 event 필드 확장 시
        주입 가능 (UNIQUE(source, product_id) 덕분에 업서트 호환).

        실패는 감사 경로가 메인 파이프라인을 멈추지 않도록 흡수.
        """
        if not event.model_no:
            return
        try:
            db = self._checkpoints._require_db()  # noqa: SLF001
            await db.execute(
                """INSERT INTO retail_products
                    (source, product_id, name, model_number, url, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source, product_id) DO UPDATE SET
                    model_number = excluded.model_number,
                    url = excluded.url,
                    updated_at = CURRENT_TIMESTAMP""",
                (
                    event.source,
                    event.model_no,
                    event.model_no,  # name fallback — 어댑터 확장 시 교체
                    event.model_no,
                    event.url or "",
                ),
            )
            await db.commit()
        except aiosqlite.Error:
            logger.debug(
                "retail_products 영속화 실패 (비치명): source=%s model=%s",
                event.source,
                event.model_no,
            )

    async def start(self) -> None:
        """3개 consumer task 기동."""
        if self._running:
            return
        self._running = True
        await self._ensure_alert_schema()

        catalog_queue = self._bus.subscribe(CatalogDumped)
        candidate_queue = self._bus.subscribe(CandidateMatched)
        profit_queue = self._bus.subscribe(ProfitFound)

        # 4-worker fanout — 단일 worker 시 snapshot 직렬 처리로 큐 백로그 누적
        # (3500+ 적체 → 신규/저빈도 어댑터 영구 미도달). throttle 은 공유 토큰
        # 버킷이라 kream 호출 캡 그대로 유지.
        self._tasks = [
            asyncio.create_task(
                self._catalog_loop(catalog_queue), name="orchestrator.catalog"
            ),
            asyncio.create_task(
                self._profit_loop(profit_queue), name="orchestrator.profit"
            ),
        ]
        for i in range(4):
            self._tasks.append(
                asyncio.create_task(
                    self._candidate_loop(candidate_queue),
                    name=f"orchestrator.candidate.{i}",
                )
            )

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
        # adapter heartbeat — 어댑터별 silent failure 감지용 (외부 API 0, local DB)
        try:
            from src.core.adapter_heartbeat import bump as _bump_heartbeat
            await _bump_heartbeat(self._checkpoints.db_path, event.source)
        except Exception:
            logger.exception("[heartbeat] bump 실패")

        # dedup 게이트 — 동일 (pid, retail_price) 가 DEDUP_WINDOW 내 이미
        # handler 를 성공적으로 통과했다면 재진입 차단. 4.83x 재처리 패턴
        # 원인이 여기. snapshot/throttle/handler 모두 우회. retail_price 가
        # 바뀌면 composite key 가 달라져 자연 재평가.
        if await self._is_dedup_recent(
            event.kream_product_id, event.retail_price
        ):
            self._stats["candidate_dedup_skipped"] += 1
            await self.log_decision(
                "candidate", DECISION_BLOCK, REASON_DEDUP_RECENT,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )
            await self._mark(ckpt_id)
            return

        # pre-filter 게이트 ① — 거래량 게이트 (kream_products.volume_7d).
        # adidas root cause: cold tier 라 kream_price_history 0건 → 기존
        # prefilter fail-open → throttle 12k+ 소비 후 handler 에서 거부.
        # DB volume_7d=0 인 케이스만 strict 차단 (handler 와 결과 동치).
        if await self._prefilter_volume_blocks(event):
            self._stats["candidate_prefilter_low_volume"] += 1
            await self.log_decision(
                "candidate", DECISION_BLOCK, REASON_PREFILTER_LOW_VOLUME,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )
            await self._mark(ckpt_id)
            return

        # pre-filter 게이트 ② — 로컬 kream_price_history 로 tentative profit
        # 계산. 명백한 손실(+20% 상승 여유에도 min_profit 미달)이면
        # API 호출 이전에 차단. 실측: 4,320/일 throttle 예산으로 10건/일
        # profit 발견(hit rate 0.23%) → 4,310 token 이 무수익 후보에 낭비.
        # fail-open: 데이터 없으면 기존 파이프라인 유지.
        block, tentative = await self._prefilter_blocks(event)
        if block:
            self._stats["candidate_prefilter_blocked"] += 1
            await self.log_decision(
                "candidate", DECISION_BLOCK, REASON_PREFILTER_UNPROFITABLE,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
                extra=f"tentative={tentative}",
            )
            await self._mark(ckpt_id)
            return

        # 캐시 히트면 API 호출 불필요 → 쓰로틀 토큰 소비 없이 바로 진행
        cache_hit = False
        sfn = self._snapshot_fn
        if sfn is not None and hasattr(sfn, "has_cache"):
            try:
                cache_hit = sfn.has_cache(event.kream_product_id)
            except Exception:  # noqa: BLE001
                pass

        _t_throttle_start = time.perf_counter()
        if not cache_hit:
            allowed = await self._throttle.acquire_wait(timeout=2.0)
        else:
            allowed = True
        _t_throttle = time.perf_counter() - _t_throttle_start

        if not allowed:
            logger.info(
                "candidate throttle deferred: model_no=%s", event.model_no
            )
            await self._defer(ckpt_id, "throttle_exhausted")
            await self.log_decision(
                "candidate", DECISION_BLOCK, REASON_THROTTLE,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )
            self._stats["candidate_dropped_throttle"] += 1
            self._stats["candidate_deferred"] += 1
            return

        handler = self._candidate_handler
        if handler is None:
            logger.warning("candidate handler 미등록 — 이벤트 drop")
            await self.log_decision(
                "candidate", DECISION_BLOCK, REASON_HANDLER_MISSING,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )
            await self._mark(ckpt_id)
            return

        # 사후 감사용 — 22 어댑터 전부의 매칭 후보를 중앙에서 영속화.
        # 2026-04-15 Phase B-4: 기존엔 reverse_scanner 만 retail_products 에
        # 기록 → 15 소싱처 커버리지 갭. 여기로 옮겨 단일 choke point.
        await self._persist_retail_product(event)

        result: ProfitFound | None = None
        _t_handler_start = time.perf_counter()
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
            await self.log_decision(
                "candidate", DECISION_BLOCK, REASON_HANDLER_EXCEPTION,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )
            await self._mark(ckpt_id)
            return
        _t_handler = time.perf_counter() - _t_handler_start
        logger.info(
            "[stage-timing] src=%s pid=%s throttle=%.3fs handler=%.3fs "
            "cache_hit=%s profit=%s",
            event.source, event.kream_product_id, _t_throttle, _t_handler,
            cache_hit, result is not None,
        )
        # handler 성공 — dedup 기록 (예외/deferred 경로는 미기록 → replay 허용)
        await self._record_dedup(
            event, "profit" if result is not None else "no_profit"
        )
        await self._mark(ckpt_id)
        if result is not None:
            await self.log_decision(
                "candidate", DECISION_PASS, REASON_PROFIT_EMITTED,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )
            profit_ckpt = await self._record(result, _CONSUMER_PROFIT)
            await self._process_profit(result, profit_ckpt)

    async def _process_profit(
        self, event: ProfitFound, ckpt_id: int | None
    ) -> None:
        handler = self._profit_handler
        if handler is None:
            logger.warning("profit handler 미등록 — 이벤트 drop")
            await self.log_decision(
                "profit", DECISION_BLOCK, REASON_HANDLER_MISSING,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )
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
                # _try_reserve_alert 내부에서 cooldown block 은 이미 기록됨.
                # 여기 도달은 ckpt_id UNIQUE 재진입 — 별도 이유 코드로 기록.
                await self.log_decision(
                    "profit", DECISION_BLOCK, REASON_DEDUP_CHECKPOINT,
                    source=event.source,
                    kream_product_id=event.kream_product_id,
                    model_no=event.model_no,
                )
                await self._mark(ckpt_id)
                return

        alert_emitted = False
        try:
            result = await handler(event)
            self._stats["profit_processed"] += 1
            if result is not None and ckpt_id is not None:
                # 예약된 행에 실제 alert_id 기록 (없어도 동작은 무방)
                await self._finalize_alert_row(ckpt_id, result)
                alert_emitted = True
                # Phase 4 피드백 루프: alert_followup 행 INSERT (sweep 가 24h 후 체결 검증)
                try:
                    await record_alert(
                        self._checkpoints.db_path,
                        alert_id=result.alert_id,
                        kream_product_id=event.kream_product_id,
                        size=event.size,
                        retail_price=event.retail_price,
                        kream_sell_price_at_fire=event.kream_sell_price,
                        fired_at=result.fired_at,
                    )
                except Exception:
                    logger.exception(
                        "alert_followup 기록 실패 (알림 자체는 정상): alert_id=%s",
                        result.alert_id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stats["profit_failed"] += 1
            logger.exception(
                "profit handler 예외: model_no=%s", event.model_no
            )
            await self.log_decision(
                "profit", DECISION_BLOCK, REASON_HANDLER_EXCEPTION,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )
            # handler 예외 시 예약 롤백 → 재시도 가능하게
            if ckpt_id is not None:
                await self._rollback_alert_reservation(ckpt_id)
            await self._mark(ckpt_id)
            return
        await self._mark(ckpt_id)
        if alert_emitted:
            await self.log_decision(
                "profit", DECISION_PASS, REASON_ALERT_SENT,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
            )

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
                await self.log_decision(
                    "profit", DECISION_BLOCK, REASON_COOLDOWN,
                    source=event.source,
                    kream_product_id=event.kream_product_id,
                    model_no=event.model_no,
                    extra=f"old={recent[0]} new={event.signal}",
                )
                return False
            # 업그레이드 → escape 허용 (아래 INSERT 로 계속)
            await self.log_decision(
                "profit", DECISION_PASS, REASON_SIGNAL_UPGRADE,
                source=event.source,
                kream_product_id=event.kream_product_id,
                model_no=event.model_no,
                extra=f"old={recent[0]} new={event.signal}",
            )

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
        task_name = asyncio.current_task().get_name() if asyncio.current_task() else "?"
        logger.info("[diag-loop-start] candidate_loop entered queue_id=%s task=%s",
                    id(queue), task_name)
        while True:
            event = await queue.get()
            _src = getattr(event, "source", "?")
            logger.info(
                "[diag-loop-get] task=%s src=%s type=%s pid=%s qsize_after_get=%d",
                task_name, _src, type(event).__name__,
                getattr(event, "kream_product_id", None),
                queue.qsize(),
            )
            if not isinstance(event, CandidateMatched):
                logger.warning(
                    "candidate_loop: 예상 외 타입 drop: %s src=%s mod=%s.%s",
                    type(event).__name__, _src,
                    type(event).__module__, type(event).__qualname__,
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
