"""CheckpointStore 테스트 (Phase 2.3a)."""

from __future__ import annotations

import time

import pytest

from src.core.checkpoint_store import CheckpointStore
from src.core.event_bus import (
    AlertSent,
    CandidateMatched,
    CatalogDumped,
    ProfitFound,
)


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "ckpt.db"
    s = CheckpointStore(str(db_path))
    await s.init()
    yield s
    await s.close()


async def test_init_is_idempotent(tmp_path):
    db_path = tmp_path / "ckpt.db"
    s = CheckpointStore(str(db_path))
    await s.init()
    await s.init()  # 두 번 호출해도 에러 없어야 한다
    await s.close()


async def test_record_returns_id_and_persists(store):
    event = CatalogDumped(source="musinsa", product_count=42, dumped_at=123.45)
    ckpt_id = await store.record(event, consumer="orchestrator.match_loop")
    assert isinstance(ckpt_id, int)
    assert ckpt_id > 0

    pending = await store.pending()
    assert len(pending) == 1
    row = pending[0]
    assert row["id"] == ckpt_id
    assert row["event_type"] == "CatalogDumped"
    assert row["payload"] == {
        "source": "musinsa",
        "product_count": 42,
        "dumped_at": 123.45,
    }
    assert row["consumer"] == "orchestrator.match_loop"


async def test_record_requires_dataclass_instance(store):
    with pytest.raises(TypeError):
        await store.record("not a dataclass", consumer="x")
    with pytest.raises(TypeError):
        await store.record(CatalogDumped, consumer="x")  # 클래스 자체는 불가


async def test_replay_round_trip(store):
    e1 = CatalogDumped(source="nike", product_count=10, dumped_at=1.0)
    e2 = CandidateMatched(
        source="nike",
        kream_product_id=999,
        model_no="DD1391-100",
        retail_price=159000,
        size="270",
        url="https://nike.com/x",
    )
    id1 = await store.record(e1, consumer="matcher")
    id2 = await store.record(e2, consumer="matcher")

    replayed = [(cid, ev) async for cid, ev in store.replay("matcher")]
    assert len(replayed) == 2
    assert replayed[0] == (id1, e1)
    assert replayed[1] == (id2, e2)


async def test_mark_consumed_excludes_from_pending(store):
    e = ProfitFound(
        source="kasina",
        kream_product_id=1,
        model_no="X",
        size="270",
        retail_price=100000,
        kream_sell_price=150000,
        net_profit=30000,
        roi=0.3,
        signal="BUY",
        volume_7d=10,
        url="https://k.x",
    )
    cid = await store.record(e, consumer="alerter")
    assert len(await store.pending("alerter")) == 1

    await store.mark_consumed(cid)
    assert await store.pending("alerter") == []

    # replay 도 비어야 함
    items = [x async for x in store.replay("alerter")]
    assert items == []


async def test_pending_filters_by_consumer(store):
    e1 = CatalogDumped(source="a", product_count=1, dumped_at=1.0)
    e2 = CatalogDumped(source="b", product_count=2, dumped_at=2.0)
    await store.record(e1, consumer="c1")
    await store.record(e2, consumer="c2")

    assert len(await store.pending("c1")) == 1
    assert len(await store.pending("c2")) == 1
    assert len(await store.pending()) == 2


async def test_purge_consumed_respects_age(store):
    e = AlertSent(alert_id=7, kream_product_id=1, signal="BUY", fired_at=0.0)
    cid = await store.record(e, consumer="followup")
    await store.mark_consumed(cid)

    # 방금 소비 → 아직 안 지워짐
    res = await store.purge_consumed(older_than_sec=3600)
    assert res["deleted"] == 0
    assert len(await store.pending()) == 0  # consumed지만 행은 존재

    # 미래 기준으로 purge → 삭제됨
    res = await store.purge_consumed(older_than_sec=-1)
    assert res["deleted"] == 1

    # 재삽입 후 기본값에도 안 지워짐
    cid2 = await store.record(e, consumer="followup")
    await store.mark_consumed(cid2)
    res = await store.purge_consumed()  # 기본 86400
    assert res["deleted"] == 0


async def test_purge_consumed_pending_ttl_marks_failed(store):
    """pending_ttl_sec 초과 pending/deferred → failed 로 전환."""
    e = CatalogDumped(source="x", product_count=1, dumped_at=1.0)
    cid = await store.record(e, consumer="c")
    # pending_ttl_sec=-1 → 무조건 초과
    res = await store.purge_consumed(pending_ttl_sec=-1)
    assert res["failed_stale"] == 1
    # 이제 pending 에서 제외 (status=failed)
    assert await store.pending("c") == []
    # 행은 여전히 존재 (감사용)
    db = store._require_db()
    cur = await db.execute(
        "SELECT status, last_reason FROM event_checkpoint WHERE id = ?",
        (cid,),
    )
    row = await cur.fetchone()
    await cur.close()
    assert row["status"] == "failed"
    assert row["last_reason"] == "pending_ttl_exceeded"


async def test_mark_deferred_keeps_in_pending(store):
    e = CatalogDumped(source="x", product_count=1, dumped_at=1.0)
    cid = await store.record(e, consumer="c")
    await store.mark_deferred(cid, "throttle_exhausted")
    pending = await store.pending("c")
    assert len(pending) == 1
    assert pending[0]["status"] == "deferred"
    assert pending[0]["last_reason"] == "throttle_exhausted"


async def test_replay_attempts_exceeded_marks_failed(store):
    e = CatalogDumped(source="x", product_count=1, dumped_at=1.0)
    cid = await store.record(e, consumer="c")
    # attempts 를 3으로
    for _ in range(3):
        await store.increment_attempts(cid)
    yielded = [x async for x in store.replay("c")]
    assert yielded == []  # attempts 초과 → skip
    assert await store.pending("c") == []  # failed 처리
    db = store._require_db()
    cur = await db.execute(
        "SELECT status FROM event_checkpoint WHERE id = ?", (cid,)
    )
    row = await cur.fetchone()
    await cur.close()
    assert row["status"] == "failed"


async def test_unknown_event_type_skipped_on_replay(store):
    """알 수 없는 event_type 은 해당 row 만 failed 처리 + skip. 나머지는 계속."""
    # 수동으로 잘못된 event_type 삽입
    db = store._require_db()
    await db.execute(
        "INSERT INTO event_checkpoint "
        "(event_type, payload, created_at, consumed_at, consumer, status) "
        "VALUES (?, ?, ?, NULL, ?, 'pending')",
        ("NotAnEvent", "{}", time.time(), "bad"),
    )
    await db.commit()

    # 정상 row 도 하나 같은 consumer 로
    ok = CatalogDumped(source="ok", product_count=1, dumped_at=1.0)
    ok_id = await store.record(ok, consumer="bad")

    yielded = [x async for x in store.replay("bad")]
    # 알 수 없는 타입은 skip, 정상 건은 통과
    assert len(yielded) == 1
    assert yielded[0][0] == ok_id

    # 잘못된 row 는 failed 로 전환됨 → pending() 에서 제외
    remaining = await store.pending("bad")
    remaining_types = [r["event_type"] for r in remaining]
    assert "NotAnEvent" not in remaining_types


async def test_record_without_init_raises(tmp_path):
    s = CheckpointStore(str(tmp_path / "x.db"))
    e = CatalogDumped(source="x", product_count=1, dumped_at=1.0)
    with pytest.raises(RuntimeError):
        await s.record(e, consumer="c")
