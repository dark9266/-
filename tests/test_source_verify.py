import time

import aiosqlite
import pytest

from src.ops.source_verify import (
    SUPPORTED_SOURCES,
    ensure_dump_runs_schema,
    evaluate,
    judge_canary_present,
    judge_field_fill,
    judge_has_data,
    judge_pagination,
    judge_volume,
    last_dump_count,
    ledger_delta,
    ledger_unique_counts,
    load_canaries,
    record_dump_run,
)


def test_has_data_rejects_empty_even_when_fetch_succeeded():
    """200 이어도 상품 0 이면 실패 — 상태코드로 판정하지 않는다."""
    ok, reason = judge_has_data([])
    assert not ok
    assert "0건" in reason


def test_has_data_accepts_items_even_with_challenge_text():
    """challenge 문구가 섞여 있어도 데이터가 있으면 통과 — 오판 방지."""
    items = [{"model_no": "FV1920-001", "_raw": "please enable JavaScript"}]
    ok, _ = judge_has_data(items)
    assert ok


def test_canary_present_detects_missing_reference_product():
    items = [{"model_no": "AAA-111"}]
    ok, reason = judge_canary_present(items, ["FV1920-001"])
    assert not ok
    assert "FV1920-001" in reason


def test_canary_present_is_case_and_space_insensitive():
    items = [{"model_no": " fv1920-001 "}]
    ok, _ = judge_canary_present(items, ["FV1920-001"])
    assert ok


def test_field_fill_flags_low_rate():
    items = [{"model_no": "A", "price": 1000}, {"model_no": "B", "price": None}]
    ok, reason, rates = judge_field_fill(items, ["price"], min_rate=0.9)
    assert not ok
    assert rates["price"] == 0.5
    assert "price" in reason


def test_field_fill_rejects_boundary_rate_before_rounding():
    """8996/10000 = 0.8996 → 반올림하면 0.900 이 되어 90% 컷을 통과해버리는
    경계 버그. 원시 비율로 비교해야 실패로 잡힌다."""
    items = [{"p": 1}] * 8996 + [{"p": None}] * 1004
    ok, reason, rates = judge_field_fill(items, ["p"], min_rate=0.9)
    assert not ok
    assert rates["p"] == 0.9  # 표시값은 반올림돼도
    assert "p" in reason  # 판정은 실패해야 한다


def test_field_fill_accepts_exact_boundary_rate():
    """정확히 9000/10000 = 0.9 는 통과해야 한다."""
    items = [{"p": 1}] * 9000 + [{"p": None}] * 1000
    ok, _, rates = judge_field_fill(items, ["p"], min_rate=0.9)
    assert ok
    assert rates["p"] == 0.9


def test_pagination_detects_identical_consecutive_pages():
    page = [{"model_no": "A"}, {"model_no": "B"}]
    ok, reason = judge_pagination([page, list(page)])
    assert not ok
    assert "동일" in reason


def test_pagination_accepts_progressing_pages():
    ok, _ = judge_pagination([[{"model_no": "A"}], [{"model_no": "B"}]])
    assert ok


def test_pagination_flags_keyless_pages_as_unjudgeable_not_identical():
    """키가 전부 없는 두 페이지는 각각 {""} 로 정규화돼 "동일 집합"으로
    오판될 수 있다 — 판정 불능으로 실패 사유를 알려야 한다."""
    pages = [[{"other": 1}], [{"other": 2}]]
    ok, reason = judge_pagination(pages)
    assert not ok
    assert "판정 불가" in reason


def test_pagination_still_accepts_normal_progressing_pages():
    """키가 정상적으로 있는 진행 페이지는 기존대로 통과한다(회귀 방지)."""
    ok, _ = judge_pagination([[{"model_no": "A"}], [{"model_no": "B"}]])
    assert ok


def test_volume_flags_sharp_drop():
    ok, reason = judge_volume(count=100, baseline=1000, max_drop=0.5)
    assert not ok
    assert "급감" in reason


def test_volume_passes_when_no_baseline():
    """기준선이 없으면(첫 실행) 급감 판정을 하지 않는다 — 거짓 경보 방지."""
    ok, _ = judge_volume(count=10, baseline=0, max_drop=0.5)
    assert ok


