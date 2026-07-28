"""송신 토큰 예약 API (2026-07-25, 코덱스 S 자문 #5 — 조각 (a) API 계층).

## 왜
현재는 **응답 후** `record_call` 로 기록한다. 그래서 두 구멍이 있다:
1. 송신 직후 ~ 기록 커밋 전 크래시 → 실제로 크림에 나갔는데 캡은 모른다.
2. 여러 프로세스가 동시에 캡을 조회하면 같은 잔여량을 두 번 본다(check-then-send).
   배치 잠금은 background 끼리만 직렬화하고 라이브 경로와의 경쟁은 못 막는다.

→ **송신 직전에 durable 토큰을 예약**하고, 캡은 "확정 송신 + 미결 예약" 을 함께 센다.

## 환불 정책 (이 파일이 고정하는 핵심 계약)
"송신하지 않았음이 **확실한** 경우" 만 취소한다. 크래시로 결론을 모르는 예약은
**소비된 것으로 둔다** — 약간 과대계수하는 쪽이 하드캡 초과보다 안전하다.

⚠️ 이 조각은 API 계층만이다. raw 경로 배선·크래시 reconciliation 은 다음 조각이라,
지금은 아무도 호출하지 않는다 = 기존 동작 불변.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from src.core import kream_budget as kb


@pytest.fixture
def token_db(tmp_path, monkeypatch):
    """빈 tmp DB — 스키마는 API 가 스스로 만든다(첫 호출 시 CREATE IF NOT EXISTS)."""
    from src.config import settings

    path = str(tmp_path / "tokens.db")
    sqlite3.connect(path).close()
    monkeypatch.setattr(settings, "db_path", path, raising=True)
    return path


def _backdate(path: str, token_id: int, seconds_ago: float) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE kream_send_token SET reserved_at = ? WHERE id = ?",
        (time.time() - seconds_ago, token_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. 예약 → 미결은 소비로 센다
# ---------------------------------------------------------------------------


async def test_reservation_counts_as_consumed_until_settled(token_db):
    assert await kb.count_open_tokens_24h() == 0

    token = await kb.reserve_send_token("bootstrap_light", "/api/p/e/products/1")

    assert token > 0
    assert await kb.count_open_tokens_24h() == 1, "미결 예약은 캡 소비로 세야 한다"


async def test_settled_as_sent_stops_counting(token_db):
    """확정 송신은 `kream_api_calls` 가 세므로 토큰 쪽에서 빼야 이중계산이 없다."""
    token = await kb.reserve_send_token("bootstrap_light", "/x")
    await kb.settle_send_token(token, sent=True)

    assert await kb.count_open_tokens_24h() == 0


async def test_settled_as_not_sent_is_refunded(token_db):
    """요청 자체를 안 보낸 게 확실할 때만 환불된다."""
    token = await kb.reserve_send_token("bootstrap_light", "/x")
    await kb.settle_send_token(token, sent=False)

    assert await kb.count_open_tokens_24h() == 0


# ---------------------------------------------------------------------------
# 2. 크래시 = 결론 없음 → **환불하지 않는다** (이 조각의 존재 이유)
# ---------------------------------------------------------------------------


async def test_orphaned_reservation_is_not_auto_refunded(token_db):
    """settle 을 못 부르고 죽은 예약은 계속 소비로 남는다 — 보수적 오차."""
    await kb.reserve_send_token("bootstrap_light", "/x")  # settle 없음 = 크래시

    assert await kb.count_open_tokens_24h() == 1
    # 시간이 좀 지나도 자동 환불되지 않는다
    assert await kb.count_open_tokens_24h() == 1


async def test_orphan_ages_out_of_the_24h_window(token_db):
    """다만 24h 창을 벗어나면 자연히 빠진다 — 영구 누적은 아니다."""
    token = await kb.reserve_send_token("bootstrap_light", "/x")
    _backdate(token_db, token, 86400.0 + 60)

    assert await kb.count_open_tokens_24h() == 0


async def test_settle_is_idempotent_and_does_not_resurrect(token_db):
    """이미 결론 난 토큰을 다시 settle 해도 상태가 뒤집히지 않는다."""
    token = await kb.reserve_send_token("bootstrap_light", "/x")
    await kb.settle_send_token(token, sent=True)
    await kb.settle_send_token(token, sent=False)  # 늦게 온 취소

    conn = sqlite3.connect(token_db)
    status = conn.execute(
        "SELECT status FROM kream_send_token WHERE id = ?", (token,)
    ).fetchone()[0]
    conn.close()
    assert status == kb.TOKEN_SENT, "확정 송신이 나중 취소로 뒤집히면 캡이 샌다"


async def test_settling_unknown_token_is_harmless(token_db):
    await kb.settle_send_token(0, sent=True)
    await kb.settle_send_token(-1, sent=False)
    await kb.settle_send_token(99999, sent=True)
    assert await kb.count_open_tokens_24h() == 0


# ---------------------------------------------------------------------------
# 3. 동시 예약 — 같은 잔여량을 두 번 보지 않는다
# ---------------------------------------------------------------------------


async def test_concurrent_reservations_each_consume_one(token_db):
    """N 개 예약이 서로 덮어쓰지 않고 각각 1을 소비한다."""
    import asyncio

    tokens = await asyncio.gather(
        *[kb.reserve_send_token("bootstrap_light", f"/x/{i}") for i in range(10)]
    )

    assert len(set(tokens)) == 10, "토큰 id 가 겹쳤다"
    assert await kb.count_open_tokens_24h() == 10


async def test_retry_and_session_init_each_take_a_token(token_db):
    """재시도·세션init 도 각각 실제 송신이므로 각각 1 토큰이다."""
    await kb.reserve_send_token("bootstrap_light:session_init", "/")
    await kb.reserve_send_token("bootstrap_light", "/api/p/e/products/1")
    await kb.reserve_send_token("bootstrap_light", "/api/p/e/products/1")  # 재시도

    assert await kb.count_open_tokens_24h() == 3


# ---------------------------------------------------------------------------
# 4. 정리 — 미결은 절대 안 지운다
# ---------------------------------------------------------------------------


async def test_purge_removes_only_settled_old_tokens(token_db):
    old_sent = await kb.reserve_send_token("p", "/old-sent")
    await kb.settle_send_token(old_sent, sent=True)
    _backdate(token_db, old_sent, 8 * 86400.0)

    old_open = await kb.reserve_send_token("p", "/old-open")  # 미결
    _backdate(token_db, old_open, 8 * 86400.0)

    recent_sent = await kb.reserve_send_token("p", "/recent-sent")
    await kb.settle_send_token(recent_sent, sent=True)

    removed = await kb.purge_settled_tokens()

    conn = sqlite3.connect(token_db)
    remaining = {r[0] for r in conn.execute("SELECT id FROM kream_send_token")}
    conn.close()

    assert removed == 1
    assert old_sent not in remaining
    assert old_open in remaining, "미결 예약을 지우면 캡이 새고 증거가 사라진다"
    assert recent_sent in remaining


# ---------------------------------------------------------------------------
# 5. 배선 (조각 b) — 캡이 미결 예약을 소비로 반영한다
# ---------------------------------------------------------------------------


async def test_hard_cap_counts_open_reservations(token_db):
    """"보냈는데 기록 전에 죽은" 호출이 캡에서 사라지지 않는다 — 예약의 존재 이유."""
    conn = sqlite3.connect(token_db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kream_api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            endpoint TEXT NOT NULL, method TEXT NOT NULL,
            status INTEGER, latency_ms INTEGER, purpose TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO kream_api_calls (ts, endpoint, method, status, latency_ms, purpose) "
        "VALUES (datetime('now'), '/x', 'GET', 200, 10, 'p')",
        [()] * 5,
    )
    conn.commit()
    conn.close()

    assert await kb._count_last_24h() == 5

    await kb.reserve_send_token("p", "/x")  # 결론 미정 = 소비
    assert await kb._count_last_24h() == 6, "미결 예약이 캡에 반영돼야 한다"


async def test_settled_reservation_is_not_double_counted(token_db):
    """확정 송신은 원장이 센다 — 토큰까지 세면 이중계산이다."""
    conn = sqlite3.connect(token_db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kream_api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            endpoint TEXT NOT NULL, method TEXT NOT NULL,
            status INTEGER, latency_ms INTEGER, purpose TEXT
        );
        INSERT INTO kream_api_calls (ts, endpoint, method, status, latency_ms, purpose)
        VALUES (datetime('now'), '/x', 'GET', 200, 10, 'p');
        """
    )
    conn.commit()
    conn.close()

    token = await kb.reserve_send_token("p", "/x")
    await kb.settle_send_token(token, sent=True)

    assert await kb._count_last_24h() == 1, "원장 1건 + 정산된 토큰 = 1이어야 한다"


