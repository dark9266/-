"""1c-2: runtime 중앙 재고 게이트 테스트.

`make_candidate_handler()` 의 `_handler` 가 `snapshot_fn`(크림 live GET) 호출
**전에** 소싱처 재고 상태(`SourceStockSnapshot`)를 게이트해야 한다.
- UNKNOWN/만료 → 크림 호출 금지 + 알림 금지, verification_pending 보류 로그.
- OUT_OF_STOCK → drop (품절 확정).
- IN_STOCK + 신선 + 사이즈 있음 → 통과, 크림 호출 진행.
- 레거시(`source_stock=None` + `available_sizes` 비어있지 않음) → IN_STOCK 승격.
- `kream_delta` 소스는 게이트 면제.

실호출 금지 — 전부 mock snapshot_fn.
"""

from __future__ import annotations

import sqlite3
import time

from src.core.event_bus import CandidateMatched
from src.core.runtime import V3Runtime
from src.models.stock import SourceStockSnapshot, StockState

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


class _NoopMusinsaAdapter:
    source_name = "musinsa"

    async def run_once(self) -> dict:
        return {"dumped": 0}


class _NoopHotWatcher:
    source_name = "kream_hot"

    async def run_forever(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _make_handler(tmp_path, snap_fn):
    db = str(tmp_path / "kream.db")
    _init_db(db)
    runtime = V3Runtime(
        db_path=db,
        enabled=True,
        alert_log_path=str(tmp_path / "v3_alerts.jsonl"),
        kream_snapshot_fn=snap_fn,
        musinsa_adapter=_NoopMusinsaAdapter(),  # type: ignore[arg-type]
        hot_watcher=_NoopHotWatcher(),  # type: ignore[arg-type]
    )
    return runtime._build_candidate_handler(), runtime


# ─── 1. source_stock=None + available_sizes 빈 소싱처 → 보류 ──────────


async def test_unverified_candidate_blocks_kream_call(tmp_path, caplog):
    calls: list[tuple[int, str]] = []

    async def snap_fn(pid: int, size: str) -> dict | None:
        calls.append((pid, size))
        return {"sell_now_price": 300_000, "volume_7d": 10}

    handler, _ = _make_handler(tmp_path, snap_fn)
    cand = CandidateMatched(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=120_000,
        size="270",
        url="https://www.musinsa.com/products/1",
    )
    with caplog.at_level("INFO", logger="src.core.runtime"):
        result = await handler(cand)

    assert result is None
    assert calls == [], "미검증 후보는 크림 호출 금지"
    assert any("verification_pending" in r.message for r in caplog.records)


# ─── 2. source_stock=UNKNOWN → 동일 보류 ───────────────────────────────


async def test_unknown_state_blocks_kream_call(tmp_path, caplog):
    calls: list[tuple[int, str]] = []
    now = time.time()

    async def snap_fn(pid: int, size: str) -> dict | None:
        calls.append((pid, size))
        return {"sell_now_price": 300_000, "volume_7d": 10}

    handler, _ = _make_handler(tmp_path, snap_fn)
    snapshot = SourceStockSnapshot(
        state=StockState.UNKNOWN,
        available_sizes=(),
        observed_at=now,
        expires_at=now + 1800,
        reason_code="api_400",
    )
    cand = CandidateMatched(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=120_000,
        size="270",
        url="https://www.musinsa.com/products/1",
        source_stock=snapshot,
    )
    with caplog.at_level("INFO", logger="src.core.runtime"):
        result = await handler(cand)

    assert result is None
    assert calls == []
    assert any("verification_pending" in r.message for r in caplog.records)
    assert any("api_400" in r.message for r in caplog.records)


# ─── 3. 만료 snapshot (IN_STOCK 이지만 expires_at < now) → 보류 ────────


async def test_expired_in_stock_snapshot_blocks_kream_call(tmp_path, caplog):
    calls: list[tuple[int, str]] = []
    now = time.time()

    async def snap_fn(pid: int, size: str) -> dict | None:
        calls.append((pid, size))
        return {"sell_now_price": 300_000, "volume_7d": 10}

    handler, _ = _make_handler(tmp_path, snap_fn)
    snapshot = SourceStockSnapshot(
        state=StockState.IN_STOCK,
        available_sizes=("270",),
        observed_at=now - 3600,
        expires_at=now - 100,  # 이미 만료
    )
    cand = CandidateMatched(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=120_000,
        size="270",
        url="https://www.musinsa.com/products/1",
        source_stock=snapshot,
    )
    with caplog.at_level("INFO", logger="src.core.runtime"):
        result = await handler(cand)

    assert result is None
    assert calls == []
    assert any("verification_pending" in r.message for r in caplog.records)


# ─── 4. OUT_OF_STOCK → drop (crawl 호출 안 됨) ─────────────────────────


async def test_out_of_stock_drops_without_kream_call(tmp_path, caplog):
    calls: list[tuple[int, str]] = []
    now = time.time()

    async def snap_fn(pid: int, size: str) -> dict | None:
        calls.append((pid, size))
        return {"sell_now_price": 300_000, "volume_7d": 10}

    handler, _ = _make_handler(tmp_path, snap_fn)
    snapshot = SourceStockSnapshot(
        state=StockState.OUT_OF_STOCK,
        available_sizes=(),
        observed_at=now,
        expires_at=now + 1800,
    )
    cand = CandidateMatched(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=120_000,
        size="270",
        url="https://www.musinsa.com/products/1",
        source_stock=snapshot,
    )
    with caplog.at_level("INFO", logger="src.core.runtime"):
        result = await handler(cand)

    assert result is None
    assert calls == []
    assert any("품절" in r.message for r in caplog.records)


# ─── 5. IN_STOCK + 신선 + 사이즈 → 통과 → ProfitFound ──────────────────


async def test_in_stock_fresh_passes_gate_and_calls_kream(tmp_path):
    calls: list[tuple[int, str]] = []
    now = time.time()

    async def snap_fn(pid: int, size: str) -> dict | None:
        calls.append((pid, size))
        return {
            "sell_now_price": 300_000,
            "volume_7d": 10,
            "size_prices": [{"size": "270", "sell_now_price": 300_000}],
        }

    handler, _ = _make_handler(tmp_path, snap_fn)
    snapshot = SourceStockSnapshot(
        state=StockState.IN_STOCK,
        available_sizes=("270",),
        observed_at=now,
        expires_at=now + 1800,
    )
    cand = CandidateMatched(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=120_000,
        size="270",
        url="https://www.musinsa.com/products/1",
        source_stock=snapshot,
    )
    result = await handler(cand)

    assert calls == [(101, "270")]
    assert result is not None
    assert result.kream_sell_price == 300_000
    assert result.net_profit >= 10_000
    assert result.roi >= 5.0


# ─── 6. 레거시 승격: source_stock=None + available_sizes 비어있지 않음 ─


async def test_legacy_available_sizes_promotes_to_in_stock(tmp_path):
    calls: list[tuple[int, str]] = []

    async def snap_fn(pid: int, size: str) -> dict | None:
        calls.append((pid, size))
        return {
            "sell_now_price": 300_000,
            "volume_7d": 10,
            "size_prices": [{"size": "270", "sell_now_price": 300_000}],
        }

    handler, _ = _make_handler(tmp_path, snap_fn)
    cand = CandidateMatched(
        source="musinsa",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=120_000,
        size="270",
        url="https://www.musinsa.com/products/1",
        available_sizes=("270",),
    )
    result = await handler(cand)

    assert calls == [(101, "270")]
    assert result is not None
    assert result.kream_sell_price == 300_000


# ─── 7. kream_delta 면제 — 게이트 안 걸리고 기존 흐름 진행 ─────────────


async def test_kream_delta_source_exempt_from_gate(tmp_path):
    calls: list[tuple[int, str]] = []

    async def snap_fn(pid: int, size: str) -> dict | None:
        calls.append((pid, size))
        return {"sell_now_price": 300_000, "volume_7d": 10}

    handler, _ = _make_handler(tmp_path, snap_fn)
    cand = CandidateMatched(
        source="kream_delta",
        kream_product_id=101,
        model_no="CW2288-111",
        retail_price=120_000,
        size="270",
        url="https://kream.co.kr/products/101",
        source_stock=None,
    )
    result = await handler(cand)

    assert calls == [(101, "270")], "kream_delta 는 게이트 면제 — 크림 호출 진행되어야 함"
    assert result is not None
    assert result.kream_sell_price == 300_000