def test_volume_passes_when_baseline_is_none():
    """`last_dump_count` 가 테이블 부재로 None 을 돌려줘도 스킵되어야 한다."""
    ok, _ = judge_volume(count=10, baseline=None, max_drop=0.5)
    assert ok


def test_canary_fixture_loads_and_has_required_keys():
    specs = load_canaries()
    assert specs, "fixture 가 비어 있다"
    for source, spec in specs.items():
        assert isinstance(spec["canary_model_numbers"], list), source
        assert isinstance(spec["required_fields"], list), source
        assert 0 < spec["min_fill_rate"] <= 1.0, source
        assert 0 < spec["max_drop"] < 1.0, source


def test_canary_fixture_sources_match_supported_sources_exactly():
    """fixture 에 적힌 소싱처 집합은 `SUPPORTED_SOURCES` 와 정확히 같아야
    한다 — 항목을 지워도(부분집합만 보는 검사와 달리) 잡힌다."""
    assert set(load_canaries()) == SUPPORTED_SOURCES


def test_supported_sources_are_all_in_adapter_registry():
    """`SUPPORTED_SOURCES` 는 실제 레지스트리에 등록된 이름이어야 한다 — 오타 방지.
    runtime import(크림 watcher 로드 부작용)는 이 테스트에만 한정한다."""
    from src.core.runtime import _ADAPTER_REGISTRY

    registered = {name for name, _cls in _ADAPTER_REGISTRY}
    assert SUPPORTED_SOURCES <= registered


@pytest.fixture
async def dump_db(tmp_path):
    path = str(tmp_path / "t.db")
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE catalog_dump_items (source TEXT, model_no TEXT, name TEXT, url TEXT)"
        )
        await db.executemany(
            "INSERT INTO catalog_dump_items VALUES (?, ?, ?, ?)",
            [("29cm", "A", "n", "u"), ("29cm", "B", "n", "u"), ("nike", "C", "n", "u")],
        )
        await db.commit()
    return path


async def test_ledger_unique_counts_reads_per_source_rows(dump_db):
    counts = await ledger_unique_counts(dump_db)
    assert counts["29cm"] == 2
    assert counts["nike"] == 1


async def test_ledger_unique_counts_returns_empty_when_table_missing(tmp_path):
    """테이블이 없는 새 DB 여도 죽지 않는다 — 첫 실행 방어."""
    assert await ledger_unique_counts(str(tmp_path / "empty.db")) == {}


async def test_ledger_unique_counts_propagates_db_error_on_corrupted_file(tmp_path):
    """확인된 '테이블 부재'가 아닌 DB 오류(파일 손상)는 삼키지 않고 전파한다
    — 판정 불능을 조용히 통과({})로 둔갑시키지 않는다."""
    path = tmp_path / "corrupted.db"
    path.write_text("not a database")
    with pytest.raises(aiosqlite.Error):
        await ledger_unique_counts(str(path))


async def test_last_dump_count_reads_most_recent_row(tmp_path):
    path = str(tmp_path / "runs.db")
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE catalog_dump_runs (source TEXT, product_count INTEGER, finished_at REAL)"
        )
        await db.executemany(
            "INSERT INTO catalog_dump_runs VALUES (?, ?, ?)",
            [("29cm", 900, 100.0), ("29cm", 499, 200.0), ("nike", 50, 150.0)],
        )
        await db.commit()
    assert await last_dump_count(path, "29cm") == 499
    assert await last_dump_count(path, "nike") == 50


async def test_last_dump_count_returns_none_when_table_missing(tmp_path):
    """`catalog_dump_runs` 가 없으면 None — 급감 판정 스킵 신호."""
    assert await last_dump_count(str(tmp_path / "empty.db"), "29cm") is None


async def test_last_dump_count_propagates_db_error_on_corrupted_file(tmp_path):
    path = tmp_path / "corrupted2.db"
    path.write_text("not a database")
    with pytest.raises(aiosqlite.Error):
        await last_dump_count(str(path), "29cm")


def test_evaluate_passes_healthy_source():
    spec = {
        "canary_model_numbers": ["A"],
        "required_fields": ["model_no", "name"],
        "min_fill_rate": 0.9,
        "max_drop": 0.5,
    }
    items = [{"model_no": "A", "name": "x"}, {"model_no": "B", "name": "y"}]
    v = evaluate("29cm", items, spec, baseline=2)
    assert v.ok
    assert v.reasons == []
    assert v.metrics["count"] == 2


