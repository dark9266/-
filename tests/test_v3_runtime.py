"""v3 런타임 통합 테스트 (Phase 2.6).

검증 목록:
    (a) enabled=False → start() 즉시 return, 태스크 생성 없음
    (b) enabled=True + mock 어댑터 → start/stop 정상, 태스크 취소 확인
    (c) recover 경로 — 미처리 이벤트 있을 때 start 시 체인 완주
    (d) candidate_handler — 하드 플로어 이하 drop, 통과 시 ProfitFound
    (e) v3_alert_logger — 외부 메시징 금지 (소스 grep) + JSONL append +
        INSERT OR IGNORE dedup
    (f) 기동 예외 격리 — _safe_start_v3 헬퍼로 예외 흡수 확인

실호출 금지: 크림·소싱처 HTTP 전부 mock.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from src.core.event_bus import (
    AlertSent,
    CandidateMatched,
    ProfitFound,
)
from src.core.runtime import _ADAPTER_REGISTRY, V3Runtime, _safe_start_v3
from src.core.v3_alert_logger import V3AlertLogger

# ─── DB 헬퍼 ─────────────────────────────────────────────

_KREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS kream_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_number TEXT NOT NULL,
    brand TEXT DEFAULT '',
    volume_7d INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kream_collect_queue (
    model_number TEXT PRIMARY KEY,
    brand_hint TEXT DEFAULT '',
    name_hint TEXT DEFAULT '',
    source TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0
);
"""


def _init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_KREAM_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO kream_products "
            "(product_id, name, model_number, brand, volume_7d) VALUES (?, ?, ?, ?, ?)",
            ("101", "Nike Air Force 1 Low White", "CW2288-111", "Nike", 10),
        )
        conn.commit()
    finally:
        conn.close()


# ─── mock 어댑터 ──────────────────────────────────────────

class _NoopMusinsaAdapter:
    """run_once 가 단순히 카운터만 증가시키는 mock."""

    source_name = "musinsa"

    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self) -> dict:
        self.calls += 1
        await asyncio.sleep(0)
        return {"dumped": 0}


class _NoopHotWatcher:
    """run_forever 가 stop 까지 대기만 하는 mock."""

    source_name = "kream_hot"

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.run_called = False

    async def run_forever(self) -> None:
        self.run_called = True
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()


class _NoopDeltaWatcher:
    """KreamDeltaWatcher mock — hot watcher 와 동일 인터페이스."""

    source_name = "kream_delta"

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.run_called = False

    async def run_forever(self) -> None:
        self.run_called = True
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()


# ─── (a) enabled=False 즉시 return ────────────────────────

async def test_disabled_start_noop(tmp_path):
    db = str(tmp_path / "kream.db")
    _init_db(db)
    runtime = V3Runtime(db_path=db, enabled=False)
    await runtime.start()
    stats = runtime.stats()
    assert stats["started"] is False
    assert stats["enabled"] is False
    assert stats["adapter_tasks"] == 0
    # stop() 도 예외 없이 no-op
    await runtime.stop()


# ─── (b) enabled=True + mock 어댑터 기동/정지 ──────────────

async def test_enabled_start_stop_with_mocks(tmp_path):
    db = str(tmp_path / "kream.db")
    _init_db(db)

    musinsa = _NoopMusinsaAdapter()
    hot = _NoopHotWatcher()

    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        musinsa_interval_sec=3600,
        hot_poll_interval_sec=60,
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        musinsa_adapter=musinsa,  # type: ignore[arg-type]
        hot_watcher=hot,  # type: ignore[arg-type]
    )
    await runtime.start()
    # 태스크가 실제로 생성됨
    stats = runtime.stats()
    assert stats["started"] is True
    assert stats["adapter_tasks"] == 2

    # 어댑터 실행이 loop 에 올라오도록 한번 양보
    await asyncio.sleep(0.05)
    assert hot.run_called is True
    assert musinsa.calls >= 1

    await runtime.stop()
    assert runtime.stats()["started"] is False


