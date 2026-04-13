"""DeltaEngine 단위 테스트 (Phase 3)."""

from __future__ import annotations

import pytest

from src.core.delta_engine import DeltaEngine, compute_hash

# ─── compute_hash ─────────────────────────────────────────


def test_compute_hash_deterministic():
    p = {"sell_now_price": 100_000, "volume_7d": 10, "sold_count": 3}
    h1 = compute_hash(p)
    h2 = compute_hash(p)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_compute_hash_key_order_independent():
    a = {"sell_now_price": 100_000, "volume_7d": 10, "sold_count": 3}
    b = {"sold_count": 3, "volume_7d": 10, "sell_now_price": 100_000}
    assert compute_hash(a) == compute_hash(b)


def test_compute_hash_ignores_extra_fields():
    a = {"sell_now_price": 100_000, "volume_7d": 10, "sold_count": 3}
    b = {**a, "name": "something", "brand": "Nike"}
    assert compute_hash(a) == compute_hash(b)


def test_compute_hash_changes_on_price():
    a = {"sell_now_price": 100_000, "volume_7d": 10, "sold_count": 3}
    b = {"sell_now_price": 110_000, "volume_7d": 10, "sold_count": 3}
    assert compute_hash(a) != compute_hash(b)


def test_compute_hash_changes_on_volume():
    a = {"sell_now_price": 100_000, "volume_7d": 10, "sold_count": 3}
    b = {"sell_now_price": 100_000, "volume_7d": 15, "sold_count": 3}
    assert compute_hash(a) != compute_hash(b)


# ─── DeltaEngine ──────────────────────────────────────────


@pytest.fixture
async def engine(tmp_path):
    db_path = str(tmp_path / "delta.db")
    eng = DeltaEngine(db_path)
    await eng.init()
    return eng


def _mk(pid: int, price: int, vol: int, sold: int = 0) -> dict:
    return {
        "kream_product_id": pid,
        "sell_now_price": price,
        "volume_7d": vol,
        "sold_count": sold,
    }


async def test_init_creates_table(tmp_path):
    db_path = str(tmp_path / "t.db")
    eng = DeltaEngine(db_path)
    await eng.init()
    # 재호출 안전 (IF NOT EXISTS)
    await eng.init()
    assert await eng.pending_count() == 0


async def test_detect_changes_new_products(engine):
    current = [_mk(101, 100_000, 10), _mk(202, 200_000, 5)]
    changed, stale = await engine.detect_changes(current)
    # 모두 신규 → 전부 반환
    assert len(changed) == 2
    assert stale == []


async def test_mark_snapshot_then_no_change(engine):
    current = [_mk(101, 100_000, 10), _mk(202, 200_000, 5)]
    await engine.detect_changes(current)
    await engine.mark_snapshot(current)

    # 재비교 → 변경 0
    changed, stale = await engine.detect_changes(current)
    assert changed == []
    assert stale == []
    assert await engine.pending_count() == 2


async def test_price_change_detected(engine):
    initial = [_mk(101, 100_000, 10)]
    await engine.detect_changes(initial)
    await engine.mark_snapshot(initial)

    updated = [_mk(101, 110_000, 10)]
    changed, _ = await engine.detect_changes(updated)
    assert len(changed) == 1
    assert changed[0]["sell_now_price"] == 110_000


async def test_volume_change_detected(engine):
    initial = [_mk(101, 100_000, 10)]
    await engine.mark_snapshot(initial)

    updated = [_mk(101, 100_000, 20)]
    changed, _ = await engine.detect_changes(updated)
    assert len(changed) == 1


async def test_stale_detection(engine):
    initial = [_mk(101, 100_000, 10), _mk(202, 200_000, 5)]
    await engine.mark_snapshot(initial)

    # 202 가 현재 목록에서 사라짐
    current = [_mk(101, 100_000, 10)]
    changed, stale = await engine.detect_changes(current)
    assert changed == []  # 101 는 안 바뀜
    assert stale == [202]


async def test_mixed_new_changed_unchanged(engine):
    initial = [_mk(101, 100_000, 10), _mk(202, 200_000, 5)]
    await engine.mark_snapshot(initial)

    current = [
        _mk(101, 100_000, 10),      # unchanged
        _mk(202, 210_000, 5),        # changed (price)
        _mk(303, 50_000, 2),         # new
    ]
    changed, stale = await engine.detect_changes(current)
    changed_pids = {c["kream_product_id"] for c in changed}
    assert changed_pids == {202, 303}
    assert stale == []


async def test_forget_removes_snapshot(engine):
    initial = [_mk(101, 100_000, 10), _mk(202, 200_000, 5)]
    await engine.mark_snapshot(initial)
    assert await engine.pending_count() == 2

    await engine.forget([202])
    assert await engine.pending_count() == 1

    # 202 재등장 시 신규로 감지되어야 함
    current = [_mk(202, 200_000, 5)]
    changed, _ = await engine.detect_changes(current)
    assert len(changed) == 1
    assert changed[0]["kream_product_id"] == 202


async def test_invalid_pid_skipped(engine):
    current = [
        {"sell_now_price": 100_000, "volume_7d": 10},  # pid 없음
        _mk(101, 100_000, 10),
    ]
    changed, _ = await engine.detect_changes(current)
    assert len(changed) == 1
    assert changed[0]["kream_product_id"] == 101


async def test_product_id_string_accepted(engine):
    # product_id 가 문자열로 들어와도 int 로 정규화
    current = [{"product_id": "101", "sell_now_price": 100_000, "volume_7d": 10}]
    changed, _ = await engine.detect_changes(current)
    assert len(changed) == 1
    await engine.mark_snapshot(current)

    # 재비교 → 변경 0
    changed2, _ = await engine.detect_changes(current)
    assert changed2 == []


async def test_custom_hash_fields(tmp_path):
    db_path = str(tmp_path / "custom.db")
    eng = DeltaEngine(db_path, hash_fields=("sell_now_price",))
    await eng.init()

    # volume 은 해시 대상 아님
    a = [{"kream_product_id": 1, "sell_now_price": 100, "volume_7d": 1}]
    b = [{"kream_product_id": 1, "sell_now_price": 100, "volume_7d": 999}]
    await eng.mark_snapshot(a)
    changed, _ = await eng.detect_changes(b)
    assert changed == []  # volume 바뀌어도 변화 없음