def test_evaluate_collects_every_failure_reason():
    """실패 사유를 첫 건에서 끊지 않고 전부 모은다 — 한 번에 고치게."""
    spec = {
        "canary_model_numbers": ["MISSING"],
        "required_fields": ["price"],
        "min_fill_rate": 0.9,
        "max_drop": 0.5,
    }
    items = [{"model_no": "A", "price": None}]
    v = evaluate("29cm", items, spec, baseline=1000)
    assert not v.ok
    assert len(v.reasons) >= 3  # canary 미검출 + 필드 충실도 + 급감


# ---------------------------------------------------------------------------
# Task 4a — catalog_dump_runs 스냅샷 테이블 (기준선 소스)
# ---------------------------------------------------------------------------


async def test_ensure_dump_runs_schema_is_idempotent(tmp_path):
    """2회 호출해도 에러 없이 통과한다."""
    path = str(tmp_path / "runs_idempotent.db")
    await ensure_dump_runs_schema(path)
    await ensure_dump_runs_schema(path)  # 재호출도 안전해야 한다


async def test_record_dump_run_then_last_dump_count_returns_it(tmp_path):
    path = str(tmp_path / "runs_record.db")
    await record_dump_run(path, "29cm", 499)
    assert await last_dump_count(path, "29cm") == 499


async def test_record_dump_run_multiple_rows_returns_latest(tmp_path):
    """같은 source 로 여러 번 기록하면 가장 최근 값을 돌려줘야 한다."""
    path = str(tmp_path / "runs_multi.db")
    await record_dump_run(path, "29cm", 900)
    await record_dump_run(path, "29cm", 499)
    assert await last_dump_count(path, "29cm") == 499


# ---------------------------------------------------------------------------
# Task 4b — 원장 델타 조회
# ---------------------------------------------------------------------------


@pytest.fixture
async def ledger_db(tmp_path):
    path = str(tmp_path / "ledger.db")
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE catalog_dump_items (source TEXT, model_no TEXT, name TEXT,"
            " url TEXT, last_seen_at REAL)"
        )
        await db.executemany(
            "INSERT INTO catalog_dump_items VALUES (?, ?, ?, ?, ?)",
            [
                ("29cm", "OLD-1", "old", "u-old", 100.0),
                ("29cm", "NEW-1", "new", "u-new", 200.0),
                ("nike", "OTHER-1", "other-source", "u-other", 200.0),
            ],
        )
        await db.commit()
    return path


async def test_ledger_delta_excludes_rows_before_since(ledger_db):
    items = await ledger_delta(ledger_db, "29cm", since=150.0)
    model_nos = {i["model_no"] for i in items}
    assert model_nos == {"NEW-1"}


async def test_ledger_delta_excludes_other_source_rows(ledger_db):
    items = await ledger_delta(ledger_db, "29cm", since=0.0)
    assert all(i["model_no"] != "OTHER-1" for i in items)


async def test_ledger_delta_returns_empty_when_table_missing(tmp_path):
    path = str(tmp_path / "no_ledger.db")
    assert await ledger_delta(path, "29cm", since=0.0) == []


# ---------------------------------------------------------------------------
# Task 4c — CLI (offline report + 원장 델타 live 검증)
#
# 가짜 어댑터는 `src/adapters/*` 를 흉내내되 크림 호출은 전혀 하지 않는다.
# `match_to_kream()` 은 실제 어댑터처럼 `dump_ledger.record_dump_item()` 을
# 거치지 않고, 원장(`catalog_dump_items`)에 **테스트가 직접 INSERT** 해
# "자기 정규화를 마친 뒤 원장에 쓴다"는 계약만 흉내낸다 — dump_ledger.py 는
# 손대지 않는다.
# ---------------------------------------------------------------------------


class _FakeAdapterOk:
    """덤프 raw 2건 → match_to_kream 이 원장에 2건(둘 다) 기록."""

    def __init__(self, db_path: str, source: str = "29cm") -> None:
        self._db_path = db_path
        self.source_name = source

    async def dump_catalog(self):
        return None, [{"raw_key": "R1"}, {"raw_key": "R2"}]

    async def match_to_kream(self, raw: list[dict]):
        now = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS catalog_dump_items"
                " (source TEXT, model_no TEXT, name TEXT, url TEXT, last_seen_at REAL)"
            )
            await db.executemany(
                "INSERT INTO catalog_dump_items VALUES (?, ?, ?, ?, ?)",
                [
                    (self.source_name, "A", "name-a", "u-a", now),
                    (self.source_name, "B", "name-b", "u-b", now),
                ],
            )
            await db.commit()
        return [], None