# ─── (c) recover 경로 — 미처리 이벤트 완주 ────────────────

async def test_recover_replays_pending_profit(tmp_path):
    """이전 세션의 ProfitFound pending 이 남아있으면 start 시 alert_sent 까지 가야 한다."""
    db = str(tmp_path / "kream.db")
    _init_db(db)
    log_path = tmp_path / "v3_alerts.jsonl"

    # 사전 이벤트 주입: CheckpointStore 로 ProfitFound 하나 pending 기록
    from src.core.checkpoint_store import CheckpointStore

    store = CheckpointStore(db)
    await store.init()
    profit = ProfitFound(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        size="270",
        retail_price=120_000,
        kream_sell_price=200_000,
        net_profit=50_000,
        roi=41.0,
        signal="강력매수",
        volume_7d=10,
        url="https://www.musinsa.com/products/1",
    )
    await store.record(profit, consumer="profit")
    await store.close()

    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        musinsa_interval_sec=3600,
        hot_poll_interval_sec=60,
        alert_log_path=str(log_path),
        musinsa_adapter=_NoopMusinsaAdapter(),  # type: ignore[arg-type]
        hot_watcher=_NoopHotWatcher(),  # type: ignore[arg-type]
    )
    await runtime.start()
    await asyncio.sleep(0.05)

    # JSONL 에 한 줄 기록 되었어야 함
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kream_product_id"] == 101
    assert rec["net_profit"] == 50_000

    # alert_sent 테이블 1행 (orchestrator 가 reserve→finalize)
    conn = sqlite3.connect(db)
    cnt = conn.execute("SELECT COUNT(*) FROM alert_sent").fetchone()[0]
    conn.close()
    assert cnt == 1

    await runtime.stop()


# ─── (d) candidate_handler — 하드 플로어 검증 ─────────────

async def test_candidate_handler_drops_below_floor(tmp_path):
    db = str(tmp_path / "kream.db")
    _init_db(db)

    captured_snapshots: list[tuple[int, str]] = []

    async def snap_fn(pid: int, size: str) -> dict | None:
        captured_snapshots.append((pid, size))
        # 낮은 수익 — 하드 플로어 미달
        return {"sell_now_price": 125_000, "volume_7d": 10}

    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        kream_snapshot_fn=snap_fn,
        musinsa_adapter=_NoopMusinsaAdapter(),  # type: ignore[arg-type]
        hot_watcher=_NoopHotWatcher(),  # type: ignore[arg-type]
    )
    await runtime.start()

    # candidate handler 를 직접 뽑아 호출
    handler = runtime._orchestrator._candidate_handler  # type: ignore[attr-defined]
    assert handler is not None

    low = CandidateMatched(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=120_000,
        size="270",
        url="https://www.musinsa.com/products/1",
    )
    result = await handler(low)
    assert result is None  # 수익 적어 drop
    assert captured_snapshots == [(101, "270")]

    # 고수익 케이스
    async def snap_fn_high(pid: int, size: str) -> dict | None:
        return {"sell_now_price": 300_000, "volume_7d": 10}

    runtime._kream_snapshot_fn = snap_fn_high  # type: ignore[attr-defined]
    handler2 = runtime._build_candidate_handler()
    result2 = await handler2(low)
    assert result2 is not None
    assert result2.kream_sell_price == 300_000
    assert result2.net_profit >= 10_000
    assert result2.roi >= 5.0

    await runtime.stop()


async def test_candidate_handler_kream_hot_source_drops(tmp_path):
    """kream_hot 소스는 소싱처 가격 미확정이라 현 단계에서 drop."""
    db = str(tmp_path / "kream.db")
    _init_db(db)

    called = {"n": 0}

    async def snap_fn(pid: int, size: str) -> dict | None:
        called["n"] += 1
        return {"sell_now_price": 999_999, "volume_7d": 99}

    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        kream_snapshot_fn=snap_fn,
        musinsa_adapter=_NoopMusinsaAdapter(),  # type: ignore[arg-type]
        hot_watcher=_NoopHotWatcher(),  # type: ignore[arg-type]
    )
    await runtime.start()

    handler = runtime._build_candidate_handler()
    cand = CandidateMatched(
        source="kream_hot",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=200_000,
        size="270",
        url="https://kream.co.kr/products/101",
    )
    res = await handler(cand)
    assert res is None
    assert called["n"] == 0

    await runtime.stop()


