"""실송신 정의 분리 + 로컬 드롭 폭주 가드 (2026-07-25).

## 왜 둘이 같은 배포 단위인가
`kream_api_calls` 에는 실제로 크림에 나간 요청과 로컬에서 끝난 스킵(500-스킵리스트,
`status=599`)이 섞여 있었고, **둘 다 하드캡을 소비**했다. 2026-05-02 하루 10,000행
중 3,917행(39%)이 로컬 드롭이었다 — 스킵리스트를 넣은 목적("cap 낭비 방지")을
절반 상쇄한 셈이다.

그런데 599 를 캡에서 빼기만 하면 **하드캡이 로컬 루프에 주던 (우연한) 제동이
사라진다**. 네트워크 0인데 CPU·DB·스케줄러만 태우는 폭주가 가능해진다.
실측 근거: `/api/p/options/display` 한 엔드포인트에 로컬 드롭이 delta_light 6,931 +
price_refresh 3,856 건 집중돼 있었다. 그래서 폭주 가드를 **같은 배포 단위**로 넣는다.

⚠️ 폭주 가드는 **크림 서킷이 아니다**. 크림이 응답한 게 아니라 우리가 안 보낸
것이므로 전역 크림 차단으로 오인하면 안 된다 — 해당 job/path 만 멈춘다.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core import kream_budget as kb

# 오늘 실측한 하루치 — 회귀의 기준점
INCIDENT_DAY_TOTAL = 10000
INCIDENT_DAY_DROPS = 3917
INCIDENT_DAY_SENDS = INCIDENT_DAY_TOTAL - INCIDENT_DAY_DROPS


def _create_calls_table(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kream_api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            endpoint TEXT NOT NULL,
            method TEXT NOT NULL,
            status INTEGER,
            latency_ms INTEGER,
            purpose TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert(path: str, n: int, *, status, purpose: str = "delta_light") -> None:
    if n <= 0:
        return
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO kream_api_calls (ts, endpoint, method, status, latency_ms, purpose) "
        "VALUES (datetime('now'), '/api/p/options/display', 'GET', ?, 10, ?)",
        [(status, purpose)] * n,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def calls_db(tmp_path, monkeypatch):
    from src.config import settings

    path = str(tmp_path / "calls.db")
    _create_calls_table(path)
    monkeypatch.setattr(settings, "db_path", path, raising=True)
    return path


@pytest.fixture(autouse=True)
def _reset_guard():
    kb.reset_local_drop_guard()
    yield
    kb.reset_local_drop_guard()


# ---------------------------------------------------------------------------
# 1. 실송신 정의 — 로컬 드롭은 캡을 소비하지 않는다
# ---------------------------------------------------------------------------


async def test_hard_cap_count_excludes_local_drops(calls_db):
    """사고 당일 비율 그대로 넣고 캡 카운트를 본다."""
    _insert(calls_db, INCIDENT_DAY_SENDS, status=200)
    _insert(calls_db, INCIDENT_DAY_DROPS, status=kb.LOCAL_DROP_STATUS)

    assert await kb._count_last_24h() == INCIDENT_DAY_SENDS


async def test_timeout_rows_still_count_as_actual_send(calls_db):
    """status NULL 은 타임아웃 — **실제로 나갔다**. 드롭과 혼동하면 안 된다."""
    _insert(calls_db, 5, status=None)
    assert await kb._count_last_24h() == 5


async def test_real_5xx_still_counts(calls_db):
    """진짜 5xx 응답은 실송신이다 (599 만 로컬 드롭 표식)."""
    _insert(calls_db, 3, status=500)
    _insert(calls_db, 2, status=503)
    _insert(calls_db, 7, status=kb.LOCAL_DROP_STATUS)
    assert await kb._count_last_24h() == 5


async def test_background_softcap_also_excludes_local_drops(calls_db):
    """소프트캡 소비량도 같은 정의를 쓴다 — 안 그러면 램프가 드롭으로 소진된다."""
    _insert(calls_db, 40, status=200, purpose="bootstrap_light")
    _insert(calls_db, 60, status=kb.LOCAL_DROP_STATUS, purpose="bootstrap_light:blacklist_500")

    total, background = await kb._count_24h_total_and_background()
    assert (total, background) == (40, 40)
    assert await kb.background_allowance() == 60  # canary 100 - 실소비 40


async def test_actual_send_where_is_the_single_definition():
    """캡·대시보드·리포트가 공유하는 상수 — 문자열이 갈라지면 화면마다 숫자가 다르다."""
    assert kb.LOCAL_DROP_STATUS == 599
    assert "599" in kb.ACTUAL_SEND_WHERE
    assert "status IS NULL" in kb.ACTUAL_SEND_WHERE

    import src.dashboard.queries as dashboard_queries

    assert dashboard_queries.ACTUAL_SEND_WHERE is kb.ACTUAL_SEND_WHERE


# ---------------------------------------------------------------------------
# 2. 폭주 가드 — 599 를 캡에서 뺀 대가를 메운다
# ---------------------------------------------------------------------------


def test_streak_limit_stops_a_stuck_path():
    """동일 (purpose, endpoint) 연속 드롭이 임계에 닿으면 중단."""
    for _ in range(kb.LOCAL_DROP_STREAK_LIMIT - 1):
        kb.record_local_drop("delta_light", "/api/p/options/display")

    with pytest.raises(kb.KreamLocalDropStorm, match="연속"):
        kb.record_local_drop("delta_light", "/api/p/options/display")


def test_actual_send_breaks_the_streak():
    """중간에 진짜 송신이 성공하면 그 경로는 갇힌 게 아니다 — 카운터 리셋."""
    for _ in range(kb.LOCAL_DROP_STREAK_LIMIT - 1):
        kb.record_local_drop("delta_light", "/api/p/options/display")

    kb.record_actual_send("delta_light", "/api/p/options/display")

    kb.record_local_drop("delta_light", "/api/p/options/display")  # 예외 없음


def test_streak_is_tracked_per_path():
    """다른 엔드포인트의 드롭이 섞여 임계를 앞당기면 안 된다(오탐 방어)."""
    for i in range(kb.LOCAL_DROP_STREAK_LIMIT - 1):
        kb.record_local_drop("delta_light", f"/api/p/e/products/{i}")

    kb.record_local_drop("delta_light", "/api/p/options/display")  # 예외 없음


def test_window_limit_catches_storm_spread_across_paths():
    """경로를 바꿔가며 도는 폭주는 연속 카운터로 못 잡는다 — 창 카운터가 잡는다."""
    with pytest.raises(kb.KreamLocalDropStorm, match="초"):
        for i in range(kb.LOCAL_DROP_WINDOW_LIMIT):
            kb.record_local_drop("delta_light", f"/api/p/e/products/{i}")


def test_window_forgets_old_drops():
    """창 밖으로 나간 과거 드롭은 임계에 안 셈된다 — 장기 저빈도는 정상."""
    old = 1_000_000.0
    for i in range(kb.LOCAL_DROP_WINDOW_LIMIT - 1):
        kb.record_local_drop("delta_light", f"/old/{i}", now=old)

    # 창(5분) 을 훌쩍 넘긴 뒤 1건 — 과거분이 만료되므로 예외 없음
    kb.record_local_drop("delta_light", "/new/1", now=old + kb.LOCAL_DROP_WINDOW_SEC + 1)


async def test_storm_is_not_a_kream_circuit_trip(calls_db):
    """폭주 가드는 크림 차단과 **다른 축**이다 — 서킷을 건드리면 안 된다.

    크림이 응답한 게 아니라 우리가 안 보낸 것이라, 전역 크림 차단으로 오인하면
    멀쩡한 라이브 경로까지 manual_resume 전까지 죽는다.
    """
    with pytest.raises(kb.KreamLocalDropStorm):
        for _ in range(kb.LOCAL_DROP_STREAK_LIMIT):
            kb.record_local_drop("delta_light", "/api/p/options/display")

    assert await kb.is_circuit_tripped() is False