class _FakeAdapterFilteredOut:
    """덤프 raw 5건이지만 어댑터 필터에 전량 걸려 원장 0건."""

    def __init__(self, db_path: str, source: str = "29cm") -> None:
        self._db_path = db_path
        self.source_name = source

    async def dump_catalog(self):
        return None, [{"raw_key": f"R{i}"} for i in range(5)]

    async def match_to_kream(self, raw: list[dict]):
        return [], None  # 필터에 전부 걸려 원장에 아무것도 안 씀


class _FakeAdapterMatchRaises:
    """덤프는 되나 매칭 단계가 예외로 깨진다."""

    def __init__(self, db_path: str, source: str = "29cm") -> None:
        self._db_path = db_path
        self.source_name = source

    async def dump_catalog(self):
        return None, [{"raw_key": "R1"}, {"raw_key": "R2"}, {"raw_key": "R3"}]

    async def match_to_kream(self, raw: list[dict]):
        raise RuntimeError("매칭 파서가 깨졌다")


_MINIMAL_SPEC = {
    "canary_model_numbers": [],
    "required_fields": [],
    "min_fill_rate": 0.9,
    "max_drop": 0.5,
}


async def test_build_offline_report_lists_fixture_sources(tmp_path):
    """offline 모드는 fixture 소싱처 전부를 나열한다(네트워크 0)."""
    from scripts.verify_sources import build_offline_report

    report = await build_offline_report(str(tmp_path / "none.db"))
    names = {row["source"] for row in report["sources"]}
    assert names == SUPPORTED_SOURCES


async def test_verify_source_live_ok_with_raw_and_ledger_counts(tmp_path):
    from scripts.verify_sources import verify_source_live

    db_path = str(tmp_path / "live_ok.db")
    adapter = _FakeAdapterOk(db_path)
    verdict = await verify_source_live(adapter, "29cm", _MINIMAL_SPEC, db_path)
    assert verdict.ok
    assert verdict.metrics["raw_count"] == 2
    assert verdict.metrics["ledger_count"] == 2


async def test_verify_source_live_fails_when_raw_present_but_ledger_empty(tmp_path):
    """덤프는 됐으나(raw 5건) 원장이 0건 — 필터 전량 걸림을 별도 사유로 알린다."""
    from scripts.verify_sources import verify_source_live

    db_path = str(tmp_path / "live_filtered.db")
    adapter = _FakeAdapterFilteredOut(db_path)
    verdict = await verify_source_live(adapter, "29cm", _MINIMAL_SPEC, db_path)
    assert not verdict.ok
    assert verdict.metrics["raw_count"] == 5
    assert verdict.metrics["ledger_count"] == 0
    assert any("전량 걸림" in r for r in verdict.reasons)


async def test_verify_source_live_fails_when_match_to_kream_raises(tmp_path):
    """매칭 단계 예외도 실패 사유로 잡는다(덤프는 됐는데 매칭이 깨진 경우)."""
    from scripts.verify_sources import verify_source_live

    db_path = str(tmp_path / "live_raise.db")
    adapter = _FakeAdapterMatchRaises(db_path)
    verdict = await verify_source_live(adapter, "29cm", _MINIMAL_SPEC, db_path)
    assert not verdict.ok
    assert any("매칭" in r for r in verdict.reasons)


async def test_run_live_unknown_source_fails_loudly(tmp_path, capsys):
    """`--source <미지명>` 은 공허 통과가 아니라 실패로 끝나야 한다.

    2026-07-28 라이브 실행에서 spec 없는 소싱처 4곳(stussy 등)이 루프 전체
    skip → "실패 0곳" 으로 통과 위장됐다. 미지명은 즉시 실패 + 가용 목록 안내.
    """
    from scripts.verify_sources import run_live

    failures = await run_live(str(tmp_path / "unknown.db"), only="no_such_source")
    assert failures >= 1
    out = capsys.readouterr().out
    assert "no_such_source" in out
    assert "spec" in out or "미지" in out