def test_hot_paths_reserve_before_sending():
    """세 송신 지점이 전부 예약을 거치는지 소스로 확인.

    한 곳이라도 빠지면 그 경로의 크래시 호출이 캡에서 사라진다.

    조각 c(원자화) 이후 정산은 별도 `settle_send_token(` 호출이 아니라
    `record_call(..., token_id=...)` 로 같은 커밋 안에서 끝난다 — 그래서
    이 테스트는 이제 `record_call(...token_id=` 조합의 개수를 센다.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "crawlers" / "kream.py"
    ).read_text(encoding="utf-8")

    assert src.count("reserve_send_token(") >= 3, "송신 지점 3곳(_request + raw 2)"
    # 예약한 만큼 원자 정산 호출이 있어야 한다(성공 1 + 예외 2 + raw 각 성공/예외 2×2)
    assert src.count("token_id=_token") + src.count("token_id=token") >= 5
    # 원자화 이후에는 hot path 에서 별도 settle_send_token 왕복이 사라져야 한다
    # (환불 결론 API 자체는 kream_budget.py 에 남아 있지만, 정산은 record_call
    # 이 대신한다).
    assert "await settle_send_token(" not in src


def test_local_drop_does_not_reserve_a_token():
    """로컬 스킵(599)은 네트워크로 안 나간다 — 예약하면 캡만 갉아먹는다.

    소스에서 드롭 분기가 `return None` 으로 **예약 전에** 끝나는지 확인.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "crawlers" / "kream.py"
    ).read_text(encoding="utf-8")

    drop_idx = src.index("_is_500_blacklisted(endpoint)")
    reserve_idx = src.index("reserve_send_token(purpose, endpoint)")
    between = src[drop_idx:reserve_idx]
    assert "return None" in between, "드롭이 예약보다 뒤에 있으면 캡이 샌다"


