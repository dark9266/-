"""v3 알림 파일 로거 — 병행 운영 기간 전용 (Phase 2.6).

목적:
    v3 런타임 경로에서 생성된 `ProfitFound` 이벤트를 **실제 알림 채널로
    발송하지 않고** JSONL 파일에만 기록한다. 기존 v2 루프가 실제 사용자
    알림 채널을 담당하므로 이중 알림을 방지한다.

    `alert_sent` 테이블 기록은 `Orchestrator._try_reserve_alert` 가 이미
    수행한다 (Phase 2.3 리뷰 도입 UNIQUE(checkpoint_id) dedup). 따라서
    여기서는 추가로 DB 에 쓰지 않는다 — 중복 행을 만들지 않기 위해서다.

하드 제약:
    - 이 모듈은 절대 외부 메시징 클라이언트를 import 하면 안 된다.
    - 테스트(`tests/test_v3_runtime.py`) 가 파일 소스 텍스트를 grep 해서
      금지 토큰 포함 여부를 검증한다.
    - 파일 입출력은 동기(블로킹) 단발 append — 어댑터 수준 단발 호출이라 허용.

제공:
    - ``V3AlertLogger`` : JSONL append + 내부 in-memory seen-set dedup
    - ``build_profit_handler`` : Orchestrator.on_profit_found 용 handler 팩토리
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from src.core.event_bus import AlertSent, ProfitFound

logger = logging.getLogger(__name__)


ProfitHandler = Callable[[ProfitFound], Awaitable[AlertSent | None]]


def _dedup_key(event: ProfitFound) -> tuple:
    return (event.kream_product_id, event.model_no, event.size, event.signal)


class V3AlertLogger:
    """JSONL append 전용 — 외부 발송 금지."""

    def __init__(self, db_path: str, log_path: str | Path) -> None:
        # db_path 는 API 호환성용(추후 DB 기반 dedup 재도입 시 사용).
        # 현재는 사용하지 않는다 — 파일 전용.
        self._db_path = db_path
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._seen: set[tuple] = set()
        self._alert_seq: int = 0

    def _append_jsonl(self, payload: dict) -> None:
        """JSONL 한 줄 append (동기). 실패 시 예외 → 호출자에서 처리."""
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def log(self, event: ProfitFound) -> AlertSent | None:
        """ProfitFound → JSONL append → AlertSent 반환.

        중복 호출(동일 dedup_key) 시 skip 후 None 반환.
        dedup 는 프로세스 내 in-memory set — 재시작 후에는 orchestrator 의
        `alert_sent` 테이블 dedup 이 최종 방벽 역할을 한다.
        """
        key = _dedup_key(event)
        if key in self._seen:
            return None
        self._seen.add(key)

        self._alert_seq += 1
        fired_at = time.time()
        alert_id = self._alert_seq

        payload = {
            "ts": fired_at,
            "alert_id": alert_id,
            "kream_product_id": event.kream_product_id,
            "model_no": event.model_no,
            "source": event.source,
            "signal": event.signal,
            "net_profit": event.net_profit,
            "roi": event.roi,
            "volume_7d": event.volume_7d,
            "kream_sell_price": event.kream_sell_price,
            "retail_price": event.retail_price,
            "size": event.size,
            "color_name": getattr(event, "color_name", "") or "",
            "url": event.url,
            "catch_applied": bool(getattr(event, "catch_applied", False)),
            "original_retail": getattr(event, "original_retail", None),
        }
        try:
            self._append_jsonl(payload)
        except Exception:
            logger.exception("[v3_alert_logger] JSONL append 실패: %s", self._log_path)
            # seen 복구 — 재시도 가능하게
            self._seen.discard(key)
            return None

        return AlertSent(
            alert_id=alert_id,
            kream_product_id=event.kream_product_id,
            signal=event.signal,
            fired_at=fired_at,
        )


def build_profit_handler(logger_instance: V3AlertLogger) -> ProfitHandler:
    """Orchestrator.on_profit_found 에 꽂을 handler 생성."""

    async def _handler(event: ProfitFound) -> AlertSent | None:
        return await logger_instance.log(event)

    return _handler


__all__ = ["V3AlertLogger", "build_profit_handler"]