# ─── (e) v3_alert_logger — 외부 메시징 금지 + JSONL + dedup ─

def test_v3_logger_forbids_external_messaging_imports():
    """runtime.py / v3_alert_logger.py 에 외부 메시징 클라이언트 토큰 금지."""
    runtime_src = Path("src/core/runtime.py").read_text(encoding="utf-8")
    logger_src = Path("src/core/v3_alert_logger.py").read_text(encoding="utf-8")
    forbidden = "discord"
    assert forbidden not in runtime_src.lower(), "runtime.py 에 외부 메시징 토큰 금지"
    assert forbidden not in logger_src.lower(), "v3_alert_logger.py 에 외부 메시징 토큰 금지"


async def test_v3_logger_append_and_dedup(tmp_path):
    """V3AlertLogger 단독 — JSONL 한 줄 append + 중복 dedup.

    alert_sent 테이블 기록은 이 모듈이 아니라 orchestrator 가 담당하므로
    여기서는 JSONL 파일과 dedup 동작만 검증한다.
    """
    db = str(tmp_path / "kream.db")
    _init_db(db)
    log_path = tmp_path / "v3_alerts.jsonl"
    alogger = V3AlertLogger(db, log_path)

    event = ProfitFound(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        size="270",
        retail_price=120_000,
        kream_sell_price=200_000,
        net_profit=50_000,
        roi=41.0,
        signal="강력매수",
        volume_7d=10,
        url="https://www.musinsa.com/products/1",
    )
    first = await alogger.log(event)
    assert isinstance(first, AlertSent)

    # 중복 호출 — in-memory seen set 으로 skip, None
    second = await alogger.log(event)
    assert second is None

    # JSONL 파일에는 한 줄만
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["model_no"] == "CW2288-111"


# ─── (f) 기동 예외 격리 — _safe_start_v3 ──────────────────

async def test_safe_start_v3_swallows_exception(tmp_path):
    class _Boom:
        async def start(self) -> None:
            raise RuntimeError("boom")

    ok = await _safe_start_v3(_Boom())  # type: ignore[arg-type]
    assert ok is False


async def test_safe_start_v3_success(tmp_path):
    db = str(tmp_path / "kream.db")
    _init_db(db)
    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        musinsa_adapter=_NoopMusinsaAdapter(),  # type: ignore[arg-type]
        hot_watcher=_NoopHotWatcher(),  # type: ignore[arg-type]
    )
    ok = await _safe_start_v3(runtime)
    assert ok is True
    await runtime.stop()


# ─── (g) delta_watcher 경로 — kream_delta_client 주입 시 hot 대신 delta ────

# ─── (h) 21 어댑터 자동 등록 경로 ─────────────────────────

class _CountingAdapter:
    """run_once 호출 횟수만 카운트하는 범용 mock 어댑터."""

    def __init__(self, name: str) -> None:
        self.source_name = name
        self.calls = 0

    async def run_once(self) -> dict:
        self.calls += 1
        await asyncio.sleep(0)
        return {"dumped": 0}


class _RaisingAdapter:
    """run_once 가 항상 예외 — 예외 격리 검증용."""

    def __init__(self, name: str) -> None:
        self.source_name = name
        self.calls = 0

    async def run_once(self) -> dict:
        self.calls += 1
        raise RuntimeError(f"{self.source_name} boom")