# ---------------------------------------------------------------------------
# 6. 조각 c — 기록↔정산 원자화 (record_call(token_id=...))
# ---------------------------------------------------------------------------


def _create_legacy_ledger_table(path: str) -> None:
    """`token_id` 컬럼이 **없는** (마이그레이션 이전) 원장 테이블을 만든다.

    실제 운영 DB 는 `src/models/database.py` 가 이 스키마로 만들어 두었을
    것이므로, `_ensure_api_calls_token_id_column` 이 ALTER 로 컬럼을 보태는
    경로를 검증하려면 일부러 컬럼 없이 만들어야 한다.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kream_api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            endpoint TEXT NOT NULL, method TEXT NOT NULL,
            status INTEGER, latency_ms INTEGER, purpose TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_ledger_row_with_token(path: str, token_id: int, endpoint: str = "/x") -> None:
    """`record_call` 을 거치지 않고 원장에 직접 행을 심는다 — 크래시 시뮬레이션.

    실제로는 `record_call(token_id=...)` 의 INSERT 가 커밋됐지만 뒤이은
    settle UPDATE 가 커밋되기 전에 프로세스가 죽은 상황을 재현한다.

    호출 시점 테이블에 `token_id` 컬럼이 아직 없을 수 있어(레거시 스키마로
    일부러 만든 테스트 fixture) 없으면 먼저 보태고 삽입한다 — 프로덕션에서는
    `_ensure_api_calls_token_id_column` 이 이 역할을 한다.
    """
    conn = sqlite3.connect(path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(kream_api_calls)")}
    if "token_id" not in cols:
        conn.execute("ALTER TABLE kream_api_calls ADD COLUMN token_id INTEGER")
    conn.execute(
        "INSERT INTO kream_api_calls (ts, endpoint, method, status, latency_ms, purpose, token_id) "
        "VALUES (datetime('now'), ?, 'GET', 200, 10, 'p', ?)",
        (endpoint, token_id),
    )
    conn.commit()
    conn.close()


def _token_status(path: str, token_id: int) -> str:
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT status FROM kream_send_token WHERE id = ?", (token_id,)
    ).fetchone()
    conn.close()
    return row[0]


async def test_atomic_record_call_inserts_ledger_and_settles_token(token_db):
    """`record_call(token_id=t)` 한 번으로 원장 1행 + 토큰 sent 가 함께 끝난다.

    (단일 커넥션/단일 커밋 여부는 코드 리뷰 몫 — 여기서는 관측 가능한 결과만
    고정한다: 원장에 token_id 가 박히고, 토큰이 reserved → sent 로 바뀐다.)
    """
    _create_legacy_ledger_table(token_db)

    token = await kb.reserve_send_token("p", "/atomic")
    await kb.record_call("/atomic", "GET", 200, 10, "p", token_id=token)

    conn = sqlite3.connect(token_db)
    row = conn.execute(
        "SELECT token_id FROM kream_api_calls WHERE endpoint = '/atomic'"
    ).fetchone()
    conn.close()

    assert row is not None and row[0] == token, "원장에 token_id 가 기록돼야 한다"
    assert _token_status(token_db, token) == kb.TOKEN_SENT
    assert await kb.count_open_tokens_24h() == 0, "정산됐으므로 미결에서 빠져야 한다"
    assert await kb._count_last_24h() == 1, "원장 1건만 세어야 한다(이중계산 없음)"


async def test_record_call_without_token_id_is_unchanged(token_db):
    """`token_id` 생략 시 기존 동작(테이블에 token_id 컬럼 없어도 정상 삽입).

    레거시(컬럼 없는) 스키마에서 **token_id 를 아예 건드리지 않는** 경로를
    검증한다 — 하위 호환 보존.
    """
    _create_legacy_ledger_table(token_db)

    await kb.record_call("/legacy", "GET", 200, 5, "p")

    conn = sqlite3.connect(token_db)
    row = conn.execute(
        "SELECT endpoint, status FROM kream_api_calls WHERE endpoint = '/legacy'"
    ).fetchone()
    conn.close()
    assert row == ("/legacy", 200)


async def test_reconcile_settles_crash_orphaned_ledger_match(token_db):
    """크래시 시뮬: 원장엔 있는데(=INSERT 커밋됨) 토큰은 reserved 로 남은 경우.

    `reconcile_send_tokens()` 가 매칭된 것만 sent 로 정산해 이중계산을 없앤다.
    """
    _create_legacy_ledger_table(token_db)
    # 레거시 테이블에는 token_id 컬럼이 없다 — ensure 함수가 ALTER 로 보태는
    # 경로까지 함께 검증한다.

    token = await kb.reserve_send_token("p", "/crash")
    _insert_ledger_row_with_token(token_db, token, "/crash")  # settle 없이 원장만 존재

    # 원자화 전이라면 원장 1 + 미결 예약 1 = 이중계산
    assert await kb._count_last_24h() == 2

    result = await kb.reconcile_send_tokens()

    assert result["settled"] == 1
    assert _token_status(token_db, token) == kb.TOKEN_SENT
    assert await kb._count_last_24h() == 1, "reconcile 후 이중계산이 사라져야 한다"


async def test_reconcile_never_refunds_unmatched_reserved(token_db):
    """원장 매칭이 없는 reserved 는 reconcile 후에도 그대로 소비로 남는다.

    "결론 모르면 소비" 환불 정책은 이 조각에서도 불변이다.
    """
    token = await kb.reserve_send_token("p", "/orphan")  # 원장 행 자체가 없음

    result = await kb.reconcile_send_tokens()

    assert result["still_open"] == 1
    assert _token_status(token_db, token) == kb.TOKEN_RESERVED
    assert await kb.count_open_tokens_24h() == 1, "매칭 없는 예약을 환불하면 캡이 샌다"


async def test_reconcile_does_not_disturb_already_sent_token(token_db):
    """이미 sent 로 결론 난 토큰은 reconcile 이 다시 건드리지 않는다.

    조건부 UPDATE(`WHERE status='reserved'`) 가 지켜지는지 확인.
    """
    _create_legacy_ledger_table(token_db)
    token = await kb.reserve_send_token("p", "/already-sent")
    await kb.settle_send_token(token, sent=True)

    conn = sqlite3.connect(token_db)
    settled_at_before = conn.execute(
        "SELECT settled_at FROM kream_send_token WHERE id = ?", (token,)
    ).fetchone()[0]
    conn.close()

    result = await kb.reconcile_send_tokens()

    conn = sqlite3.connect(token_db)
    settled_at_after = conn.execute(
        "SELECT settled_at FROM kream_send_token WHERE id = ?", (token,)
    ).fetchone()[0]
    conn.close()

    assert result["settled"] == 0, "이미 sent 인 토큰을 다시 세면 안 된다"
    assert settled_at_before == settled_at_after, "settled_at 이 갱신되면 안 된다"
    assert _token_status(token_db, token) == kb.TOKEN_SENT


async def test_reconcile_is_idempotent(token_db):
    """reconcile 2회 연속 실행 — 두 번째는 아무 것도 더 정산하지 않는다."""
    _create_legacy_ledger_table(token_db)
    token = await kb.reserve_send_token("p", "/idempotent")
    _insert_ledger_row_with_token(token_db, token, "/idempotent")

    first = await kb.reconcile_send_tokens()
    assert first["settled"] == 1
    status_after_first = _token_status(token_db, token)

    second = await kb.reconcile_send_tokens()
    status_after_second = _token_status(token_db, token)

    assert second["settled"] == 0, "이미 정산된 토큰을 두 번째 실행이 또 세면 안 된다"
    assert status_after_first == status_after_second == kb.TOKEN_SENT


async def test_reconcile_handles_missing_ledger_table_gracefully(token_db):
    """원장 테이블 자체가 아직 없는 갓 초기화된 DB — reconcile 이 죽지 않아야 한다."""
    token = await kb.reserve_send_token("p", "/no-ledger-yet")  # kream_api_calls 없음

    result = await kb.reconcile_send_tokens()

    assert result["settled"] == 0
    assert result["still_open"] == 1
    assert _token_status(token_db, token) == kb.TOKEN_RESERVED


def test_reconcile_wired_at_session_start_once_per_process():
    """`reconcile_send_tokens()` 가 세션 시작 지점(assert_kream_config_safe 직후)
    에서 프로세스당 1회 배선됐는지 소스로 확인."""
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "crawlers" / "kream.py"
    ).read_text(encoding="utf-8")

    assert "reconcile_send_tokens" in src
    config_idx = src.index("assert_kream_config_safe()")
    reconcile_call_idx = src.index("await _reconcile_send_tokens_once()")
    session_ctor_idx = src.index("self._session = AsyncSession(")
    assert config_idx < reconcile_call_idx < session_ctor_idx, (
        "reconcile 은 config 안전성 확인 직후 · 실 세션 생성 이전에 있어야 한다"
    )
