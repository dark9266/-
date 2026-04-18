"""크림 델타 기반 감시 어댑터 (Phase 3) — hot watcher 대체 경로.

배경:
    `kream_hot_watcher` 는 130건 × 60초 전수 폴링 → 일 187,200 호출.
    KREAM_DAILY_CAP=10,000 의 18.7배. 파일럿 즉시 차단.

해결:
    1. 감시 대상 ID 목록 로드 (DB only — volume_7d >= 5)
    2. 경량 조회 (list/latest API) 1~2회로 모든 대상의 메타 획득
    3. DeltaEngine 해시 비교 → 바뀐 것만 추출
    4. 변경분에 대해서만 상세 조회 (N << 130, 보통 수 건)
    5. CandidateMatched publish
    6. mark_snapshot 으로 해시 갱신

설계 원칙:
    - 어댑터는 producer 전용. orchestrator 참조 X.
    - Discord 직접 알림 금지 — bus.publish(CandidateMatched) 만.
    - 크림 실호출은 주입된 kream_client 통해서만 — 테스트는 mock.
    - 개별 상품 예외는 루프 중단시키지 않음 (격리).
    - max_changes_per_poll 로 1 사이클 상한 (폭증 방어).
    - 병행 운영: kream_hot_watcher 는 그대로 살려둠. 실전 교체는 파일럿 후.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.core.db import sync_connect
from src.core.delta_engine import DeltaEngine
from src.core.event_bus import CandidateMatched, EventBus
from src.core.kream_budget import KreamBudgetExceeded

logger = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SEC: int = 60
DEFAULT_HOT_VOLUME_THRESHOLD: int = 5
DEFAULT_MAX_CHANGES_PER_POLL: int = 30


class KreamDeltaClientProtocol(Protocol):
    """델타 워처가 기대하는 크림 클라이언트 인터페이스.

    - fetch_light(ids): 1~2 회 경량 호출로 대상 상품들의 최소 메타 반환
    - get_snapshot(pid): 개별 상세 조회 (sell_now/volume/top_size 등)

    Phase 3 에서는 mock 전용 — 실호출 금지.
    """

    async def fetch_light(
        self, product_ids: list[int]
    ) -> list[dict[str, Any]]:
        """경량 메타 리스트. 각 dict 는 최소 product_id 와
        sell_now_price/volume_7d/sold_count 중 해시 대상 필드를 포함."""
        ...

    async def get_snapshot(self, product_id: int) -> dict[str, Any] | None:
        """상세 조회. sell_now_price/volume_7d/top_size 반환."""
        ...


@dataclass
class DeltaPollStats:
    """1 사이클 결과."""

    targets: int = 0
    light_fetch_calls: int = 0
    changed: int = 0
    stale: int = 0
    detail_fetch_calls: int = 0
    published: int = 0
    skipped_over_cap: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "targets": self.targets,
            "light_fetch_calls": self.light_fetch_calls,
            "changed": self.changed,
            "stale": self.stale,
            "detail_fetch_calls": self.detail_fetch_calls,
            "published": self.published,
            "skipped_over_cap": self.skipped_over_cap,
            "errors": self.errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class KreamDeltaWatcher:
    """크림 델타 기반 감시 어댑터.

    hot watcher 의 전수 폴링을 대체. 변경 감지 후에만 상세 조회 → 크림 호출 극감.
    """

    source_name: str = "kream_delta"

    def __init__(
        self,
        bus: EventBus,
        db_path: str,
        kream_client: KreamDeltaClientProtocol | None = None,
        *,
        delta_engine: DeltaEngine | None = None,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        hot_volume_threshold: int = DEFAULT_HOT_VOLUME_THRESHOLD,
        max_changes_per_poll: int = DEFAULT_MAX_CHANGES_PER_POLL,
    ) -> None:
        self._bus = bus
        self._db_path = db_path
        self._client = kream_client
        self._engine = delta_engine or DeltaEngine(db_path)
        self._poll_interval_sec = poll_interval_sec
        self._hot_volume_threshold = hot_volume_threshold
        self._max_changes_per_poll = max_changes_per_poll

        self._stop_event = asyncio.Event()
        self._running = False
        self._initialized = False

    # ------------------------------------------------------------------
    # 초기화
    # ------------------------------------------------------------------
    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        await self._engine.init()
        self._initialized = True

    # ------------------------------------------------------------------
    # 감시 대상 로드
    # ------------------------------------------------------------------
    async def load_watch_targets(self) -> list[int]:
        """kream_products WHERE volume_7d >= threshold → 정수 pid 리스트."""
        return await asyncio.get_running_loop().run_in_executor(
            None, self._load_watch_targets_sync
        )

    def _load_watch_targets_sync(self) -> list[int]:
        with sync_connect(self._db_path, read_only=True) as conn:
            rows = conn.execute(
                "SELECT product_id FROM kream_products "
                "WHERE volume_7d >= ? ORDER BY volume_7d DESC",
                (self._hot_volume_threshold,),
            ).fetchall()
        pids: list[int] = []
        for r in rows:
            try:
                pids.append(int(r["product_id"]))
            except (TypeError, ValueError):
                continue
        return pids

    async def _load_kream_row(self, pid: int) -> dict[str, Any] | None:
        """pid 의 DB 메타 (model_number 등) — publish 시 필요."""
        return await asyncio.get_running_loop().run_in_executor(
            None, self._load_kream_row_sync, pid
        )

    def _load_kream_row_sync(self, pid: int) -> dict[str, Any] | None:
        with sync_connect(self._db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT product_id, name, brand, model_number, volume_7d "
                "FROM kream_products WHERE product_id = ?",
                (str(pid),),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 경량 조회 (프로토콜 위임)
    # ------------------------------------------------------------------
    async def fetch_light(self, ids: list[int]) -> list[dict[str, Any]]:
        """주입된 client 에 위임. Phase 3 mock 전용 — 실호출 금지."""
        if self._client is None or not ids:
            return []
        return await self._client.fetch_light(ids)

    # ------------------------------------------------------------------
    # 1 사이클
    # ------------------------------------------------------------------
    async def poll_once(self) -> DeltaPollStats:
        """감시 대상 로드 → light fetch → delta detect → detail fetch → publish."""
        await self._ensure_init()
        # 크림 호출 원점 태그 — 일일 캡 분포를 루프별로 쪼개 진단.
        # delta_light / delta_snapshot 은 이미 endpoint level purpose 가 걸려
        # 있어 여기 태그는 혹시 태그 누락 경로(설정 조회 등)만 커버한다.
        from src.core.kream_budget import kream_purpose

        with kream_purpose("delta_watcher"):
            return await self._poll_once_inner()

    async def _poll_once_inner(self) -> DeltaPollStats:
        stats = DeltaPollStats()

        client = self._client
        if client is None:
            logger.warning(
                "[kream_delta] kream_client 미주입 — 스킵 (Phase 3 mock 전용)"
            )
            stats.finished_at = time.time()
            return stats

        # 1) 감시 대상 ID
        try:
            target_ids = await self.load_watch_targets()
        except Exception:
            logger.exception("[kream_delta] watch_targets 로드 실패")
            stats.errors += 1
            stats.finished_at = time.time()
            return stats
        stats.targets = len(target_ids)
        if not target_ids:
            stats.finished_at = time.time()
            return stats

        # 2) 경량 조회 (1~2회 호출 예상)
        try:
            light_rows = await self.fetch_light(target_ids)
            stats.light_fetch_calls += 1
        except KreamBudgetExceeded:
            # 캡 고갈은 예상된 정지 — traceback 스팸 없이 스킵
            logger.debug("[kream_delta] 캡 고갈 — poll 스킵")
            stats.finished_at = time.time()
            return stats
        except Exception:
            logger.exception("[kream_delta] fetch_light 실패")
            stats.errors += 1
            stats.finished_at = time.time()
            return stats

        # 3) 델타 감지
        try:
            changed, stale_ids = await self._engine.detect_changes(light_rows)
        except Exception:
            logger.exception("[kream_delta] detect_changes 실패")
            stats.errors += 1
            stats.finished_at = time.time()
            return stats
        stats.changed = len(changed)
        stats.stale = len(stale_ids)

        # 4) 상한 적용
        to_process = changed
        if len(changed) > self._max_changes_per_poll:
            stats.skipped_over_cap = len(changed) - self._max_changes_per_poll
            to_process = changed[: self._max_changes_per_poll]

        # 5) 변경된 것만 상세 조회 → publish
        for item in to_process:
            await self._process_changed(item, client, stats)

        # 6) 처리된 것만 해시 마킹 (상한 내 + 에러 안난 것)
        try:
            await self._engine.mark_snapshot(to_process)
        except Exception:
            logger.exception("[kream_delta] mark_snapshot 실패")
            stats.errors += 1

        stats.finished_at = time.time()
        logger.info("[kream_delta] poll_once: %s", stats.as_dict())
        return stats

    async def _process_changed(
        self,
        light_item: dict[str, Any],
        client: KreamDeltaClientProtocol,
        stats: DeltaPollStats,
    ) -> None:
        """변경된 상품 상세 조회 → CandidateMatched publish. 예외 격리."""
        raw_pid = light_item.get("kream_product_id") or light_item.get("product_id")
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            logger.warning("[kream_delta] 비정수 pid 스킵: %r", raw_pid)
            stats.errors += 1
            return
        if pid is None:
            stats.errors += 1
            return

        try:
            snapshot = await client.get_snapshot(pid)
            stats.detail_fetch_calls += 1
        except KreamBudgetExceeded:
            logger.debug("[kream_delta] 캡 고갈 — snapshot 스킵: pid=%s", pid)
            return
        except Exception:
            logger.exception("[kream_delta] get_snapshot 예외: pid=%s", pid)
            stats.errors += 1
            return

        if not snapshot:
            return

        try:
            curr_price = int(snapshot.get("sell_now_price") or 0)
            top_size = str(snapshot.get("top_size") or "ALL")
        except (TypeError, ValueError):
            logger.warning(
                "[kream_delta] 스냅샷 파싱 실패: pid=%s data=%r", pid, snapshot
            )
            stats.errors += 1
            return
        if curr_price <= 0:
            return

        # DB 메타 (model_number)
        kream_row = await self._load_kream_row(pid)
        model_no = ""
        if kream_row:
            model_no = str(kream_row.get("model_number") or "")

        url = f"https://kream.co.kr/products/{pid}"
        candidate = CandidateMatched(
            source=self.source_name,
            kream_product_id=pid,
            model_no=model_no,
            retail_price=curr_price,
            size=top_size,
            url=url,
        )
        try:
            await self._bus.publish(candidate)
            stats.published += 1
        except Exception:
            logger.exception("[kream_delta] publish 실패: pid=%s", pid)
            stats.errors += 1

    # ------------------------------------------------------------------
    # 무한 루프
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        """stop() 호출 전까지 poll_once 를 poll_interval_sec 간격 반복."""
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
                    logger.exception("[kream_delta] poll_once 예외 — 루프 지속")
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
        self._stop_event.set()


__all__ = [
    "DEFAULT_HOT_VOLUME_THRESHOLD",
    "DEFAULT_MAX_CHANGES_PER_POLL",
    "DEFAULT_POLL_INTERVAL_SEC",
    "DeltaPollStats",
    "KreamDeltaClientProtocol",
    "KreamDeltaWatcher",
]
