"""테스트 전역 안전망 — 실 네트워크·실 운영 DB 를 기계로 막는다.

## 왜 (2026-07-25 실사고, 두 번째)

- **2026-07-25**: 레거시 `scripts/bootstrap_cold_volumes.py` 실행 차단 가드의
  판별력을 변이(mutation) 검증으로 확인하려고 **가드만 끄고** 테스트를 돌렸다.
  그 테스트가 tmp DB 격리·네트워크 mock 없이 실 `settings.db_path` 를 읽는 바람에
  레거시 배치가 그대로 돌아 **실계정 크림 호출 311건**이 나갔다.
- **그 전에도**: `scripts/manual_auto_scan_live.py` 주석 — "과거 tests/ 하위에 있어
  pytest 자동 수집으로 실운영 크림 예산 오염".

구조적 원인은 하나다: 이 저장소에 `conftest.py` 가 없어서 DB·네트워크 격리를
**테스트 파일마다 각자 재구현**했다. 하나만 빠뜨리면 실계정으로 나간다.
그리고 **프로덕션 가드에 안전을 의존하면, 그 가드를 끄는 검증이 곧 사고가 된다.**

## 무엇을 막나

1. **실 HTTP 송신** — `curl_cffi`(AsyncSession/Session) · `httpx`(AsyncClient/Client) ·
   `aiohttp`(ClientSession) · `requests`(Session) 의 **송신 메서드**를 막는다.
   **클래스 메서드를 직접 갈아끼운다** — `from curl_cffi.requests import AsyncSession`
   처럼 이름을 자기 모듈에 바인딩한 코드(`src/crawlers/kream.py`)도 같은 클래스
   객체를 보므로 한 번에 덮인다.

   ⚠️ **생성이 아니라 송신을 막는 이유**: 생성(`__init__`)을 막았더니 클라이언트를
   만들어두고 상위 레이어를 mock 하는 정상 테스트 8건이 오탐으로 깨졌다
   (`test_musinsa_httpx.py` 등). **오탐이 나면 사람이 가드를 꺼버린다** —
   실제로 밖으로 나가는 순간만 막는 게 정밀도·수용성 모두 낫다. 인스턴스에
   mock 을 씌운 테스트는 인스턴스 속성이 클래스 메서드를 가리므로 그대로 통과한다.
2. **운영 DB 연결** — `sqlite3.connect` / `aiosqlite.connect` 대상 경로가
   `data/kream_bot.db`(운영)면 거부. tmp 경로·`:memory:` 는 그대로 통과.
   `src/core/db.py` 의 `async_connect`/`sync_connect` 도 내부적으로 이 둘을 쓰므로
   여기 한 곳이면 전 경로가 덮인다.

## 해제 (의도적으로 실호출이 필요할 때)

`KREAM_TEST_ALLOW_NETWORK=1` / `KREAM_TEST_ALLOW_REAL_DB=1`.
이 두 변수를 세우는 Bash 실행은 `.claude/hooks/kream_live_call_guard.py` 가 다시
한 번 막는다(사장 GO = `KREAM_LIVE_BATCH_GO=1` 필요) — 이중 방어가 의도다.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ALLOW_NETWORK_ENV = "KREAM_TEST_ALLOW_NETWORK"
ALLOW_REAL_DB_ENV = "KREAM_TEST_ALLOW_REAL_DB"

_REPO_ROOT = Path(__file__).resolve().parents[1]

# 현재 실행 중인 테스트 nodeid — 차단 메시지에 넣어 "어느 테스트가 범인인지" 즉시 안다.
_CURRENT_TEST: str = "<수집 단계>"


class KreamTestSafetyError(BaseException):
    """테스트가 실 네트워크/운영 DB 로 나가려 했다 — 안전망이 막았다.

    **`Exception` 이 아니라 `BaseException` 을 상속한다** (2026-07-25 실측):
    `KreamCrawler.fetch_screens_status` 등 프로덕션 경로가 `except Exception` 으로
    네트워크 오류를 흡수하는데, 안전망 예외까지 삼키면 차단이 "타임아웃(status 0)"
    으로 둔갑해 **테스트가 그대로 통과한다** — mock 을 빠뜨린 걸 아무도 모른다.
    `KeyboardInterrupt` 와 같은 계열로 두면 어떤 `except Exception` 도 못 가린다.
    """


# ---------------------------------------------------------------------------
# 운영 DB 경로 판별
# ---------------------------------------------------------------------------


def _production_db_paths() -> frozenset[str]:
    """막아야 할 운영 DB 절대경로 집합 (세션 시작 시 1회 확정)."""
    paths = {str((_REPO_ROOT / "data" / "kream_bot.db").resolve())}
    try:  # settings 가 다른 경로를 가리키면 그것도 운영 DB 다
        from src.config import settings

        paths.add(str(Path(settings.db_path).resolve()))
    except Exception:  # pragma: no cover — 설정 로드 실패는 안전망을 죽이지 않는다
        pass
    return frozenset(paths)


_PROD_DB_PATHS: frozenset[str] = _production_db_paths()


def _normalize_db_target(target) -> str | None:
    """`sqlite3.connect` 인자 → 비교 가능한 절대경로. 메모리 DB 는 None.

    `file:/path/db?mode=ro` URI 형태(읽기전용 조회에서 쓴다)도 경로만 뽑는다.
    """
    if isinstance(target, (int, bytes)):  # fd 기반/바이트 경로 — 판별 대상 아님
        return None
    try:
        s = os.fspath(target)
    except TypeError:
        return None
    if not isinstance(s, str) or not s or s == ":memory:":
        return None
    if s.startswith("file:"):
        s = s[len("file:") :].split("?", 1)[0]
        if s.startswith(":memory:") or not s:
            return None
    try:
        return str(Path(s).resolve())
    except OSError:  # pragma: no cover
        return None


def _is_production_db(target) -> bool:
    normalized = _normalize_db_target(target)
    return normalized is not None and normalized in _PROD_DB_PATHS


# ---------------------------------------------------------------------------
# 안전망 1 — 실 네트워크 클라이언트 생성 차단
# ---------------------------------------------------------------------------

# (모듈경로, 클래스명, 송신메서드) — 없으면 조용히 건너뛴다(선택 의존성 대응).
# 각 라이브러리에서 **모든 요청이 반드시 통과하는 하나의 메서드**만 고른다:
#   httpx: request()/get()/stream() 전부 send() 로 수렴
#   curl_cffi: get()/post() 전부 request() 로 수렴
#   aiohttp: get()/post() 전부 _request() 로 수렴
_NETWORK_EGRESS: tuple[tuple[str, str, str], ...] = (
    ("curl_cffi.requests", "AsyncSession", "request"),
    ("curl_cffi.requests", "Session", "request"),
    ("httpx", "AsyncClient", "send"),
    ("httpx", "Client", "send"),
    ("aiohttp", "ClientSession", "_request"),
    ("requests", "Session", "request"),
)


# httpx 는 in-process 전송에도 쓰인다 — 대시보드 라우트 테스트의 TestClient 는
# ASGI/WSGI transport 로 **프로세스 안**에서 앱을 호출한다(네트워크 아님).
# 실제 소켓으로 나가는 transport 는 이 둘뿐이라 그때만 막는다(오탐 방지).
_HTTPX_NETWORK_TRANSPORTS = frozenset({"HTTPTransport", "AsyncHTTPTransport"})


def _httpx_goes_to_network(client) -> bool:
    transport = getattr(client, "_transport", None)
    return type(transport).__name__ in _HTTPX_NETWORK_TRANSPORTS


def _blocked_egress(label: str, *, httpx_client: bool = False):
    def _send(self, *args, **kwargs):
        if httpx_client and not _httpx_goes_to_network(self):
            return _send.__wrapped_original__(self, *args, **kwargs)
        raise KreamTestSafetyError(
            f"테스트에서 실 네트워크 송신 차단: {label}\n"
            f"  테스트: {_CURRENT_TEST}\n"
            "  → 네트워크 진입점을 mock 하라 (AsyncMock/monkeypatch).\n"
            "     배치·스크립트 main() 을 부르는 테스트는 tmp DB 픽스처도 함께.\n"
            f"  의도적 실호출이면 {ALLOW_NETWORK_ENV}=1 (사장 GO 필요)."
        )

    return _send


@pytest.fixture(scope="session", autouse=True)
def _mark_test_process():
    """`KREAM_IN_TEST=1` 을 심는다 — **자식 프로세스까지 덮는 유일한 수단**.

    아래 송신 차단은 같은 프로세스 안에서만 유효하다. 테스트가 `subprocess` 로
    스크립트를 띄우면(예: `test_canary_matches.py`) 자식엔 conftest 가 없다.
    환경변수는 상속되므로, `src/crawlers/kream.py::_assert_not_under_test()` 가
    크림 세션 생성 관문에서 이 값을 보고 막는다.
    """
    previous = os.environ.get("KREAM_IN_TEST")
    os.environ["KREAM_IN_TEST"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("KREAM_IN_TEST", None)
        else:
            os.environ["KREAM_IN_TEST"] = previous


@pytest.fixture(scope="session", autouse=True)
def _block_real_network():
    """실 HTTP **송신**을 세션 내내 막는다 (생성은 허용 — 오탐 방지).

    클래스 메서드 교체라 `from X import Y` 로 이름을 미리 바인딩한 모듈도
    동일 클래스 객체를 보므로 함께 덮인다(모듈 스윕 불필요).
    """
    if os.getenv(ALLOW_NETWORK_ENV) == "1":
        yield
        return

    saved: list[tuple[type, str, object]] = []
    for module_path, cls_name, method in _NETWORK_EGRESS:
        try:
            __import__(module_path)
            cls = getattr(sys.modules[module_path], cls_name, None)
        except Exception:  # pragma: no cover — 미설치 의존성
            continue
        if not isinstance(cls, type) or not hasattr(cls, method):
            continue
        original = getattr(cls, method)
        saved.append((cls, method, original))
        blocker = _blocked_egress(
            f"{module_path}.{cls_name}.{method}()", httpx_client=(module_path == "httpx")
        )
        blocker.__wrapped_original__ = original  # in-process(ASGI/WSGI) 는 통과시킨다
        setattr(cls, method, blocker)

    try:
        yield
    finally:
        for cls, method, original in saved:
            setattr(cls, method, original)


# ---------------------------------------------------------------------------
# 안전망 2 — 운영 DB 연결 차단
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _block_production_db():
    """`data/kream_bot.db`(운영) 연결을 세션 내내 막는다. tmp/`:memory:` 는 통과."""
    if os.getenv(ALLOW_REAL_DB_ENV) == "1":
        yield
        return

    def _guard(target) -> None:
        if _is_production_db(target):
            raise KreamTestSafetyError(
                f"테스트에서 운영 DB 연결 차단: {target}\n"
                f"  테스트: {_CURRENT_TEST}\n"
                "  → tmp_path 기반 DB 를 쓰고 settings.db_path 를 monkeypatch 하라.\n"
                f"  의도적이면 {ALLOW_REAL_DB_ENV}=1 (사장 GO 필요)."
            )

    original_sqlite3 = sqlite3.connect

    def _sqlite3_connect(database, *args, **kwargs):
        _guard(database)
        return original_sqlite3(database, *args, **kwargs)

    sqlite3.connect = _sqlite3_connect

    original_aiosqlite = None
    try:
        import aiosqlite

        original_aiosqlite = aiosqlite.connect

        def _aiosqlite_connect(database, *args, **kwargs):
            _guard(database)
            return original_aiosqlite(database, *args, **kwargs)

        aiosqlite.connect = _aiosqlite_connect
    except Exception:  # pragma: no cover
        pass

    try:
        yield
    finally:
        sqlite3.connect = original_sqlite3
        if original_aiosqlite is not None:
            import aiosqlite

            aiosqlite.connect = original_aiosqlite


# ---------------------------------------------------------------------------
# 안전망 3 — `settings.db_path` 기본값을 테스트마다 tmp 로 (격리를 잊을 수 없게)
#
# 위 2번(운영 DB 차단)만 두면 "격리를 잊은 테스트"가 **실패**한다. 그건 사고는
# 막지만 매번 사람이 고쳐야 한다. 여기서 기본값 자체를 per-test tmp 로 돌려
# **잊어도 안전**하게 만든다. 자기 tmp DB 를 쓰는 테스트는 자기 픽스처에서 다시
# monkeypatch 하므로 그쪽이 이긴다(나중 대입 우선).
#
# 실측 발견(2026-07-25): 이게 없던 동안 `test_orchestrator_prefilter` ·
# `test_runtime_stock_gate` · `test_v3_runtime` 이 `coupon_store.lookup_catch`
# 경유로 **운영 쿠폰 데이터를 읽고** 있었다 — 결과가 운영 데이터에 따라 흔들렸다.
#
# 스키마는 세션 1회 생성 후 파일 복사(테스트당 ~1ms) — 테스트마다 마이그레이션을
# 돌리면 스위트가 수십 초 느려진다.
# ---------------------------------------------------------------------------


async def _build_schema(path: str) -> None:
    from src.models.database import Database

    manager = Database(path)
    await manager.connect()
    await manager.close()


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory) -> Path | None:
    """정본 스키마(`src/models/database.py`)가 적용된 빈 DB 템플릿."""
    template = tmp_path_factory.mktemp("kream_schema") / "template.db"
    try:
        asyncio.run(_build_schema(str(template)))
    except Exception:  # pragma: no cover — 스키마 생성 실패가 스위트를 막지 않는다
        return None
    return template


@pytest.fixture(autouse=True)
def _default_db_is_tmp(_schema_template, tmp_path, monkeypatch):
    if _schema_template is None:  # pragma: no cover
        return
    target = tmp_path / "settings_db.db"
    try:
        shutil.copyfile(_schema_template, target)
    except OSError:  # pragma: no cover
        return
    from src.config import settings

    monkeypatch.setattr(settings, "db_path", str(target), raising=False)


# ---------------------------------------------------------------------------
# 차단 메시지에 실을 "현재 테스트" 추적 (비용 = 문자열 대입 1회)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _track_current_test(request):
    global _CURRENT_TEST
    _CURRENT_TEST = request.node.nodeid
    yield
    _CURRENT_TEST = "<테스트 밖>"