async def test_all_registered_adapters_started(tmp_path):
    """기본 경로 — 레지스트리의 모든 어댑터 기동, stagger=0 으로 즉시 run_once."""
    db = str(tmp_path / "kream.db")
    _init_db(db)

    overrides = {name: _CountingAdapter(name) for name, _ in _ADAPTER_REGISTRY}
    hot = _NoopHotWatcher()

    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        adapter_interval_sec=3600,
        adapter_stagger_sec=0,
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        adapter_overrides=overrides,
        hot_watcher=hot,
    )
    await runtime.start()
    await asyncio.sleep(0.05)

    stats = runtime.stats()
    assert stats["started"] is True
    expected = len(_ADAPTER_REGISTRY)
    # 레지스트리 수 + 1 hot watcher
    assert stats["adapter_tasks"] == expected + 1
    assert len(stats["adapters"]) == expected
    assert "musinsa" in stats["adapters"]
    assert "29cm" in stats["adapters"]
    assert "worksout" in stats["adapters"]
    assert "thenorthface" in stats["adapters"]

    # stagger=0 이므로 모든 어댑터가 최소 1회는 run_once 실행
    for name, a in overrides.items():
        assert a.calls >= 1, f"{name} run_once 미호출"

    await runtime.stop()


async def test_adapter_exception_isolated(tmp_path):
    """한 어댑터의 run_once 예외가 다른 어댑터 실행을 막지 않아야 한다."""
    db = str(tmp_path / "kream.db")
    _init_db(db)

    # nike는 예외, 나머지는 정상
    overrides: dict[str, object] = {}
    for name, _ in _ADAPTER_REGISTRY:
        if name == "nike":
            overrides[name] = _RaisingAdapter(name)
        else:
            overrides[name] = _CountingAdapter(name)

    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        adapter_interval_sec=3600,
        adapter_stagger_sec=0,
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        adapter_overrides=overrides,
        hot_watcher=_NoopHotWatcher(),
    )
    await runtime.start()
    await asyncio.sleep(0.05)

    # nike 도 한 번 호출되었고 (예외 발생), 다른 어댑터들도 정상 호출
    assert overrides["nike"].calls >= 1
    assert overrides["musinsa"].calls >= 1
    assert overrides["kasina"].calls >= 1

    await runtime.stop()


async def test_adapter_stagger_delays_startup(tmp_path):
    """stagger_sec > 0 이면 초기 지연으로 첫 run_once 가 늦춰져야 한다."""
    db = str(tmp_path / "kream.db")
    _init_db(db)

    overrides = {name: _CountingAdapter(name) for name, _ in _ADAPTER_REGISTRY}

    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        adapter_interval_sec=3600,
        adapter_stagger_sec=5,  # idx 0 즉시, idx 1 은 5초 후
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        adapter_overrides=overrides,
        hot_watcher=_NoopHotWatcher(),
    )
    await runtime.start()
    await asyncio.sleep(0.05)

    # 첫 번째 (idx 0) 는 delay 0 → run_once 1회
    assert overrides["musinsa"].calls >= 1
    # 두 번째 이후 (idx ≥ 1) 는 delay ≥ 5s → 아직 run_once 안 된 상태
    assert overrides["29cm"].calls == 0
    assert overrides["nike"].calls == 0

    await runtime.stop()


async def test_delta_watcher_path_replaces_hot(tmp_path):
    """delta_watcher override 주입 시 hot watcher 대신 delta 가 기동.
    (설계: KreamDeltaWatcher 는 hot watcher 대비 187k→4.3k 호출 절감 경로)
    """
    db = str(tmp_path / "kream.db")
    _init_db(db)

    musinsa = _NoopMusinsaAdapter()
    delta = _NoopDeltaWatcher()
    hot = _NoopHotWatcher()  # 같이 주입해도 delta 우선이어야 함

    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        musinsa_adapter=musinsa,  # type: ignore[arg-type]
        hot_watcher=hot,  # type: ignore[arg-type]
        delta_watcher=delta,  # type: ignore[arg-type]
    )
    await runtime.start()
    await asyncio.sleep(0.05)

    assert delta.run_called is True
    assert hot.run_called is False  # delta 우선 → hot 기동 안 됨
    assert runtime.stats()["adapter_tasks"] == 2

    await runtime.stop()
