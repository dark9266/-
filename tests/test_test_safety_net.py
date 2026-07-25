"""전역 안전망(`tests/conftest.py`) 자체가 **진짜 막는지** 고정.

안전망은 "있는 것"이 아니라 "작동하는 것"이어야 한다. 2026-07-25 사고의 교훈:
가드가 있다고 믿고 검증을 안 하면, 그 가드가 꺼진 순간을 아무도 모른다.

여기서 고정하는 것:
1. 실 네트워크 송신 → 차단 (curl_cffi / httpx 실 transport)
2. in-process(ASGI/WSGI) httpx → **통과** (오탐 방어 — 대시보드 라우트 테스트)
3. 운영 DB 연결 → 차단, tmp·`:memory:` → 통과
4. `settings.db_path` 기본값이 tmp 로 격리돼 있음 (격리를 잊어도 안전)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.conftest import _PROD_DB_PATHS, KreamTestSafetyError

# ---------------------------------------------------------------------------
# 1. 실 네트워크 송신 차단
# ---------------------------------------------------------------------------


async def test_curl_cffi_real_request_is_blocked():
    from curl_cffi.requests import AsyncSession

    session = AsyncSession()  # 생성 자체는 허용 (오탐 방지)
    with pytest.raises(KreamTestSafetyError, match="실 네트워크 송신 차단"):
        await session.request("GET", "https://kream.co.kr/")


async def test_httpx_real_transport_request_is_blocked():
    import httpx

    with pytest.raises(KreamTestSafetyError, match="실 네트워크 송신 차단"):
        with httpx.Client() as client:  # 기본 transport = HTTPTransport(실 소켓)
            client.get("https://example.com/")


async def test_kream_crawler_cannot_reach_network():
    """크림 크롤러 실경로 — `from X import Y` 바인딩도 덮이고, 프로덕션의
    `except Exception` 이 안전망 예외를 **삼키지 못하는지**까지 확인한다."""
    from src.crawlers.kream import KreamCrawler

    crawler = KreamCrawler()
    with pytest.raises(KreamTestSafetyError):
        await crawler.fetch_screens_status("P1")


# ---------------------------------------------------------------------------
# 2. in-process 전송은 통과 (오탐 방어)
# ---------------------------------------------------------------------------


async def test_httpx_in_process_asgi_transport_is_allowed():
    """TestClient 류(프로세스 안에서 앱 호출)는 네트워크가 아니다 — 막으면 오탐."""
    import httpx

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert resp.text == "ok"


# ---------------------------------------------------------------------------
# 3. 운영 DB 차단 / tmp·메모리 통과
# ---------------------------------------------------------------------------


def test_production_db_connect_is_blocked():
    prod = sorted(_PROD_DB_PATHS)[0]
    with pytest.raises(KreamTestSafetyError, match="운영 DB 연결 차단"):
        sqlite3.connect(prod)


def test_production_db_readonly_uri_is_also_blocked():
    """`file:...?mode=ro` 우회도 같이 막힌다 (읽기여도 운영 DB 는 운영 DB)."""
    prod = sorted(_PROD_DB_PATHS)[0]
    with pytest.raises(KreamTestSafetyError, match="운영 DB 연결 차단"):
        sqlite3.connect(f"file:{prod}?mode=ro", uri=True)


async def test_production_db_async_connect_is_blocked():
    import aiosqlite

    prod = sorted(_PROD_DB_PATHS)[0]
    with pytest.raises(KreamTestSafetyError, match="운영 DB 연결 차단"):
        async with aiosqlite.connect(prod):
            pass


def test_tmp_and_memory_db_pass_through(tmp_path):
    with sqlite3.connect(str(tmp_path / "ok.db")) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    with sqlite3.connect(":memory:") as conn:
        conn.execute("SELECT 1")


# ---------------------------------------------------------------------------
# 4. settings.db_path 기본 격리 — 격리를 "잊어도" 안전
# ---------------------------------------------------------------------------


def test_settings_db_path_defaults_to_tmp_copy():
    from src.config import settings

    current = Path(settings.db_path).resolve()
    assert str(current) not in _PROD_DB_PATHS, "테스트 기본 DB 가 운영 DB 를 가리킨다"
    assert current.exists(), "스키마 템플릿 복사본이 없다"


def test_default_tmp_db_has_canonical_schema():
    """정본 스키마가 들어있어야 `coupon_store` 류 직접 접근이 깨지지 않는다."""
    from src.config import settings

    conn = sqlite3.connect(settings.db_path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"kream_products", "kream_api_calls", "coupon_catches"} <= names


def test_default_tmp_db_is_isolated_per_test(tmp_path):
    """다른 테스트가 쓴 내용이 넘어오지 않는다 (테스트마다 새 복사본)."""
    from src.config import settings

    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM kream_products").fetchone()[0]
    finally:
        conn.close()
    assert rows == 0


# ---------------------------------------------------------------------------
# 5. 사고 시나리오 재현 — 프로덕션 가드가 없어도 안전망이 막는다
#
# 2026-07-25 사고: 레거시 배치의 실행 차단 가드를 변이 검증으로 끄자 그대로
# 돌아 실계정 크림 호출 311건. 그때는 안전망이 없었다.
# 여기서는 **가드를 명시적으로 우회**(GO env)하고 대상 데이터까지 채워서,
# 안전망 단독으로 실호출을 막는지 고정한다. 이 테스트가 깨지면 = 안전망이
# 뚫렸다는 뜻이다.
# ---------------------------------------------------------------------------


async def test_incident_replay_legacy_batch_cannot_reach_kream(monkeypatch):
    import scripts.bootstrap_cold_volumes as legacy
    from src.config import settings

    # 가드를 우회한다 — 안전망 단독 성능을 보는 게 목적
    monkeypatch.setenv(legacy.LEGACY_GO_ENV, "1")

    # 기본 tmp DB(안전망 3)에 대상 1건을 심어 "대상 없음" 조기 종료를 배제
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO kream_products (product_id, name, model_number) VALUES (?,?,?)",
            ("P1", "테스트 상품", "M1"),
        )
        conn.execute(
            "INSERT INTO retail_products (source, product_id, name, model_number) "
            "VALUES (?,?,?,?)",
            ("puma", "R1", "테스트 리테일", "M1"),
        )
        conn.commit()
    finally:
        conn.close()

    # 실 크림으로 나가려는 순간 안전망이 막는다 — 프로덕션의 except Exception 도
    # 못 삼킨다(BaseException 상속).
    with pytest.raises(KreamTestSafetyError):
        await legacy.main()
