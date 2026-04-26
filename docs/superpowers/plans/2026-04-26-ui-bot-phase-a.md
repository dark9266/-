# UI 봇 Phase A 구현 Plan (Setup + A1~A4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 현 크림봇 (Python 백그라운드 + Discord) → Electron 데스크톱 UI 봇 전환. 시작/중지 + 모드 토글 + 대시보드 + 운영 패널까지 풀 구현.

**Architecture:** 옵션 D — 봇 = Python + FastAPI (헤드리스 서버, `localhost:8000` → 미래 임대 서버). Electron = 클라이언트 (메인 윈도우 + 분리 가능 로그 윈도우). 통신 = HTTP REST + WebSocket. DB 공유 X (FastAPI가 봇 DB 단독 접근).

**Tech Stack:**
- 봇 측: Python 3.12 + FastAPI 0.115+ + uvicorn 0.32+ (이미 deps에 있음) + aiosqlite
- UI 측: Electron 33 + React 18 + Vite 5 + Tailwind 3 + shadcn/ui + lucide-react + Recharts + Zustand + axios
- 테스트: pytest (이미 사용) + Playwright (Electron e2e)
- 의존성 관리: pyproject.toml + electron/package.json

**Spec:** `docs/superpowers/specs/2026-04-26-ui-bot-design.md` (커밋 fabbc29)

---

## Scope

이 plan = Phase A 풀 완주 (Setup + A1 + A2 + A3 + A4). 약 4일 effort, 35+ tasks.

Phase B (보관판매 자동등록) / Phase C (자동경쟁) = 사이드바 메뉴 자리만 (Disabled placeholder), 디테일 추후 사용자 합의 후 별도 plan.

트랙 1 (소싱처 안정화) 와 병행 — 이 plan은 봇 코어 (`runtime.py`, `orchestrator.py`, 어댑터들) 거의 안 건드림.

---

## File Structure

### Python (봇 측, 신규)

```
src/
├── api/                              # 신규 패키지
│   ├── __init__.py                   # 빈 파일
│   ├── server.py                     # FastAPI app + lifespan + 라우터 등록
│   ├── auth.py                       # 토큰 인증 의존성
│   ├── state.py                      # bot_state CRUD (paused / mode / enabled_sources)
│   ├── log_emitter.py                # logging.Handler → SQLite + WebSocket broadcast
│   ├── ws_manager.py                 # WebSocket connection manager (channels: status/alerts/logs)
│   └── routers/
│       ├── __init__.py
│       ├── bot.py                    # /api/bot/{pause,resume,status,restart,debug,update}
│       ├── sources.py                # /api/sources/{list,enable,disable,mode}
│       ├── dashboard.py              # /api/dashboard/snapshot, /ws/status, /ws/alerts
│       ├── ops.py                    # /api/diagnostics/snapshot, /api/health
│       └── logs.py                   # /api/logs/export?grep=&hours=, /ws/logs
└── models/database.py                # 수정: SCHEMA_SQL 에 bot_state 추가
main.py                               # 수정: FastAPI 백그라운드 task 추가
.env.example                          # 수정: API_TOKEN, API_HOST, API_PORT 추가
pyproject.toml                        # 변경 X (deps 이미 있음)
tests/api/                            # 신규 테스트 디렉토리
├── __init__.py
├── test_state.py
├── test_auth.py
├── test_bot_router.py
├── test_sources_router.py
├── test_dashboard_router.py
├── test_ws_status.py
├── test_log_emitter.py
└── test_diagnostics.py
```

### Electron (UI 측, 완전 신규)

```
electron/
├── package.json
├── tsconfig.json
├── tsconfig.node.json
├── vite.config.ts
├── electron-builder.json
├── tailwind.config.js
├── postcss.config.js
├── .gitignore
├── index.html                        # 메인 윈도우 entry
├── log-window.html                   # 분리 로그 윈도우 entry
├── src/
│   ├── main/                         # Electron Main Process
│   │   ├── index.ts                  # 앱 entry + 윈도우 생성
│   │   ├── windows.ts                # 메인/로그 윈도우 매니저
│   │   ├── preload.ts                # Renderer ↔ Main IPC bridge
│   │   └── api-config.ts             # API URL/토큰 환경변수
│   ├── renderer/                     # 메인 윈도우 (React)
│   │   ├── main.tsx                  # React mount
│   │   ├── App.tsx                   # Router + Layout
│   │   ├── index.css                 # Tailwind base
│   │   ├── pages/
│   │   │   ├── Dashboard.tsx
│   │   │   ├── Adapters.tsx
│   │   │   ├── Logs.tsx
│   │   │   ├── Diagnostics.tsx
│   │   │   ├── Settings.tsx
│   │   │   └── Placeholder.tsx       # Phase B/C 메뉴용
│   │   ├── components/
│   │   │   ├── Sidebar.tsx
│   │   │   ├── StartStopButton.tsx
│   │   │   ├── StatusCard.tsx
│   │   │   ├── AdapterGrid.tsx
│   │   │   ├── ModeToggle.tsx
│   │   │   ├── RecentAlerts.tsx
│   │   │   └── ChartCalls24h.tsx
│   │   └── lib/
│   │       ├── api.ts                # axios 인스턴스 + 토큰
│   │       ├── ws.ts                 # WebSocket client + 재연결
│   │       └── store.ts              # Zustand global state
│   └── log-window/                   # 분리 로그 윈도우 (별도 entry)
│       ├── main.tsx
│       └── LogWindow.tsx
└── tests/
    ├── dashboard.spec.ts             # Playwright e2e
    ├── start-stop.spec.ts
    ├── adapters.spec.ts
    └── log-window.spec.ts
.gitignore                            # 수정: electron/node_modules, electron/dist 추가
```

### 데이터 흐름 요약

```
[Bot Python Process]
  ├── runtime.py + scheduler 6 loops (pause check 추가)
  ├── matcher / profit_calculator / alert (현행)
  └── FastAPI (asyncio.create_task) ◄─── HTTP/WebSocket ─── [Electron]
       ├── /api/bot/* (제어)
       ├── /api/sources/* (모드 토글)
       ├── /api/dashboard/snapshot
       ├── /api/diagnostics/snapshot
       ├── /api/logs/export
       ├── /ws/status (5초 push)
       ├── /ws/alerts (이벤트 push)
       └── /ws/logs (실시간 stream)
       
[SQLite kream_bot.db]
  ├── 기존 테이블 (kream_products, alert_sent, decision_log 등)
  └── bot_state (신규 — paused/mode/enabled_sources/debug_mode)
```

---

## Setup Phase (Step 0) — FastAPI 골격 + Electron 골격

이 단계 = "양쪽 끝 비어있는 상태로 연결만 확인". 다음 Phase A1~A4 작업 진입 가능 base.

### Task 0.1: bot_state 테이블 추가 (DB 스키마)

**Files:**
- Modify: `src/models/database.py:13` (SCHEMA_SQL 블록에 추가)
- Create: `tests/api/__init__.py` (빈 파일)
- Create: `tests/api/test_state_table.py`

- [ ] **Step 1: 빈 테스트 디렉토리 + __init__.py 생성**

```bash
mkdir -p tests/api && touch tests/api/__init__.py
```

- [ ] **Step 2: 실패 테스트 작성**

```python
# tests/api/test_state_table.py
import aiosqlite
import pytest

from src.config import settings
from src.models.database import init_db


@pytest.mark.asyncio
async def test_bot_state_table_exists():
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_state'"
        )
        row = await cur.fetchone()
    assert row is not None, "bot_state 테이블이 init_db 후 존재해야 함"


@pytest.mark.asyncio
async def test_bot_state_default_rows():
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("SELECT key, value FROM bot_state")
        rows = await cur.fetchall()
    keys = {r[0] for r in rows}
    assert "paused" in keys
    assert "mode" in keys
    assert "enabled_sources" in keys
    assert "debug_mode" in keys
```

- [ ] **Step 3: 테스트 실행 — 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_state_table.py -v
```
Expected: FAIL — `no such table: bot_state`

- [ ] **Step 4: SCHEMA_SQL 에 bot_state 추가**

`src/models/database.py` 의 `SCHEMA_SQL = """ ... """` 블록 끝부분 (마지막 CREATE TABLE 다음, `"""` 닫기 직전) 에 추가:

```sql
-- UI 봇 상태 (Phase A — 시작/중지, 모드, 소싱처 토글, 디버그)
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO bot_state (key, value) VALUES
    ('paused', 'false'),
    ('mode', '24h_full'),
    ('enabled_sources', '[]'),
    ('debug_mode', 'false');
```

- [ ] **Step 5: 테스트 재실행 — PASS 확인**

```bash
PYTHONPATH=. pytest tests/api/test_state_table.py -v
```
Expected: 2 passed

- [ ] **Step 6: 커밋**

```bash
git add src/models/database.py tests/api/__init__.py tests/api/test_state_table.py
git commit -m "feat(api): bot_state 테이블 추가 — UI 봇 상태 영속화"
```

---

### Task 0.2: state.py CRUD 모듈

**Files:**
- Create: `src/api/__init__.py` (빈 파일)
- Create: `src/api/state.py`
- Create: `tests/api/test_state.py`

- [ ] **Step 1: 패키지 생성**

```bash
mkdir -p src/api/routers && touch src/api/__init__.py src/api/routers/__init__.py
```

- [ ] **Step 2: 실패 테스트**

```python
# tests/api/test_state.py
import pytest

from src.api.state import get_state, set_state, get_all_state
from src.models.database import init_db


@pytest.fixture(autouse=True)
async def _setup_db():
    await init_db()
    yield


@pytest.mark.asyncio
async def test_get_state_default_paused_false():
    val = await get_state("paused")
    assert val == "false"


@pytest.mark.asyncio
async def test_set_then_get_state_roundtrip():
    await set_state("paused", "true")
    val = await get_state("paused")
    assert val == "true"
    await set_state("paused", "false")  # cleanup


@pytest.mark.asyncio
async def test_get_all_state_returns_dict():
    state = await get_all_state()
    assert isinstance(state, dict)
    assert "paused" in state
    assert "mode" in state


@pytest.mark.asyncio
async def test_set_unknown_key_creates_row():
    await set_state("test_new_key", "value123")
    val = await get_state("test_new_key")
    assert val == "value123"
```

- [ ] **Step 3: 테스트 실행 — 실패**

```bash
PYTHONPATH=. pytest tests/api/test_state.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'src.api.state'`

- [ ] **Step 4: state.py 구현**

```python
# src/api/state.py
"""bot_state 테이블 CRUD — UI 봇 제어/모드 상태 영속화."""

from __future__ import annotations

import aiosqlite

from src.config import settings


async def get_state(key: str) -> str | None:
    """단일 key 값 조회. 없으면 None."""
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = await cur.fetchone()
    return row[0] if row else None


async def set_state(key: str, value: str) -> None:
    """단일 key 값 upsert. updated_at 자동 갱신."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """
            INSERT INTO bot_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        await db.commit()


async def get_all_state() -> dict[str, str]:
    """전체 key/value 한 번에 조회."""
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("SELECT key, value FROM bot_state")
        rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}
```

- [ ] **Step 5: 테스트 PASS 확인**

```bash
PYTHONPATH=. pytest tests/api/test_state.py -v
```
Expected: 4 passed

- [ ] **Step 6: 커밋**

```bash
git add src/api/__init__.py src/api/routers/__init__.py src/api/state.py tests/api/test_state.py
git commit -m "feat(api): bot_state CRUD 모듈 — get/set/get_all"
```

---

### Task 0.3: API 토큰 인증 (auth.py)

**Files:**
- Create: `src/api/auth.py`
- Modify: `.env.example` (API_TOKEN 추가)
- Modify: `src/config.py` (API 설정 추가)
- Create: `tests/api/test_auth.py`

- [ ] **Step 1: .env.example 갱신**

`.env.example` 끝부분에 추가:
```bash

# UI 봇 (Phase A) — FastAPI 서버
API_HOST=127.0.0.1
API_PORT=8000
API_TOKEN=change-me-to-random-token-for-electron-client
```

- [ ] **Step 2: src/config.py 갱신 — settings 클래스에 추가**

`src/config.py` 의 `Settings` 클래스 안에 추가 (기존 필드 끝부분):

```python
    # UI 봇 (Phase A) — FastAPI
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_token: str = "dev-token-replace-in-prod"
```

- [ ] **Step 3: 실패 테스트**

```python
# tests/api/test_auth.py
import pytest
from fastapi import HTTPException

from src.api.auth import verify_token


def test_verify_token_correct():
    # settings.api_token 의 기본값 사용
    from src.config import settings
    result = verify_token(f"Bearer {settings.api_token}")
    assert result is True


def test_verify_token_missing_bearer_prefix():
    with pytest.raises(HTTPException) as exc:
        verify_token("invalid-no-bearer")
    assert exc.value.status_code == 401


def test_verify_token_wrong_token():
    with pytest.raises(HTTPException) as exc:
        verify_token("Bearer wrong-token-value")
    assert exc.value.status_code == 401


def test_verify_token_empty():
    with pytest.raises(HTTPException) as exc:
        verify_token(None)
    assert exc.value.status_code == 401
```

- [ ] **Step 4: 테스트 실행 — 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_auth.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 5: auth.py 구현**

```python
# src/api/auth.py
"""FastAPI 토큰 인증 — Bearer 헤더 검증."""

from fastapi import Header, HTTPException, status

from src.config import settings


def verify_token(authorization: str | None = Header(default=None)) -> bool:
    """Bearer 토큰 검증. 실패 시 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header (expected 'Bearer <token>')",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
        )
    return True
```

- [ ] **Step 6: 테스트 PASS 확인**

```bash
PYTHONPATH=. pytest tests/api/test_auth.py -v
```
Expected: 4 passed

- [ ] **Step 7: 커밋**

```bash
git add src/api/auth.py src/config.py .env.example tests/api/test_auth.py
git commit -m "feat(api): Bearer 토큰 인증 + API 설정 추가"
```

---

### Task 0.4: WebSocket connection manager (ws_manager.py)

**Files:**
- Create: `src/api/ws_manager.py`
- Create: `tests/api/test_ws_manager.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_ws_manager.py
import pytest

from src.api.ws_manager import WebSocketManager


class _FakeWebSocket:
    """테스트용 fake — accept/send_json 호출 기록."""
    def __init__(self):
        self.sent = []
        self.accepted = False
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, data):
        if self.closed:
            raise RuntimeError("closed")
        self.sent.append(data)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_connect_registers_socket():
    mgr = WebSocketManager()
    ws = _FakeWebSocket()
    await mgr.connect("status", ws)
    assert ws.accepted is True
    assert ws in mgr._channels["status"]


@pytest.mark.asyncio
async def test_broadcast_sends_to_channel():
    mgr = WebSocketManager()
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    await mgr.connect("status", ws1)
    await mgr.connect("status", ws2)
    await mgr.broadcast("status", {"hello": "world"})
    assert ws1.sent == [{"hello": "world"}]
    assert ws2.sent == [{"hello": "world"}]


@pytest.mark.asyncio
async def test_broadcast_skips_other_channels():
    mgr = WebSocketManager()
    ws_status = _FakeWebSocket()
    ws_logs = _FakeWebSocket()
    await mgr.connect("status", ws_status)
    await mgr.connect("logs", ws_logs)
    await mgr.broadcast("status", {"x": 1})
    assert ws_status.sent == [{"x": 1}]
    assert ws_logs.sent == []


@pytest.mark.asyncio
async def test_disconnect_removes_socket():
    mgr = WebSocketManager()
    ws = _FakeWebSocket()
    await mgr.connect("status", ws)
    mgr.disconnect("status", ws)
    assert ws not in mgr._channels.get("status", [])


@pytest.mark.asyncio
async def test_broadcast_drops_dead_socket():
    mgr = WebSocketManager()
    ws = _FakeWebSocket()
    await mgr.connect("status", ws)
    ws.closed = True  # 다음 send 시 RuntimeError
    await mgr.broadcast("status", {"x": 1})  # 예외 흡수해야 함
    assert ws not in mgr._channels["status"]
```

- [ ] **Step 2: 테스트 실행 — 실패**

```bash
PYTHONPATH=. pytest tests/api/test_ws_manager.py -v
```
Expected: FAIL

- [ ] **Step 3: ws_manager.py 구현**

```python
# src/api/ws_manager.py
"""WebSocket connection manager — 채널별 broadcast.

채널: status (5초 주기 봇 상태) / alerts (이벤트 push) / logs (실시간 로그 stream).
fail-safe: 죽은 socket 자동 제거.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Protocol


class _WSLike(Protocol):
    async def accept(self) -> None: ...
    async def send_json(self, data: Any) -> None: ...
    async def close(self) -> None: ...


class WebSocketManager:
    """채널 기반 WebSocket fan-out."""

    def __init__(self) -> None:
        self._channels: dict[str, list[_WSLike]] = defaultdict(list)

    async def connect(self, channel: str, ws: _WSLike) -> None:
        await ws.accept()
        self._channels[channel].append(ws)

    def disconnect(self, channel: str, ws: _WSLike) -> None:
        if ws in self._channels.get(channel, []):
            self._channels[channel].remove(ws)

    async def broadcast(self, channel: str, payload: Any) -> None:
        """채널 모든 구독자에 push. send 실패 시 해당 socket 제거."""
        dead: list[_WSLike] = []
        for ws in list(self._channels.get(channel, [])):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(channel, ws)


# 전역 싱글톤 — FastAPI app + 봇 코어 양쪽에서 import
ws_manager = WebSocketManager()
```

- [ ] **Step 4: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_ws_manager.py -v
```
Expected: 5 passed

- [ ] **Step 5: 커밋**

```bash
git add src/api/ws_manager.py tests/api/test_ws_manager.py
git commit -m "feat(api): WebSocket 채널 매니저 — broadcast + dead socket 제거"
```

---

### Task 0.5: FastAPI 서버 골격 + /api/health (server.py)

**Files:**
- Create: `src/api/server.py`
- Create: `src/api/routers/ops.py` (health endpoint 우선)
- Create: `tests/api/test_health.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_health.py
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.server import app


@pytest.mark.asyncio
async def test_health_endpoint_no_auth_required():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "uptime_sec" in body
```

- [ ] **Step 2: 테스트 실행 — 실패**

```bash
PYTHONPATH=. pytest tests/api/test_health.py -v
```
Expected: FAIL — `ModuleNotFoundError: src.api.server`

- [ ] **Step 3: ops.py (health 만 우선) 구현**

```python
# src/api/routers/ops.py
"""운영/진단 라우터 — health, diagnostics snapshot."""

import time

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["ops"])

_START_TIME = time.time()


@router.get("/health")
async def health() -> dict:
    """기본 헬스체크 — 인증 불필요. Electron이 서버 가동 확인용."""
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - _START_TIME),
    }
```

- [ ] **Step 4: server.py 구현**

```python
# src/api/server.py
"""FastAPI app — 봇 main.py 가 asyncio.create_task 로 띄움."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routers import ops
from src.utils.logging import setup_logger

logger = setup_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[api] FastAPI lifespan start")
    yield
    logger.info("[api] FastAPI lifespan stop")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Kream Bot API",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Electron Renderer (file:// 또는 vite dev) 에서 호출 → CORS 허용
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # localhost 전용 — 임대 서버 시점에 조정
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(ops.router)
    return app


app = create_app()
```

- [ ] **Step 5: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_health.py -v
```
Expected: 1 passed

- [ ] **Step 6: 커밋**

```bash
git add src/api/server.py src/api/routers/ops.py tests/api/test_health.py
git commit -m "feat(api): FastAPI 서버 골격 + /api/health 엔드포인트"
```

---

### Task 0.6: main.py 통합 — FastAPI를 봇 asyncio loop 의 background task 로

**Files:**
- Modify: `main.py:145-153` (`_main_async` 함수)
- Create: `tests/test_main_api_launch.py`

- [ ] **Step 1: 실패 테스트 — main 가 import 됨 + create_task 로 uvicorn 실행됨**

```python
# tests/test_main_api_launch.py
"""main.py 가 FastAPI 서버를 asyncio task 로 띄우는지 검증."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_main_async_launches_api_task():
    """_main_async 진입 시 uvicorn.Server.serve 가 task 로 띄워져야 함."""
    serve_mock = AsyncMock()
    bot_start_mock = AsyncMock()

    with (
        patch("main.bot") as bot_patch,
        patch("uvicorn.Server") as server_cls,
    ):
        bot_patch.__aenter__ = AsyncMock(return_value=bot_patch)
        bot_patch.__aexit__ = AsyncMock(return_value=False)
        bot_patch.start = bot_start_mock

        server_inst = MagicMock()
        server_inst.serve = serve_mock
        server_cls.return_value = server_inst

        from main import _main_async
        await _main_async()

    assert serve_mock.called, "uvicorn.Server.serve 가 호출되어야 함"
    assert bot_start_mock.called, "discord bot.start 도 함께 호출되어야 함"
```

- [ ] **Step 2: 테스트 실행 — 실패**

```bash
PYTHONPATH=. pytest tests/test_main_api_launch.py -v
```
Expected: FAIL — uvicorn.Server 호출 안 됨

- [ ] **Step 3: main.py 수정 — `_main_async` 변경**

`main.py` 의 `_main_async` 함수 (라인 145~153) 를 다음으로 교체:

```python
async def _main_async() -> None:
    """봇 이벤트 루프 — graceful shutdown 보장 (2026-04-18 incident 대응).

    bot.run()은 SIGINT 시 내부 cleanup만 돌고 `KreamBot.close()`를 안 부른다.
    `async with bot:` 의 __aexit__가 close()를 보장 → WAL checkpoint flush.

    Phase A (UI 봇 전환) — FastAPI 서버를 봇과 동일 process 의 background task 로 launch.
    봇 죽으면 API 도 죽음 → UI 가 즉시 감지 (의도적).
    """
    import uvicorn

    from src.api.server import app as api_app

    api_config = uvicorn.Config(
        api_app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="warning",  # 봇 로그와 섞이지 않게
        access_log=False,
    )
    api_server = uvicorn.Server(api_config)
    api_task = asyncio.create_task(api_server.serve(), name="kream-api-server")

    try:
        async with bot:
            await bot.start(settings.discord_token)
    finally:
        api_server.should_exit = True
        try:
            await asyncio.wait_for(api_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            api_task.cancel()
```

- [ ] **Step 4: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/test_main_api_launch.py -v
```
Expected: 1 passed

- [ ] **Step 5: 실 봇 가동 검증 (수동)**

```bash
# 현 봇 정지
kill $(cat data/kreambot.pid)
# 재가동
source ~/kream-venv/bin/activate
PYTHONPATH=. python main.py &
# 5초 대기 후 health 확인
sleep 5 && curl -s http://localhost:8000/api/health | python3 -m json.tool
```
Expected: `{"status": "ok", "uptime_sec": <small_number>}`

- [ ] **Step 6: 커밋**

```bash
git add main.py tests/test_main_api_launch.py
git commit -m "feat(main): FastAPI 서버를 봇 asyncio loop background task 로 통합"
```

---

### Task 0.7: Electron 앱 골격 (package.json + Vite + React + Tailwind)

**Files:**
- Create: `electron/package.json`
- Create: `electron/tsconfig.json`
- Create: `electron/tsconfig.node.json`
- Create: `electron/vite.config.ts`
- Create: `electron/tailwind.config.js`
- Create: `electron/postcss.config.js`
- Create: `electron/index.html`
- Create: `electron/.gitignore`
- Modify: `.gitignore` (electron/node_modules 등 추가)

- [ ] **Step 1: electron 디렉토리 생성**

```bash
mkdir -p electron/src/main electron/src/renderer/pages electron/src/renderer/components electron/src/renderer/lib electron/src/log-window electron/tests
```

- [ ] **Step 2: electron/package.json 생성**

```json
{
  "name": "kream-bot-ui",
  "version": "0.1.0",
  "description": "크림봇 UI (Electron + React)",
  "main": "dist/main/index.js",
  "type": "module",
  "scripts": {
    "dev": "concurrently \"vite\" \"wait-on http://localhost:5173 && electron .\"",
    "build:renderer": "vite build",
    "build:main": "tsc -p tsconfig.node.json",
    "build": "npm run build:renderer && npm run build:main",
    "start": "electron .",
    "test": "playwright test"
  },
  "dependencies": {
    "axios": "^1.7.7",
    "lucide-react": "^0.460.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-hot-toast": "^2.4.1",
    "react-router-dom": "^6.28.0",
    "recharts": "^2.13.3",
    "zustand": "^5.0.1"
  },
  "devDependencies": {
    "@playwright/test": "^1.48.2",
    "@types/node": "^22.9.0",
    "@types/react": "^18.3.12",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.3",
    "autoprefixer": "^10.4.20",
    "concurrently": "^9.1.0",
    "electron": "^33.2.0",
    "electron-builder": "^25.1.8",
    "postcss": "^8.4.49",
    "tailwindcss": "^3.4.15",
    "typescript": "^5.6.3",
    "vite": "^5.4.11",
    "wait-on": "^8.0.1"
  }
}
```

- [ ] **Step 3: electron/tsconfig.json (renderer용)**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true
  },
  "include": ["src/renderer/**/*", "src/log-window/**/*"]
}
```

- [ ] **Step 4: electron/tsconfig.node.json (main process용)**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "CommonJS",
    "moduleResolution": "node",
    "outDir": "dist/main",
    "rootDir": "src/main",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "include": ["src/main/**/*"]
}
```

- [ ] **Step 5: electron/vite.config.ts**

```typescript
import react from "@vitejs/plugin-react";
import { resolve } from "path";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist/renderer",
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
        log: resolve(__dirname, "log-window.html"),
      },
    },
  },
  server: { port: 5173 },
});
```

- [ ] **Step 6: tailwind.config.js + postcss.config.js**

```javascript
// electron/tailwind.config.js
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./log-window.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0d1117",
        sidebar: "#010409",
        card: "#161b22",
        border: "#30363d",
        text: { DEFAULT: "#c9d1d9", muted: "#8b949e" },
        accent: "#58a6ff",
        ok: "#3fb950",
        warn: "#d29922",
        err: "#f85149",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
```

```javascript
// electron/postcss.config.js
export default {
  plugins: { tailwindcss: {}, autoprefixer: {} },
};
```

- [ ] **Step 7: electron/index.html (메인 윈도우)**

```html
<!doctype html>
<html lang="ko">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Kream Bot — Control Center</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link
      href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
  </head>
  <body class="bg-bg text-text">
    <div id="root"></div>
    <script type="module" src="/src/renderer/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 8: electron/log-window.html (분리 로그 윈도우 entry)**

```html
<!doctype html>
<html lang="ko">
  <head>
    <meta charset="UTF-8" />
    <title>Kream Bot — Live Logs</title>
    <link
      href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
  </head>
  <body class="bg-black text-text">
    <div id="root"></div>
    <script type="module" src="/src/log-window/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 9: electron/.gitignore + 루트 .gitignore 갱신**

```
# electron/.gitignore
node_modules/
dist/
release/
.vite/
```

루트 `.gitignore` 끝에 추가:
```
# Electron 빌드 산출물
electron/node_modules/
electron/dist/
electron/release/
```

- [ ] **Step 10: npm install + 빌드 sanity check**

```bash
cd electron && npm install
```
Expected: deps 설치 완료, ~120MB node_modules

- [ ] **Step 11: 커밋**

```bash
cd /mnt/c/Users/USER/Desktop/크림봇
git add electron/package.json electron/tsconfig.json electron/tsconfig.node.json electron/vite.config.ts electron/tailwind.config.js electron/postcss.config.js electron/index.html electron/log-window.html electron/.gitignore .gitignore
git commit -m "feat(electron): 앱 골격 셋업 — Vite + React + Tailwind + dual entry"
```

---

### Task 0.8: Electron Main Process (windows.ts + index.ts + preload.ts)

**Files:**
- Create: `electron/src/main/index.ts`
- Create: `electron/src/main/windows.ts`
- Create: `electron/src/main/preload.ts`
- Create: `electron/src/main/api-config.ts`

- [ ] **Step 1: api-config.ts**

```typescript
// electron/src/main/api-config.ts
export const API_BASE_URL = process.env.KREAM_API_URL ?? "http://localhost:8000";
export const API_TOKEN = process.env.KREAM_API_TOKEN ?? "dev-token-replace-in-prod";
```

- [ ] **Step 2: windows.ts — 메인 + 분리 로그 윈도우 매니저**

```typescript
// electron/src/main/windows.ts
import { BrowserWindow, screen } from "electron";
import path from "path";

const isDev = !!process.env.VITE_DEV_SERVER_URL || process.env.NODE_ENV === "development";

let mainWindow: BrowserWindow | null = null;
let logWindow: BrowserWindow | null = null;

export function createMainWindow(): BrowserWindow {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.focus();
    return mainWindow;
  }

  const display = screen.getPrimaryDisplay();
  mainWindow = new BrowserWindow({
    width: Math.min(1400, display.workAreaSize.width - 100),
    height: Math.min(900, display.workAreaSize.height - 100),
    backgroundColor: "#0d1117",
    title: "Kream Bot — Control Center",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (isDev) {
    mainWindow.loadURL("http://localhost:5173/");
  } else {
    mainWindow.loadFile(path.join(__dirname, "../renderer/index.html"));
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  return mainWindow;
}

export function createLogWindow(): BrowserWindow {
  if (logWindow && !logWindow.isDestroyed()) {
    logWindow.focus();
    return logWindow;
  }

  logWindow = new BrowserWindow({
    width: 720,
    height: 600,
    backgroundColor: "#000000",
    title: "Kream Bot — Live Logs",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (isDev) {
    logWindow.loadURL("http://localhost:5173/log-window.html");
  } else {
    logWindow.loadFile(path.join(__dirname, "../renderer/log-window.html"));
  }

  logWindow.on("closed", () => {
    logWindow = null;
  });
  return logWindow;
}

export function getMainWindow() {
  return mainWindow;
}

export function getLogWindow() {
  return logWindow;
}
```

- [ ] **Step 3: preload.ts — IPC bridge**

```typescript
// electron/src/main/preload.ts
import { contextBridge, ipcRenderer } from "electron";

import { API_BASE_URL, API_TOKEN } from "./api-config";

contextBridge.exposeInMainWorld("kream", {
  apiBaseUrl: API_BASE_URL,
  apiToken: API_TOKEN,
  openLogWindow: () => ipcRenderer.invoke("window:open-log"),
  closeLogWindow: () => ipcRenderer.invoke("window:close-log"),
});

declare global {
  interface Window {
    kream: {
      apiBaseUrl: string;
      apiToken: string;
      openLogWindow: () => Promise<void>;
      closeLogWindow: () => Promise<void>;
    };
  }
}
```

- [ ] **Step 4: index.ts — 앱 entry**

```typescript
// electron/src/main/index.ts
import { app, ipcMain } from "electron";

import { createLogWindow, createMainWindow, getLogWindow } from "./windows";

app.whenReady().then(() => {
  createMainWindow();

  ipcMain.handle("window:open-log", () => {
    createLogWindow();
  });

  ipcMain.handle("window:close-log", () => {
    const win = getLogWindow();
    if (win && !win.isDestroyed()) win.close();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  createMainWindow();
});
```

- [ ] **Step 5: TypeScript 컴파일 sanity check**

```bash
cd electron && npm run build:main
```
Expected: `dist/main/{index.js, windows.js, preload.js, api-config.js}` 생성

- [ ] **Step 6: 커밋**

```bash
cd /mnt/c/Users/USER/Desktop/크림봇
git add electron/src/main/
git commit -m "feat(electron): Main process — 메인/로그 윈도우 매니저 + IPC bridge"
```

---

### Task 0.9: Electron Renderer 골격 (App.tsx + Sidebar + 빈 페이지들)

**Files:**
- Create: `electron/src/renderer/main.tsx`
- Create: `electron/src/renderer/App.tsx`
- Create: `electron/src/renderer/index.css`
- Create: `electron/src/renderer/components/Sidebar.tsx`
- Create: `electron/src/renderer/pages/Dashboard.tsx`
- Create: `electron/src/renderer/pages/Adapters.tsx`
- Create: `electron/src/renderer/pages/Logs.tsx`
- Create: `electron/src/renderer/pages/Diagnostics.tsx`
- Create: `electron/src/renderer/pages/Settings.tsx`
- Create: `electron/src/renderer/pages/Placeholder.tsx`
- Create: `electron/src/renderer/lib/api.ts`

- [ ] **Step 1: index.css — Tailwind base**

```css
/* electron/src/renderer/index.css */
@tailwind base;
@tailwind components;
@tailwind utilities;

html, body, #root {
  height: 100%;
  margin: 0;
  font-family: 'Inter', system-ui, sans-serif;
}
```

- [ ] **Step 2: main.tsx**

```tsx
// electron/src/renderer/main.tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import { Toaster } from "react-hot-toast";

import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <HashRouter>
      <App />
      <Toaster position="bottom-right" toastOptions={{ style: { background: "#161b22", color: "#c9d1d9", border: "1px solid #30363d" } }} />
    </HashRouter>
  </React.StrictMode>,
);
```

- [ ] **Step 3: lib/api.ts — axios 인스턴스**

```typescript
// electron/src/renderer/lib/api.ts
import axios from "axios";

const baseURL = window.kream?.apiBaseUrl ?? "http://localhost:8000";
const token = window.kream?.apiToken ?? "dev-token-replace-in-prod";

export const api = axios.create({
  baseURL,
  headers: { Authorization: `Bearer ${token}` },
  timeout: 5000,
});
```

- [ ] **Step 4: components/Sidebar.tsx**

```tsx
// electron/src/renderer/components/Sidebar.tsx
import {
  Activity, Cable, FileText, Settings as SettingsIcon, Stethoscope, Target, Zap,
} from "lucide-react";
import { NavLink } from "react-router-dom";

const items = [
  { to: "/", icon: Activity, label: "Dashboard" },
  { to: "/adapters", icon: Cable, label: "Adapters" },
  { to: "/logs", icon: FileText, label: "Logs", detachable: true },
  { to: "/storage-sale", icon: Target, label: "보관판매 자동등록", disabled: true },
  { to: "/auto-compete", icon: Zap, label: "자동경쟁", disabled: true },
  { to: "/diagnostics", icon: Stethoscope, label: "Diagnostics" },
  { to: "/settings", icon: SettingsIcon, label: "Settings" },
];

export function Sidebar({ onDetachLog }: { onDetachLog: () => void }) {
  return (
    <nav className="w-44 bg-sidebar border-r border-border flex flex-col">
      <div className="p-3 border-b border-border">
        <div className="text-xs uppercase tracking-wider text-text-muted">Kream Bot</div>
      </div>
      <ul className="flex-1 py-2">
        {items.map((it) => (
          <li key={it.to}>
            {it.disabled ? (
              <div className="px-3 py-2 text-xs text-text-muted cursor-not-allowed flex items-center gap-2 opacity-50">
                <it.icon size={14} />
                <span>{it.label}</span>
                <span className="ml-auto text-[9px] bg-card px-1.5 py-0.5 rounded">대기</span>
              </div>
            ) : (
              <NavLink
                to={it.to}
                className={({ isActive }) =>
                  `px-3 py-2 text-xs flex items-center gap-2 ${
                    isActive
                      ? "text-accent bg-card border-l-2 border-accent"
                      : "text-text-muted hover:text-text hover:bg-card/50"
                  }`
                }
              >
                <it.icon size={14} />
                <span>{it.label}</span>
                {it.detachable && (
                  <button
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      onDetachLog();
                    }}
                    className="ml-auto text-[9px] bg-card px-1.5 py-0.5 rounded hover:bg-accent/20"
                    title="별도 윈도우로 분리"
                  >
                    ⇱ Detach
                  </button>
                )}
              </NavLink>
            )}
          </li>
        ))}
      </ul>
      <div className="p-2 border-t border-border" id="sidebar-controls">
        {/* Phase A1 에서 StartStopButton 추가 */}
      </div>
    </nav>
  );
}
```

- [ ] **Step 5: 빈 페이지 5개 + Placeholder**

```tsx
// electron/src/renderer/pages/Dashboard.tsx
export default function Dashboard() {
  return <div className="p-6"><h1 className="text-2xl font-semibold">Dashboard</h1><p className="text-text-muted mt-2">Phase A3 에서 구현</p></div>;
}
```

```tsx
// electron/src/renderer/pages/Adapters.tsx
export default function Adapters() {
  return <div className="p-6"><h1 className="text-2xl font-semibold">Adapters</h1><p className="text-text-muted mt-2">Phase A2 에서 구현</p></div>;
}
```

```tsx
// electron/src/renderer/pages/Logs.tsx
export default function Logs() {
  return <div className="p-6"><h1 className="text-2xl font-semibold">Logs</h1><p className="text-text-muted mt-2">Phase A4 에서 구현 (또는 ⇱ Detach 로 분리 윈도우)</p></div>;
}
```

```tsx
// electron/src/renderer/pages/Diagnostics.tsx
export default function Diagnostics() {
  return <div className="p-6"><h1 className="text-2xl font-semibold">Diagnostics</h1><p className="text-text-muted mt-2">Phase A4 에서 구현</p></div>;
}
```

```tsx
// electron/src/renderer/pages/Settings.tsx
export default function Settings() {
  return <div className="p-6"><h1 className="text-2xl font-semibold">Settings</h1><p className="text-text-muted mt-2">Phase A4 에서 구현 (API URL/토큰 변경)</p></div>;
}
```

```tsx
// electron/src/renderer/pages/Placeholder.tsx
export default function Placeholder({ title, phase }: { title: string; phase: string }) {
  return (
    <div className="p-6 max-w-2xl">
      <h1 className="text-2xl font-semibold">{title}</h1>
      <div className="mt-4 p-4 bg-card border border-border rounded">
        <p className="text-text-muted">{phase} — 추후 사용자 디테일 합의 후 활성화</p>
      </div>
    </div>
  );
}
```

- [ ] **Step 6: App.tsx — Layout + Router**

```tsx
// electron/src/renderer/App.tsx
import { Route, Routes } from "react-router-dom";

import { Sidebar } from "./components/Sidebar";
import Adapters from "./pages/Adapters";
import Dashboard from "./pages/Dashboard";
import Diagnostics from "./pages/Diagnostics";
import Logs from "./pages/Logs";
import Placeholder from "./pages/Placeholder";
import Settings from "./pages/Settings";

export default function App() {
  return (
    <div className="flex h-full bg-bg text-text">
      <Sidebar onDetachLog={() => window.kream.openLogWindow()} />
      <main className="flex-1 overflow-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/adapters" element={<Adapters />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/storage-sale" element={<Placeholder title="🎯 보관판매 자동등록" phase="Phase B" />} />
          <Route path="/auto-compete" element={<Placeholder title="🤖 자동경쟁" phase="Phase C" />} />
          <Route path="/diagnostics" element={<Diagnostics />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
```

- [ ] **Step 7: dev 서버 sanity check**

봇이 가동 중이어야 함 (Task 0.6 후). 새 터미널에서:

```bash
cd electron && npm run dev
```
Expected: Vite dev server `localhost:5173` 가동 + Electron 윈도우 뜸 + 사이드바 + Dashboard 페이지 보임

확인 후 `Ctrl+C` 로 종료.

- [ ] **Step 8: 커밋**

```bash
cd /mnt/c/Users/USER/Desktop/크림봇
git add electron/src/renderer/
git commit -m "feat(electron): Renderer 골격 — 사이드바 + 빈 페이지 7개 + Placeholder"
```

---

### Task 0.10: Setup 완료 검증 + Setup 단계 종료 commit

**Files:** (검증만, 코드 변경 X)

- [ ] **Step 1: 봇 + Electron 동시 가동 e2e 검증**

봇 가동 (별도 터미널 또는 `&` background):
```bash
PYTHONPATH=. python main.py &
sleep 5
curl -s http://localhost:8000/api/health
```
Expected: `{"status":"ok","uptime_sec":5}`

Electron 가동 (별도 터미널):
```bash
cd electron && npm run dev
```
Expected: Electron 윈도우 + 사이드바 7개 메뉴 + Dashboard "Phase A3에서 구현" 보임

- [ ] **Step 2: 사이드바 모든 메뉴 클릭 동작 확인**

각 메뉴 클릭 시 페이지 전환 + 보관판매/자동경쟁 = 클릭 안 되고 "대기" 표시.

⇱ Detach 클릭 → 새 로그 윈도우 뜸 (빈 페이지) — 윈도우 종료 후 메인으로 돌아옴.

- [ ] **Step 3: 전체 테스트 한 번 실행**

```bash
PYTHONPATH=. pytest tests/api/ tests/test_main_api_launch.py -v
```
Expected: 모든 테스트 PASS

- [ ] **Step 4: Setup Phase 종료 마커 커밋 (선택, 코드 변경 0)**

`docs/superpowers/plans/2026-04-26-ui-bot-phase-a.md` 마지막에 `[Setup completed: YYYY-MM-DD HH:MM]` 추가 후 커밋:

```bash
git commit --allow-empty -m "checkpoint: Setup phase complete — Phase A1 진입 준비"
```

## Phase A1 — pause/resume + START/STOP UI

목표: UI 버튼으로 봇 일시 정지/재개. 6개 scheduler 루프가 `bot_state.paused` 체크.

### Task 1.1: /api/bot/pause + /api/bot/resume + /api/bot/status 라우터

**Files:**
- Create: `src/api/routers/bot.py`
- Modify: `src/api/server.py:21` (라우터 등록 추가)
- Create: `tests/api/test_bot_router.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_bot_router.py
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.server import app
from src.api.state import set_state
from src.config import settings
from src.models.database import init_db

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    await set_state("paused", "false")
    yield


@pytest.mark.asyncio
async def test_status_returns_running_state():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/bot/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is False
    assert "uptime_sec" in body


@pytest.mark.asyncio
async def test_pause_sets_state():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/bot/pause", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["paused"] is True


@pytest.mark.asyncio
async def test_resume_sets_state():
    await set_state("paused", "true")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/bot/resume", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["paused"] is False


@pytest.mark.asyncio
async def test_pause_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/bot/pause")  # no Authorization
    assert r.status_code == 401
```

- [ ] **Step 2: 테스트 실행 — 실패**

```bash
PYTHONPATH=. pytest tests/api/test_bot_router.py -v
```
Expected: FAIL — 라우터 없음

- [ ] **Step 3: bot.py 라우터 구현**

```python
# src/api/routers/bot.py
"""봇 제어 라우터 — pause / resume / status (Phase A1).

Phase A4 에서 restart / debug / update 추가 예정.
"""

import time

from fastapi import APIRouter, Depends

from src.api.auth import verify_token
from src.api.state import get_all_state, set_state

router = APIRouter(prefix="/api/bot", tags=["bot"])

_START_TIME = time.time()


@router.get("/status", dependencies=[Depends(verify_token)])
async def status() -> dict:
    """봇 현재 상태."""
    state = await get_all_state()
    return {
        "paused": state.get("paused", "false") == "true",
        "mode": state.get("mode", "24h_full"),
        "debug_mode": state.get("debug_mode", "false") == "true",
        "uptime_sec": int(time.time() - _START_TIME),
    }


@router.post("/pause", dependencies=[Depends(verify_token)])
async def pause() -> dict:
    await set_state("paused", "true")
    return {"paused": True}


@router.post("/resume", dependencies=[Depends(verify_token)])
async def resume() -> dict:
    await set_state("paused", "false")
    return {"paused": False}
```

- [ ] **Step 4: server.py 갱신 — 라우터 등록**

`src/api/server.py` 상단 import 부분에 추가:
```python
from src.api.routers import bot, ops
```

`create_app()` 안 `app.include_router(ops.router)` 다음 줄에 추가:
```python
    app.include_router(bot.router)
```

- [ ] **Step 5: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_bot_router.py -v
```
Expected: 4 passed

- [ ] **Step 6: 커밋**

```bash
git add src/api/routers/bot.py src/api/server.py tests/api/test_bot_router.py
git commit -m "feat(api): /api/bot/{pause,resume,status} — Phase A1 제어 라우터"
```

---

### Task 1.2: scheduler 6 루프 pause 체크 추가

**Files:**
- Modify: `src/scheduler.py` (각 `@tasks.loop` 함수 시작 부분)
- Create: `tests/test_scheduler_pause.py`

- [ ] **Step 1: scheduler 의 6개 루프 위치 확인**

```bash
grep -nE "@tasks\.loop|async def" src/scheduler.py | grep -B1 "_loop\|_task\|_runner" | head -40
```

루프 진입점 식별 (예: `tier1_loop`, `tier2_loop`, `daily_report_loop`, `kream_collector_loop`, `kream_refresh_loop`, `volume_spike_loop`).

- [ ] **Step 2: 실패 테스트 — pause flag 가 true 이면 루프 body 스킵**

```python
# tests/test_scheduler_pause.py
"""scheduler 가 bot_state.paused=true 일 때 본체 작업 스킵하는지 검증.

각 루프를 직접 호출하기 어려운 구조라, helper 함수로 추출된 _is_paused()
를 검증한다 (Step 3 에서 helper 추가).
"""

import pytest

from src.api.state import set_state
from src.models.database import init_db
from src.scheduler import _is_paused


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    await set_state("paused", "false")
    yield


@pytest.mark.asyncio
async def test_is_paused_false_default():
    assert await _is_paused() is False


@pytest.mark.asyncio
async def test_is_paused_true_after_set():
    await set_state("paused", "true")
    assert await _is_paused() is True
```

- [ ] **Step 3: 테스트 실행 — 실패**

```bash
PYTHONPATH=. pytest tests/test_scheduler_pause.py -v
```
Expected: FAIL — `_is_paused` 미존재

- [ ] **Step 4: scheduler.py 에 helper 추가**

`src/scheduler.py` 의 import 블록 끝부분에 추가:

```python
from src.api.state import get_state


async def _is_paused() -> bool:
    """bot_state.paused 체크 — UI 시작/중지 토글."""
    val = await get_state("paused")
    return val == "true"
```

- [ ] **Step 5: scheduler 의 6개 루프 각 진입 부분에 pause guard 추가**

각 `@tasks.loop` 데코레이터로 등록된 async 함수 시작 부분에 다음 추가:

```python
        if await _is_paused():
            return  # UI에서 일시 정지 — 다음 tick 까지 대기
```

위치 (각 함수 첫 try 또는 작업 코드 직전):
- `tier1_loop` (워치리스트 빌더)
- `tier2_loop` (실시간 폴링)
- `daily_report_loop`
- `kream_collector_loop` (신규 수집)
- `kream_refresh_loop` (시세 갱신)
- `volume_spike_loop` (급등 감지)
- `continuous_loop` (Tier 0 v2 연속 배치) — 존재 시

(실제 함수명은 Step 1 에서 확인한 이름 사용)

- [ ] **Step 6: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/test_scheduler_pause.py -v
```
Expected: 2 passed

- [ ] **Step 7: 봇 회귀 테스트**

```bash
PYTHONPATH=. pytest tests/ -v --ignore=tests/integration
```
Expected: 모든 기존 테스트 그대로 PASS (회귀 0)

- [ ] **Step 8: 커밋**

```bash
git add src/scheduler.py tests/test_scheduler_pause.py
git commit -m "feat(scheduler): 6 루프 pause 체크 추가 — UI 일시정지 통합"
```

---

### Task 1.3: V3Runtime adapter dispatch 측 pause 체크

**Files:**
- Modify: `src/core/runtime.py` 또는 `src/core/orchestrator.py` (어댑터 dispatch 진입 부분)
- Create: `tests/core/test_runtime_pause.py`

- [ ] **Step 1: dispatch 진입점 확인**

```bash
grep -nE "async def.*dispatch\|async def.*tick\|async def.*run_once" src/core/runtime.py src/core/orchestrator.py 2>/dev/null | head -20
```

어댑터 trigger 가 일어나는 메서드 식별 (예: `V3Runtime._adapter_tick`, `Orchestrator.dispatch`).

- [ ] **Step 2: 실패 테스트**

```python
# tests/core/test_runtime_pause.py
"""V3Runtime 의 adapter dispatch 가 paused 상태에서 스킵되는지 검증."""

import pytest
from unittest.mock import AsyncMock, patch

from src.api.state import set_state
from src.models.database import init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    await set_state("paused", "false")
    yield


@pytest.mark.asyncio
async def test_runtime_skips_when_paused():
    from src.core.runtime import V3Runtime, _is_paused_guard
    await set_state("paused", "true")
    assert await _is_paused_guard() is True


@pytest.mark.asyncio
async def test_runtime_runs_when_not_paused():
    from src.core.runtime import _is_paused_guard
    assert await _is_paused_guard() is False
```

- [ ] **Step 3: 실패 확인**

```bash
PYTHONPATH=. pytest tests/core/test_runtime_pause.py -v
```
Expected: FAIL — `_is_paused_guard` 미존재

- [ ] **Step 4: runtime.py 갱신 — guard 추가 + adapter dispatch 진입점에 적용**

`src/core/runtime.py` 상단에 추가:

```python
from src.api.state import get_state


async def _is_paused_guard() -> bool:
    """V3Runtime 의 adapter dispatch 진입 전 pause 체크.

    UI 에서 paused=true 면 어댑터 호출 스킵 (이미 실행 중인 task 는 자연 종료).
    """
    return (await get_state("paused")) == "true"
```

V3Runtime 의 adapter tick / dispatch 함수 (Step 1 에서 식별) 진입 부분에 추가:

```python
        if await _is_paused_guard():
            return
```

- [ ] **Step 5: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/core/test_runtime_pause.py -v
```
Expected: 2 passed

- [ ] **Step 6: 봇 코어 회귀 검증**

```bash
PYTHONPATH=. pytest tests/core/ -v
```
Expected: 모든 코어 테스트 PASS

- [ ] **Step 7: 커밋**

```bash
git add src/core/runtime.py tests/core/test_runtime_pause.py
git commit -m "feat(runtime): V3Runtime adapter dispatch 측 pause guard"
```

---

### Task 1.4: Electron StartStopButton 컴포넌트

**Files:**
- Create: `electron/src/renderer/components/StartStopButton.tsx`
- Modify: `electron/src/renderer/components/Sidebar.tsx` (sidebar-controls 영역에 삽입)
- Create: `electron/src/renderer/lib/store.ts` (Zustand)

- [ ] **Step 1: store.ts (Zustand) 생성 — 봇 상태 전역 store**

```typescript
// electron/src/renderer/lib/store.ts
import { create } from "zustand";

interface BotState {
  paused: boolean;
  mode: string;
  uptime_sec: number;
  debug_mode: boolean;
  setStatus: (s: Partial<BotState>) => void;
}

export const useBotStore = create<BotState>((set) => ({
  paused: false,
  mode: "24h_full",
  uptime_sec: 0,
  debug_mode: false,
  setStatus: (s) => set(s),
}));
```

- [ ] **Step 2: StartStopButton.tsx 구현**

```tsx
// electron/src/renderer/components/StartStopButton.tsx
import { Pause, Play } from "lucide-react";
import { useEffect } from "react";
import toast from "react-hot-toast";

import { api } from "../lib/api";
import { useBotStore } from "../lib/store";

export function StartStopButton() {
  const paused = useBotStore((s) => s.paused);
  const setStatus = useBotStore((s) => s.setStatus);

  useEffect(() => {
    api
      .get("/api/bot/status")
      .then((r) => setStatus(r.data))
      .catch(() => {});
    const t = setInterval(() => {
      api
        .get("/api/bot/status")
        .then((r) => setStatus(r.data))
        .catch(() => {});
    }, 5000);
    return () => clearInterval(t);
  }, [setStatus]);

  const onToggle = async () => {
    const action = paused ? "resume" : "pause";
    try {
      const r = await api.post(`/api/bot/${action}`);
      setStatus({ paused: r.data.paused });
      toast.success(paused ? "봇 재개됨 ▶" : "봇 일시정지 ⏸");
    } catch (e) {
      toast.error(`실패: ${(e as Error).message}`);
    }
  };

  return (
    <button
      onClick={onToggle}
      className={`w-full flex items-center justify-center gap-2 py-2 px-3 rounded text-xs font-semibold transition-colors ${
        paused
          ? "bg-ok hover:bg-ok/80 text-black"
          : "bg-err hover:bg-err/80 text-white"
      }`}
    >
      {paused ? <><Play size={12} /> START</> : <><Pause size={12} /> STOP</>}
    </button>
  );
}
```

- [ ] **Step 3: Sidebar.tsx 갱신 — controls 영역에 button 삽입**

`electron/src/renderer/components/Sidebar.tsx` 의 `<div className="p-2 border-t border-border" id="sidebar-controls">` 블록 안에 다음 추가:

```tsx
      <div className="p-2 border-t border-border">
        <StartStopButton />
      </div>
```

상단 import 추가:
```tsx
import { StartStopButton } from "./StartStopButton";
```

(기존 빈 sidebar-controls div 는 삭제하고 위 코드로 교체)

- [ ] **Step 4: 봇 + Electron 동시 가동 e2e 검증**

봇 가동 중인 상태에서:
```bash
cd electron && npm run dev
```

Electron 윈도우에서:
- 사이드바 하단에 빨간 ⏸ STOP 버튼 보임
- 클릭 → 토스트 "봇 일시정지 ⏸" + 버튼이 초록 ▶ START 로 변경
- 다시 클릭 → 토스트 "봇 재개됨 ▶" + 빨간 ⏸ STOP 으로 복귀

- [ ] **Step 5: 봇 측 로그 확인**

봇 로그 (`tail -f logs/...`) 에 STOP 동안 scheduler 루프가 "skipped (paused)" 메시지가 나오거나 단순히 작업 0건 처리되어야 함.

- [ ] **Step 6: 커밋**

```bash
cd /mnt/c/Users/USER/Desktop/크림봇
git add electron/src/renderer/components/StartStopButton.tsx electron/src/renderer/components/Sidebar.tsx electron/src/renderer/lib/store.ts
git commit -m "feat(electron): StartStopButton + Zustand store — Phase A1 UI"
```

---

### Task 1.5: Phase A1 e2e 검증 + checkpoint commit

**Files:** (검증만)

- [ ] **Step 1: 5회 START/STOP 사이클**

Electron 에서 STOP → 30초 대기 (scheduler 루프 1 tick) → START → 30초 대기 → 반복 5회.

확인 사항:
- 봇 프로세스 죽지 않음 (PID 유지)
- 봇 로그에 "skipped (paused)" 또는 작업 0건 (STOP 동안)
- START 후 정상 어댑터 dispatch 재개 (decision_log 새 entry)

- [ ] **Step 2: 봇 코어 회귀 테스트**

```bash
PYTHONPATH=. pytest tests/ -v --ignore=tests/integration
```
Expected: 모든 테스트 PASS

- [ ] **Step 3: Phase A1 완료 마커 commit**

```bash
git commit --allow-empty -m "checkpoint: Phase A1 (pause/resume) complete — Phase A2 진입 준비"
```

## Phase A2 — per-source 토글 + 모드 시스템

목표: 22 어댑터 중 사용자 선택만 활성화. 24h 모드 (전체 활성) ↔ Selective 모드 (선택만).

### Task 2.1: registry.py UI flag 통합

**Files:**
- Modify: `src/crawlers/registry.py` (enabled 체크에 UI flag 추가)
- Create: `src/api/source_state.py` (소싱처별 ON/OFF + 모드 helper)
- Create: `tests/api/test_source_state.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_source_state.py
import pytest

from src.api.source_state import (
    get_enabled_sources, set_source_enabled, set_mode,
    is_source_active, get_mode,
)
from src.api.state import set_state
from src.models.database import init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    await set_state("mode", "24h_full")
    await set_state("enabled_sources", "[]")
    yield


@pytest.mark.asyncio
async def test_24h_mode_all_active():
    """24h 모드: 모든 소싱처 active=True (enabled_sources 무시)."""
    assert await is_source_active("musinsa") is True
    assert await is_source_active("anything") is True


@pytest.mark.asyncio
async def test_selective_mode_only_listed_active():
    await set_mode("selective")
    await set_source_enabled("musinsa", True)
    await set_source_enabled("nike", True)
    assert await is_source_active("musinsa") is True
    assert await is_source_active("nike") is True
    assert await is_source_active("adidas") is False


@pytest.mark.asyncio
async def test_get_enabled_sources_returns_list():
    await set_source_enabled("musinsa", True)
    await set_source_enabled("nike", True)
    enabled = await get_enabled_sources()
    assert "musinsa" in enabled
    assert "nike" in enabled


@pytest.mark.asyncio
async def test_disable_source_removes_from_list():
    await set_source_enabled("musinsa", True)
    await set_source_enabled("musinsa", False)
    enabled = await get_enabled_sources()
    assert "musinsa" not in enabled


@pytest.mark.asyncio
async def test_get_mode_returns_current():
    await set_mode("selective")
    assert await get_mode() == "selective"
```

- [ ] **Step 2: 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_source_state.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: source_state.py 구현**

```python
# src/api/source_state.py
"""소싱처 ON/OFF + 모드 (24h_full / selective) helper.

모드:
- 24h_full: 모든 소싱처 active (enabled_sources 무시) — 기본값
- selective: enabled_sources JSON 배열에 있는 소싱처만 active

bot_state 키:
- mode: "24h_full" | "selective"
- enabled_sources: JSON 배열 문자열 (예: '["musinsa","nike"]')
"""

from __future__ import annotations

import json

from src.api.state import get_state, set_state


async def get_mode() -> str:
    return await get_state("mode") or "24h_full"


async def set_mode(mode: str) -> None:
    if mode not in ("24h_full", "selective"):
        raise ValueError(f"invalid mode: {mode}")
    await set_state("mode", mode)


async def get_enabled_sources() -> list[str]:
    raw = await get_state("enabled_sources") or "[]"
    try:
        return list(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return []


async def set_source_enabled(source: str, enabled: bool) -> None:
    current = await get_enabled_sources()
    if enabled and source not in current:
        current.append(source)
    elif not enabled and source in current:
        current.remove(source)
    await set_state("enabled_sources", json.dumps(current))


async def is_source_active(source: str) -> bool:
    """모드 + enabled_sources 종합 판정."""
    mode = await get_mode()
    if mode == "24h_full":
        return True
    enabled = await get_enabled_sources()
    return source in enabled
```

- [ ] **Step 4: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_source_state.py -v
```
Expected: 5 passed

- [ ] **Step 5: registry.py 통합 — adapter dispatch 진입 시 UI 활성 체크**

`src/crawlers/registry.py` 의 어댑터 활성 체크 함수 (예: `get_active_adapters` 또는 dispatch 진입점) 식별:

```bash
grep -nE "def.*active|def.*dispatch|circuit_breaker" src/crawlers/registry.py | head
```

dispatch 시점에 다음 패턴 추가 (서킷브레이커 체크 옆):

```python
        # UI 측 per-source 토글 — Phase A2
        from src.api.source_state import is_source_active
        if not await is_source_active(source_name):
            continue  # 또는 return False (함수 형태에 맞게)
```

(파일 구조에 따라 동기/비동기 컨텍스트 정확히 맞춤. registry 가 동기면 sync wrapper 추가 필요)

- [ ] **Step 6: 회귀 검증**

```bash
PYTHONPATH=. pytest tests/ -v --ignore=tests/integration
```
Expected: 모든 테스트 PASS

- [ ] **Step 7: 커밋**

```bash
git add src/api/source_state.py src/crawlers/registry.py tests/api/test_source_state.py
git commit -m "feat(api): per-source 토글 + 모드 (24h/selective) state helper + registry 통합"
```

---

### Task 2.2: /api/sources/* 라우터

**Files:**
- Create: `src/api/routers/sources.py`
- Modify: `src/api/server.py` (라우터 등록)
- Create: `tests/api/test_sources_router.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_sources_router.py
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.server import app
from src.api.state import set_state
from src.config import settings
from src.models.database import init_db

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    await set_state("mode", "24h_full")
    await set_state("enabled_sources", "[]")
    yield


@pytest.mark.asyncio
async def test_list_returns_all_sources_with_status():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/sources/list", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "sources" in body
    assert "mode" in body
    # 22 어댑터 (또는 현재 등록된 수) 모두 포함
    assert len(body["sources"]) >= 15


@pytest.mark.asyncio
async def test_enable_disable_source_roundtrip():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r1 = await ac.post("/api/sources/musinsa/enable", headers=AUTH)
        assert r1.status_code == 200
        assert "musinsa" in r1.json()["enabled_sources"]

        r2 = await ac.post("/api/sources/musinsa/disable", headers=AUTH)
        assert r2.status_code == 200
        assert "musinsa" not in r2.json()["enabled_sources"]


@pytest.mark.asyncio
async def test_set_mode():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/sources/mode", json={"mode": "selective"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["mode"] == "selective"


@pytest.mark.asyncio
async def test_set_mode_invalid_returns_400():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/sources/mode", json={"mode": "bogus"}, headers=AUTH)
    assert r.status_code == 400
```

- [ ] **Step 2: 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_sources_router.py -v
```
Expected: FAIL

- [ ] **Step 3: sources.py 라우터 구현**

```python
# src/api/routers/sources.py
"""소싱처 ON/OFF + 모드 토글 라우터 (Phase A2)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.auth import verify_token
from src.api.source_state import (
    get_enabled_sources, get_mode, is_source_active,
    set_mode, set_source_enabled,
)
from src.crawlers.registry import REGISTRY  # 22 어댑터 레지스트리

router = APIRouter(prefix="/api/sources", tags=["sources"])


class ModePayload(BaseModel):
    mode: str  # "24h_full" | "selective"


@router.get("/list", dependencies=[Depends(verify_token)])
async def list_sources() -> dict:
    """등록된 모든 소싱처 + UI 활성 상태 반환."""
    mode = await get_mode()
    enabled = set(await get_enabled_sources())
    sources = []
    for name in REGISTRY.keys():  # registry.py 의 정확한 attribute 명에 맞춰 조정
        sources.append({
            "name": name,
            "ui_enabled": name in enabled,
            "active": await is_source_active(name),
            # circuit_breaker 상태 등은 Phase A3 dashboard 에서 확장
        })
    return {"sources": sources, "mode": mode, "enabled_sources": list(enabled)}


@router.post("/{source}/enable", dependencies=[Depends(verify_token)])
async def enable(source: str) -> dict:
    await set_source_enabled(source, True)
    return {"enabled_sources": await get_enabled_sources()}


@router.post("/{source}/disable", dependencies=[Depends(verify_token)])
async def disable(source: str) -> dict:
    await set_source_enabled(source, False)
    return {"enabled_sources": await get_enabled_sources()}


@router.post("/mode", dependencies=[Depends(verify_token)])
async def update_mode(payload: ModePayload) -> dict:
    try:
        await set_mode(payload.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"mode": payload.mode}
```

(주의: `REGISTRY` import 가 실제 registry.py 의 export 명과 다르면 — `grep -nE "^REGISTRY|^_registry|^adapters" src/crawlers/registry.py` 로 확인 후 조정)

- [ ] **Step 4: server.py 갱신**

```python
from src.api.routers import bot, ops, sources
```

`create_app()` 안에 추가:
```python
    app.include_router(sources.router)
```

- [ ] **Step 5: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_sources_router.py -v
```
Expected: 4 passed

- [ ] **Step 6: 커밋**

```bash
git add src/api/routers/sources.py src/api/server.py tests/api/test_sources_router.py
git commit -m "feat(api): /api/sources/* — 소싱처 토글 + 모드 라우터"
```

---

### Task 2.3: Electron Adapters 페이지 (그리드 + 스위치 + 모드 토글)

**Files:**
- Modify: `electron/src/renderer/pages/Adapters.tsx`
- Create: `electron/src/renderer/components/AdapterCard.tsx`
- Create: `electron/src/renderer/components/ModeToggle.tsx`

- [ ] **Step 1: ModeToggle.tsx**

```tsx
// electron/src/renderer/components/ModeToggle.tsx
import { Globe, Filter } from "lucide-react";
import toast from "react-hot-toast";

import { api } from "../lib/api";

interface Props {
  mode: string;
  onChange: (mode: string) => void;
}

export function ModeToggle({ mode, onChange }: Props) {
  const setMode = async (next: string) => {
    try {
      await api.post("/api/sources/mode", { mode: next });
      onChange(next);
      toast.success(`모드 변경: ${next === "24h_full" ? "24h 전체" : "선택 모드"}`);
    } catch (e) {
      toast.error(`실패: ${(e as Error).message}`);
    }
  };

  return (
    <div className="inline-flex bg-card border border-border rounded p-1">
      <button
        onClick={() => setMode("24h_full")}
        className={`px-3 py-1.5 text-xs rounded flex items-center gap-1.5 transition-colors ${
          mode === "24h_full" ? "bg-accent/20 text-accent" : "text-text-muted hover:text-text"
        }`}
      >
        <Globe size={12} /> 24h 전체
      </button>
      <button
        onClick={() => setMode("selective")}
        className={`px-3 py-1.5 text-xs rounded flex items-center gap-1.5 transition-colors ${
          mode === "selective" ? "bg-accent/20 text-accent" : "text-text-muted hover:text-text"
        }`}
      >
        <Filter size={12} /> 선택 모드
      </button>
    </div>
  );
}
```

- [ ] **Step 2: AdapterCard.tsx**

```tsx
// electron/src/renderer/components/AdapterCard.tsx
import toast from "react-hot-toast";

import { api } from "../lib/api";

interface Source {
  name: string;
  ui_enabled: boolean;
  active: boolean;
}

interface Props {
  source: Source;
  selectiveMode: boolean;
  onToggle: () => void;
}

export function AdapterCard({ source, selectiveMode, onToggle }: Props) {
  const onClick = async () => {
    const action = source.ui_enabled ? "disable" : "enable";
    try {
      await api.post(`/api/sources/${source.name}/${action}`);
      onToggle();
    } catch (e) {
      toast.error(`${source.name} ${action} 실패`);
    }
  };

  const dotColor = source.active ? "bg-ok" : "bg-text-muted";

  return (
    <button
      onClick={onClick}
      disabled={!selectiveMode}
      className={`bg-card border rounded p-3 flex flex-col gap-1 transition-all text-left ${
        selectiveMode
          ? "border-border hover:border-accent cursor-pointer"
          : "border-border opacity-60 cursor-not-allowed"
      } ${source.ui_enabled ? "ring-1 ring-accent/40" : ""}`}
    >
      <div className="flex items-center gap-1.5">
        <span className={`w-2 h-2 rounded-full ${dotColor}`} />
        <span className="text-xs font-mono">{source.name}</span>
      </div>
      <div className="text-[9px] text-text-muted uppercase tracking-wide">
        {source.active ? "ACTIVE" : "INACTIVE"}
      </div>
    </button>
  );
}
```

- [ ] **Step 3: Adapters.tsx 페이지 구현**

```tsx
// electron/src/renderer/pages/Adapters.tsx
import { useEffect, useState } from "react";

import { AdapterCard } from "../components/AdapterCard";
import { ModeToggle } from "../components/ModeToggle";
import { api } from "../lib/api";

interface Source {
  name: string;
  ui_enabled: boolean;
  active: boolean;
}

export default function Adapters() {
  const [sources, setSources] = useState<Source[]>([]);
  const [mode, setMode] = useState("24h_full");

  const fetchData = async () => {
    const r = await api.get("/api/sources/list");
    setSources(r.data.sources);
    setMode(r.data.mode);
  };

  useEffect(() => {
    fetchData();
  }, []);

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold">Adapters ({sources.length})</h1>
        <ModeToggle mode={mode} onChange={(m) => { setMode(m); fetchData(); }} />
      </div>
      <p className="text-text-muted text-xs mb-4">
        {mode === "24h_full"
          ? "전체 모드 — 모든 소싱처 활성. 카드 클릭 비활성."
          : "선택 모드 — 카드 클릭하여 활성화/비활성화"}
      </p>
      <div className="grid grid-cols-4 lg:grid-cols-6 gap-2">
        {sources.map((s) => (
          <AdapterCard
            key={s.name}
            source={s}
            selectiveMode={mode === "selective"}
            onToggle={fetchData}
          />
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: e2e 검증**

봇 + Electron 가동 후:
- 사이드바 "Adapters" 클릭 → 그리드 표시
- 모드 = 24h 전체 → 모든 카드 ACTIVE 녹색
- 모드 변경 (선택 모드) → 토스트 + 카드 클릭 가능
- musinsa 클릭 → ring 추가 + ACTIVE
- 봇 로그에서 musinsa dispatch 만 일어나는지 확인

- [ ] **Step 5: 봇 회귀 검증**

```bash
PYTHONPATH=. pytest tests/ -v --ignore=tests/integration
```
Expected: 모든 테스트 PASS

- [ ] **Step 6: 커밋**

```bash
git add electron/src/renderer/pages/Adapters.tsx electron/src/renderer/components/AdapterCard.tsx electron/src/renderer/components/ModeToggle.tsx
git commit -m "feat(electron): Adapters 페이지 — 그리드 + 모드 토글 + per-source 스위치"
```

---

### Task 2.4: Phase A2 e2e + checkpoint

- [ ] **Step 1: 모드 전환 5회 검증**

24h ↔ selective 5회 전환 + selective 모드에서 1~3개 소싱처만 활성.

확인:
- 30초 (Tier2 1 cycle) 동안 활성 소싱처만 dispatch (decision_log 확인)
- 비활성 소싱처 0건

- [ ] **Step 2: Phase A2 완료 마커**

```bash
git commit --allow-empty -m "checkpoint: Phase A2 (per-source toggle + mode) complete"
```

## Phase A3 — 메인 대시보드 (status cards + adapter grid + alerts + 차트)

목표: 한눈에 봇 상태 파악. 5초 주기 status 갱신 + 알림 실시간 push + 24h 차트.

### Task 3.1: /api/dashboard/snapshot — 초기 로드 데이터

**Files:**
- Create: `src/api/routers/dashboard.py`
- Modify: `src/api/server.py` (라우터 등록)
- Create: `tests/api/test_dashboard.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_dashboard.py
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.server import app
from src.config import settings
from src.models.database import init_db

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    yield


@pytest.mark.asyncio
async def test_dashboard_snapshot_schema():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/dashboard/snapshot", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    # 필수 필드
    assert "kream_calls_24h" in body
    assert "kream_calls_cap" in body
    assert "alerts_24h" in body
    assert "matches_6h" in body
    assert "queue_pending" in body
    assert "adapters" in body
    assert "recent_alerts" in body
    assert isinstance(body["adapters"], list)
    assert isinstance(body["recent_alerts"], list)
```

- [ ] **Step 2: 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_dashboard.py -v
```
Expected: FAIL

- [ ] **Step 3: dashboard.py 구현**

```python
# src/api/routers/dashboard.py
"""대시보드 라우터 — snapshot (초기 로드) + WebSocket (실시간 push)."""

import aiosqlite
from fastapi import APIRouter, Depends

from src.api.auth import verify_token
from src.api.source_state import is_source_active
from src.config import settings
from src.crawlers.registry import REGISTRY

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/snapshot", dependencies=[Depends(verify_token)])
async def snapshot() -> dict:
    """초기 대시보드 로드 — status cards + adapter list + recent alerts."""
    async with aiosqlite.connect(settings.db_path) as db:
        # 크림 호출 24h
        cur = await db.execute(
            "SELECT COUNT(*) FROM kream_api_calls WHERE ts > datetime('now','-24 hours')"
        )
        kream_calls_24h = (await cur.fetchone())[0]

        # 알림 24h
        cur = await db.execute(
            "SELECT COUNT(*) FROM alert_sent WHERE fired_at > strftime('%s','now') - 86400"
        )
        alerts_24h = (await cur.fetchone())[0]

        # 매칭 6h (decision_log profit_emitted)
        cur = await db.execute(
            "SELECT COUNT(*) FROM decision_log "
            "WHERE ts > datetime('now','-6 hours') AND stage='profit_emitted'"
        )
        matches_6h = (await cur.fetchone())[0]

        # 큐 적체
        cur = await db.execute(
            "SELECT COUNT(*) FROM kream_collect_queue WHERE status='pending'"
        )
        queue_pending = (await cur.fetchone())[0]

        # 최근 알림 10건
        cur = await db.execute(
            "SELECT signal, source, model_no, net_profit, fired_at "
            "FROM alert_sent ORDER BY fired_at DESC LIMIT 10"
        )
        alert_rows = await cur.fetchall()

    recent_alerts = [
        {
            "signal": r[0],
            "source": r[1],
            "model_no": r[2],
            "net_profit": r[3],
            "fired_at": r[4],
        }
        for r in alert_rows
    ]

    adapters = []
    for name in REGISTRY.keys():
        adapters.append({
            "name": name,
            "active": await is_source_active(name),
            # 헬스 색상 = 다음 task 에서 서킷브레이커 상태 + 최근 매칭 수 통합
        })

    return {
        "kream_calls_24h": kream_calls_24h,
        "kream_calls_cap": settings.kream_daily_cap if hasattr(settings, "kream_daily_cap") else 50000,
        "alerts_24h": alerts_24h,
        "matches_6h": matches_6h,
        "queue_pending": queue_pending,
        "adapters": adapters,
        "recent_alerts": recent_alerts,
    }
```

(주의: `kream_daily_cap` 이 settings 에 없으면 추가 필요. `grep -E "daily_cap|KREAM_DAILY" src/config.py` 로 확인)

- [ ] **Step 4: server.py 갱신**

```python
from src.api.routers import bot, dashboard, ops, sources
```
```python
    app.include_router(dashboard.router)
```

- [ ] **Step 5: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_dashboard.py -v
```
Expected: 1 passed

- [ ] **Step 6: 커밋**

```bash
git add src/api/routers/dashboard.py src/api/server.py tests/api/test_dashboard.py
git commit -m "feat(api): /api/dashboard/snapshot — 초기 로드 데이터"
```

---

### Task 3.2: WebSocket /ws/status — 5초 주기 status push

**Files:**
- Modify: `src/api/routers/dashboard.py` (WebSocket endpoint 추가)
- Create: `src/api/status_broadcaster.py` (5초 주기 broadcast loop)
- Modify: `main.py` (status broadcaster task 추가)
- Create: `tests/api/test_status_broadcast.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_status_broadcast.py
import asyncio

import pytest

from src.api.status_broadcaster import _build_status_payload
from src.models.database import init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    yield


@pytest.mark.asyncio
async def test_build_status_payload_returns_all_fields():
    payload = await _build_status_payload()
    assert "kream_calls_24h" in payload
    assert "alerts_24h" in payload
    assert "matches_6h" in payload
    assert "paused" in payload
    assert "uptime_sec" in payload
    assert "ts" in payload
```

- [ ] **Step 2: 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_status_broadcast.py -v
```
Expected: FAIL

- [ ] **Step 3: status_broadcaster.py 구현**

```python
# src/api/status_broadcaster.py
"""5초 주기로 /ws/status 채널에 봇 상태 push."""

import asyncio
import time

import aiosqlite

from src.api.state import get_all_state
from src.api.ws_manager import ws_manager
from src.config import settings
from src.utils.logging import setup_logger

logger = setup_logger("status_broadcaster")

_START_TIME = time.time()


async def _build_status_payload() -> dict:
    """현재 봇 상태 + 핵심 지표 계산."""
    state = await get_all_state()
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM kream_api_calls WHERE ts > datetime('now','-24 hours')"
        )
        kream_calls_24h = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM alert_sent WHERE fired_at > strftime('%s','now') - 86400"
        )
        alerts_24h = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM decision_log "
            "WHERE ts > datetime('now','-6 hours') AND stage='profit_emitted'"
        )
        matches_6h = (await cur.fetchone())[0]
    return {
        "kream_calls_24h": kream_calls_24h,
        "alerts_24h": alerts_24h,
        "matches_6h": matches_6h,
        "paused": state.get("paused", "false") == "true",
        "mode": state.get("mode", "24h_full"),
        "uptime_sec": int(time.time() - _START_TIME),
        "ts": int(time.time()),
    }


async def status_broadcast_loop(interval_sec: int = 5) -> None:
    """봇 lifespan 동안 무한 루프 — interval_sec 주기로 broadcast."""
    logger.info("[status_broadcaster] start (interval=%ds)", interval_sec)
    while True:
        try:
            payload = await _build_status_payload()
            await ws_manager.broadcast("status", payload)
        except Exception:
            logger.exception("[status_broadcaster] payload build/broadcast failed")
        await asyncio.sleep(interval_sec)
```

- [ ] **Step 4: WebSocket endpoint 추가 (dashboard.py)**

`src/api/routers/dashboard.py` 에 추가:

```python
from fastapi import WebSocket, WebSocketDisconnect, Query

from src.api.ws_manager import ws_manager
from src.config import settings


@router.websocket("/ws/status")
async def ws_status(ws: WebSocket, token: str = Query(default="")):
    """WebSocket: 5초 주기 봇 상태 push. token 쿼리 파라미터로 인증."""
    if token != settings.api_token:
        await ws.close(code=1008, reason="invalid token")
        return
    await ws_manager.connect("status", ws)
    try:
        while True:
            await ws.receive_text()  # client ping/keep-alive
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect("status", ws)
```

- [ ] **Step 5: main.py 갱신 — broadcaster task 추가**

`_main_async` 에서 `api_task = asyncio.create_task(...)` 다음에 추가:

```python
    from src.api.status_broadcaster import status_broadcast_loop
    status_task = asyncio.create_task(status_broadcast_loop(5), name="status-broadcaster")
```

`finally` 블록 안 `api_task.cancel()` 다음에 추가:
```python
        status_task.cancel()
```

- [ ] **Step 6: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_status_broadcast.py -v
```
Expected: 1 passed

- [ ] **Step 7: 수동 e2e — wscat 또는 브라우저로 WebSocket 확인**

봇 가동 후:
```bash
# wscat 없으면 npx wscat 설치
npx wscat -c "ws://localhost:8000/api/dashboard/ws/status?token=$API_TOKEN"
```
Expected: 5초마다 JSON 메시지 수신 (kream_calls_24h, alerts_24h 등 포함)

- [ ] **Step 8: 커밋**

```bash
git add src/api/status_broadcaster.py src/api/routers/dashboard.py main.py tests/api/test_status_broadcast.py
git commit -m "feat(api): WebSocket /ws/status — 5초 주기 broadcast + main.py 통합"
```

---

### Task 3.3: alert_sent → /ws/alerts broadcast hook

**Files:**
- Modify: `src/core/runtime.py` 또는 알림 발사 지점 (alert_sent insert 직후)
- Modify: `src/api/routers/dashboard.py` (/ws/alerts endpoint 추가)
- Create: `tests/api/test_alert_broadcast.py`

- [ ] **Step 1: 알림 발사 지점 확인**

```bash
grep -nE "alert_sent.*INSERT|INSERT INTO alert_sent" src/ -r | head
```

- [ ] **Step 2: WebSocket alerts endpoint 추가**

`src/api/routers/dashboard.py` 에 추가:

```python
@router.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket, token: str = Query(default="")):
    if token != settings.api_token:
        await ws.close(code=1008, reason="invalid token")
        return
    await ws_manager.connect("alerts", ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect("alerts", ws)
```

- [ ] **Step 3: 알림 발사 hook 추가**

알림 발사 코드 (Step 1 에서 식별) 의 `INSERT INTO alert_sent` 직후에:

```python
        try:
            from src.api.ws_manager import ws_manager
            await ws_manager.broadcast("alerts", {
                "signal": signal,        # STRONG_BUY / BUY / WATCH
                "source": source,
                "model_no": model_no,
                "net_profit": net_profit,
                "kream_url": kream_url,  # 가능한 경우
                "ts": int(time.time()),
            })
        except Exception:
            logger.warning("[ws] alert broadcast 실패", exc_info=True)
```

- [ ] **Step 4: 통합 테스트 — broadcast 가 호출되는지 mock 으로 검증**

```python
# tests/api/test_alert_broadcast.py
import pytest
from unittest.mock import AsyncMock, patch

from src.api.ws_manager import ws_manager


@pytest.mark.asyncio
async def test_ws_manager_broadcast_alert():
    # 실제 알림 발사 함수를 호출하는 대신, broadcast helper 직접 테스트
    payload = {"signal": "BUY", "source": "musinsa", "model_no": "TEST123"}
    fake_ws = AsyncMock()
    fake_ws.send_json = AsyncMock()
    await ws_manager.connect("alerts", fake_ws)
    await ws_manager.broadcast("alerts", payload)
    fake_ws.send_json.assert_called_once_with(payload)
    ws_manager.disconnect("alerts", fake_ws)
```

- [ ] **Step 5: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_alert_broadcast.py -v
```
Expected: 1 passed

- [ ] **Step 6: 봇 회귀 검증 + 실 알림 e2e (선택)**

봇 가동 + Electron 가동 + 실 알림 발생 시 (수동 트리거 어려우면 dry-run):
- wscat 으로 `/ws/alerts` 구독 → 알림 발생 시 메시지 수신 확인

- [ ] **Step 7: 커밋**

```bash
git add src/api/routers/dashboard.py src/core/runtime.py tests/api/test_alert_broadcast.py
git commit -m "feat(api): /ws/alerts — 알림 발사 시 실시간 broadcast"
```

---

### Task 3.4: Electron Dashboard 페이지 + Status Cards + Adapter Grid + Recent Alerts

**Files:**
- Modify: `electron/src/renderer/pages/Dashboard.tsx`
- Create: `electron/src/renderer/components/StatusCard.tsx`
- Create: `electron/src/renderer/components/AdapterGrid.tsx`
- Create: `electron/src/renderer/components/RecentAlerts.tsx`
- Create: `electron/src/renderer/lib/ws.ts`

- [ ] **Step 1: lib/ws.ts — WebSocket 재연결 client**

```typescript
// electron/src/renderer/lib/ws.ts
const baseUrl = window.kream?.apiBaseUrl ?? "http://localhost:8000";
const token = window.kream?.apiToken ?? "dev-token-replace-in-prod";
const wsBase = baseUrl.replace(/^http/, "ws");

export type WSMessage<T = unknown> = T;

export function subscribe<T>(
  path: string,
  onMessage: (msg: T) => void,
  onConnectionChange?: (connected: boolean) => void,
): () => void {
  let ws: WebSocket | null = null;
  let backoff = 1000;
  let stopped = false;

  const connect = () => {
    if (stopped) return;
    const url = `${wsBase}${path}?token=${encodeURIComponent(token)}`;
    ws = new WebSocket(url);
    ws.onopen = () => {
      backoff = 1000;
      onConnectionChange?.(true);
      // keep-alive ping
      const ping = setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) ws.send("ping");
        else clearInterval(ping);
      }, 30000);
    };
    ws.onmessage = (e) => {
      try {
        onMessage(JSON.parse(e.data) as T);
      } catch {
        /* ignore */
      }
    };
    ws.onclose = () => {
      onConnectionChange?.(false);
      if (stopped) return;
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000);
    };
    ws.onerror = () => ws?.close();
  };

  connect();
  return () => {
    stopped = true;
    ws?.close();
  };
}
```

- [ ] **Step 2: StatusCard.tsx**

```tsx
// electron/src/renderer/components/StatusCard.tsx
interface Props {
  label: string;
  value: string | number;
  sub?: string;
  color?: "accent" | "ok" | "warn" | "err" | "text";
  progress?: number; // 0~1
}

const colorClass: Record<string, string> = {
  accent: "text-accent",
  ok: "text-ok",
  warn: "text-warn",
  err: "text-err",
  text: "text-text",
};

export function StatusCard({ label, value, sub, color = "text", progress }: Props) {
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="text-[10px] uppercase tracking-wider text-text-muted">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${colorClass[color]}`}>{value}</div>
      {sub && <div className="text-xs text-text-muted mt-1">{sub}</div>}
      {progress !== undefined && (
        <div className="mt-2 h-1 bg-border/60 rounded overflow-hidden">
          <div
            className={`h-full ${color === "accent" ? "bg-accent" : "bg-text-muted"}`}
            style={{ width: `${Math.min(progress * 100, 100)}%` }}
          />
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: AdapterGrid.tsx (Dashboard 용 — Adapters 페이지와 별도, 간략 표시)**

```tsx
// electron/src/renderer/components/AdapterGrid.tsx
interface Adapter {
  name: string;
  active: boolean;
}

export function AdapterGrid({ adapters }: { adapters: Adapter[] }) {
  const activeCount = adapters.filter((a) => a.active).length;
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[10px] uppercase tracking-wider text-text-muted">
          Sourcing Sources ({adapters.length})
        </span>
        <span className="text-[10px] text-text-muted">
          🟢 {activeCount} · 🔴 {adapters.length - activeCount}
        </span>
      </div>
      <div className="grid grid-cols-6 lg:grid-cols-8 gap-1.5">
        {adapters.map((a) => (
          <div
            key={a.name}
            className={`px-2 py-1.5 rounded text-[9px] text-center font-mono border ${
              a.active
                ? "bg-ok/10 text-ok border-ok/30"
                : "bg-err/10 text-err border-err/30"
            }`}
          >
            {a.name}
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: RecentAlerts.tsx**

```tsx
// electron/src/renderer/components/RecentAlerts.tsx
import { ArrowUpRight } from "lucide-react";

interface Alert {
  signal: string;
  source: string;
  model_no: string;
  net_profit: number;
  fired_at: number | string;
}

const signalColor: Record<string, string> = {
  STRONG_BUY: "text-ok",
  BUY: "text-accent",
  WATCH: "text-warn",
};

function relativeTime(ts: number | string) {
  const t = typeof ts === "string" ? new Date(ts).getTime() / 1000 : ts;
  const diff = Math.floor(Date.now() / 1000 - t);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function RecentAlerts({ alerts }: { alerts: Alert[] }) {
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="text-[10px] uppercase tracking-wider text-text-muted mb-3">
        Recent Alerts ({alerts.length})
      </div>
      {alerts.length === 0 ? (
        <div className="text-text-muted text-xs">알림 없음</div>
      ) : (
        <ul className="space-y-1">
          {alerts.map((a, i) => (
            <li key={i} className="flex items-center gap-2 text-xs py-1 border-b border-border/30 last:border-0">
              <span className={`font-semibold ${signalColor[a.signal] ?? "text-text"}`}>
                {a.signal}
              </span>
              <span className="text-text-muted font-mono">{a.source}</span>
              <span className="font-mono">{a.model_no}</span>
              <span className="ml-auto text-text-muted">{relativeTime(a.fired_at)}</span>
              <span className="text-ok font-semibold">₩{a.net_profit.toLocaleString()}</span>
              <ArrowUpRight size={10} className="text-text-muted" />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Dashboard.tsx — 통합 페이지**

```tsx
// electron/src/renderer/pages/Dashboard.tsx
import { useEffect, useState } from "react";

import { AdapterGrid } from "../components/AdapterGrid";
import { RecentAlerts } from "../components/RecentAlerts";
import { StatusCard } from "../components/StatusCard";
import { api } from "../lib/api";
import { subscribe } from "../lib/ws";

interface Snapshot {
  kream_calls_24h: number;
  kream_calls_cap: number;
  alerts_24h: number;
  matches_6h: number;
  queue_pending: number;
  adapters: { name: string; active: boolean }[];
  recent_alerts: any[];
}

interface StatusUpdate {
  kream_calls_24h: number;
  alerts_24h: number;
  matches_6h: number;
  paused: boolean;
  mode: string;
  uptime_sec: number;
}

export default function Dashboard() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [wsConnected, setWsConnected] = useState(false);

  useEffect(() => {
    api.get("/api/dashboard/snapshot").then((r) => setSnap(r.data));
    const unsubStatus = subscribe<StatusUpdate>(
      "/api/dashboard/ws/status",
      (msg) => {
        setSnap((prev) =>
          prev ? { ...prev, kream_calls_24h: msg.kream_calls_24h, alerts_24h: msg.alerts_24h, matches_6h: msg.matches_6h } : prev,
        );
      },
      setWsConnected,
    );
    const unsubAlerts = subscribe<any>(
      "/api/dashboard/ws/alerts",
      (newAlert) => {
        setSnap((prev) =>
          prev ? { ...prev, recent_alerts: [newAlert, ...prev.recent_alerts].slice(0, 10) } : prev,
        );
      },
    );
    return () => {
      unsubStatus();
      unsubAlerts();
    };
  }, []);

  if (!snap) return <div className="p-6 text-text-muted">로딩 중...</div>;

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <span
          className={`text-[10px] flex items-center gap-1.5 ${
            wsConnected ? "text-ok" : "text-warn"
          }`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? "bg-ok" : "bg-warn"}`} />
          {wsConnected ? "LIVE" : "재연결 중..."}
        </span>
      </div>

      <div className="grid grid-cols-4 gap-3">
        <StatusCard
          label="크림 호출 24h"
          value={snap.kream_calls_24h.toLocaleString()}
          sub={`/ ${snap.kream_calls_cap.toLocaleString()} (${((snap.kream_calls_24h / snap.kream_calls_cap) * 100).toFixed(1)}%)`}
          color="accent"
          progress={snap.kream_calls_24h / snap.kream_calls_cap}
        />
        <StatusCard label="알림 24h" value={snap.alerts_24h} color="ok" />
        <StatusCard label="매칭 6h" value={snap.matches_6h} color="warn" sub={`${snap.adapters.length} sources`} />
        <StatusCard label="큐 적체" value={snap.queue_pending.toLocaleString()} color="text" />
      </div>

      <AdapterGrid adapters={snap.adapters} />

      <RecentAlerts alerts={snap.recent_alerts} />
    </div>
  );
}
```

- [ ] **Step 6: e2e 검증**

봇 + Electron 가동 후:
- Dashboard 페이지 = 4개 status card + adapter grid + recent alerts 표시
- 우측 상단 LIVE 인디케이터 (녹색)
- 5초마다 status card 값 갱신 확인 (ts diff)
- 새 알림 발생 시 (수동 트리거 어려우면 sql insert 시뮬) recent_alerts 상단에 push 확인

- [ ] **Step 7: 커밋**

```bash
git add electron/src/renderer/pages/Dashboard.tsx electron/src/renderer/components/StatusCard.tsx electron/src/renderer/components/AdapterGrid.tsx electron/src/renderer/components/RecentAlerts.tsx electron/src/renderer/lib/ws.ts
git commit -m "feat(electron): Dashboard 페이지 — status cards + adapter grid + recent alerts + WebSocket"
```

---

### Task 3.5: Recharts 시계열 차트 (호출량 + 알림 24h)

**Files:**
- Modify: `src/api/routers/dashboard.py` (시계열 endpoint 추가)
- Create: `electron/src/renderer/components/ChartCalls24h.tsx`
- Modify: `electron/src/renderer/pages/Dashboard.tsx` (차트 추가)

- [ ] **Step 1: 시계열 endpoint 추가 (dashboard.py)**

```python
@router.get("/timeseries", dependencies=[Depends(verify_token)])
async def timeseries() -> dict:
    """24h 시계열 — 1시간 bucket 기준 크림 호출 + 알림 수."""
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("""
            SELECT strftime('%Y-%m-%d %H:00', ts) AS hour, COUNT(*) AS cnt
            FROM kream_api_calls
            WHERE ts > datetime('now', '-24 hours')
            GROUP BY hour ORDER BY hour
        """)
        kream = [{"hour": r[0], "count": r[1]} for r in await cur.fetchall()]
        cur = await db.execute("""
            SELECT strftime('%Y-%m-%d %H:00', datetime(fired_at, 'unixepoch')) AS hour,
                   COUNT(*) AS cnt
            FROM alert_sent
            WHERE fired_at > strftime('%s','now') - 86400
            GROUP BY hour ORDER BY hour
        """)
        alerts = [{"hour": r[0], "count": r[1]} for r in await cur.fetchall()]
    return {"kream_calls": kream, "alerts": alerts}
```

- [ ] **Step 2: ChartCalls24h.tsx**

```tsx
// electron/src/renderer/components/ChartCalls24h.tsx
import { useEffect, useState } from "react";
import {
  Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { api } from "../lib/api";

interface Point {
  hour: string;
  kream_calls?: number;
  alerts?: number;
}

export function ChartCalls24h() {
  const [data, setData] = useState<Point[]>([]);

  useEffect(() => {
    const load = async () => {
      const r = await api.get("/api/dashboard/timeseries");
      const merged: Record<string, Point> = {};
      r.data.kream_calls.forEach((p: any) => {
        merged[p.hour] = { hour: p.hour, kream_calls: p.count };
      });
      r.data.alerts.forEach((p: any) => {
        merged[p.hour] = { ...(merged[p.hour] ?? { hour: p.hour }), alerts: p.count };
      });
      setData(Object.values(merged));
    };
    load();
    const t = setInterval(load, 60000); // 1분 주기 갱신
    return () => clearInterval(t);
  }, []);

  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="text-[10px] uppercase tracking-wider text-text-muted mb-3">
        24h 시계열 — 크림 호출 / 알림
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <AreaChart data={data}>
          <defs>
            <linearGradient id="kreamG" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#58a6ff" stopOpacity={0.5} />
              <stop offset="100%" stopColor="#58a6ff" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="alertG" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#3fb950" stopOpacity={0.5} />
              <stop offset="100%" stopColor="#3fb950" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="hour" stroke="#8b949e" fontSize={9} tickFormatter={(v) => v?.slice(11, 16) ?? ""} />
          <YAxis stroke="#8b949e" fontSize={9} />
          <Tooltip contentStyle={{ background: "#161b22", border: "1px solid #30363d", fontSize: 11 }} />
          <Area dataKey="kream_calls" stroke="#58a6ff" fill="url(#kreamG)" />
          <Area dataKey="alerts" stroke="#3fb950" fill="url(#alertG)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
```

- [ ] **Step 3: Dashboard.tsx 에 차트 추가**

`Dashboard.tsx` 에서 `<RecentAlerts ... />` 위에 다음 추가:

```tsx
import { ChartCalls24h } from "../components/ChartCalls24h";
```

```tsx
      <ChartCalls24h />
```

- [ ] **Step 4: e2e 검증 — 차트 렌더 확인**

봇 가동 + Electron 가동 → Dashboard 에서 24h 시계열 차트 표시 (데이터 있어야 시각화)

- [ ] **Step 5: 커밋**

```bash
git add src/api/routers/dashboard.py electron/src/renderer/components/ChartCalls24h.tsx electron/src/renderer/pages/Dashboard.tsx
git commit -m "feat(dashboard): Recharts 시계열 차트 — 24h 크림 호출 + 알림"
```

---

### Task 3.6: Phase A3 e2e + checkpoint

- [ ] **Step 1: 30분 동안 Dashboard 라이브 관찰**

봇 + Electron 가동 → Dashboard 30분간 띄워둠 → 다음 확인:
- LIVE 인디케이터 녹색 유지 (재연결 X)
- status card 값 5초마다 갱신
- 알림 발생 시 (운 좋게) recent_alerts 즉시 push

- [ ] **Step 2: Phase A3 완료 마커**

```bash
git commit --allow-empty -m "checkpoint: Phase A3 (dashboard) complete"
```

## Phase A4 — 운영 패널 (수정 관리자 모드 + 분리 로그 윈도우)

목표: 터미널에서 하던 운영 작업 (재시작/디버그/업데이트/로그) 전부 UI로. Detached log window.

### Task 4.1: log_emitter.py — logging Handler → SQLite + WebSocket

**Files:**
- Modify: `src/models/database.py` (bot_logs 테이블 추가 — 또는 기존 로그 테이블 활용)
- Create: `src/api/log_emitter.py`
- Modify: `main.py` 또는 `src/utils/logging.py` (root logger 에 emitter 추가)
- Create: `tests/api/test_log_emitter.py`

- [ ] **Step 1: bot_logs 테이블 추가 (database.py SCHEMA_SQL)**

```sql
-- UI 봇 로그 영속화 (Phase A4) — ring buffer (max 7d 또는 100k row)
CREATE TABLE IF NOT EXISTS bot_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    level TEXT NOT NULL,           -- DEBUG/INFO/WARN/ERROR
    logger TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_logs_ts ON bot_logs(ts);
CREATE INDEX IF NOT EXISTS idx_bot_logs_level ON bot_logs(level);
```

- [ ] **Step 2: 실패 테스트**

```python
# tests/api/test_log_emitter.py
import logging
import pytest

import aiosqlite

from src.api.log_emitter import SQLiteLogHandler
from src.config import settings
from src.models.database import init_db


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("DELETE FROM bot_logs")
        await db.commit()
    yield


@pytest.mark.asyncio
async def test_handler_writes_to_sqlite():
    h = SQLiteLogHandler(db_path=settings.db_path)
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py", lineno=1,
        msg="hello world", args=(), exc_info=None,
    )
    await h.emit_async(record)

    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute("SELECT level, logger, message FROM bot_logs")
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "INFO"
    assert rows[0][1] == "test"
    assert rows[0][2] == "hello world"


@pytest.mark.asyncio
async def test_handler_emits_to_ws_manager():
    from src.api.ws_manager import ws_manager
    from unittest.mock import AsyncMock

    fake_ws = AsyncMock()
    fake_ws.send_json = AsyncMock()
    await ws_manager.connect("logs", fake_ws)

    h = SQLiteLogHandler(db_path=settings.db_path)
    record = logging.LogRecord(
        name="test", level=logging.WARNING, pathname="t.py", lineno=1,
        msg="warn msg", args=(), exc_info=None,
    )
    await h.emit_async(record)

    fake_ws.send_json.assert_called_once()
    payload = fake_ws.send_json.call_args[0][0]
    assert payload["level"] == "WARNING"
    assert payload["message"] == "warn msg"
    ws_manager.disconnect("logs", fake_ws)
```

- [ ] **Step 3: 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_log_emitter.py -v
```
Expected: FAIL

- [ ] **Step 4: log_emitter.py 구현**

```python
# src/api/log_emitter.py
"""logging.Handler — SQLite 영속화 + WebSocket /ws/logs broadcast.

비동기 emit 을 위해 asyncio.Queue + 백그라운드 worker 사용 (logging.Handler 는 sync).
"""

from __future__ import annotations

import asyncio
import logging

import aiosqlite

from src.api.ws_manager import ws_manager


class SQLiteLogHandler(logging.Handler):
    """logging Handler — emit() 시 비동기 task 로 SQLite + WS broadcast."""

    def __init__(self, db_path: str, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.db_path = db_path

    def emit(self, record: logging.LogRecord) -> None:
        """sync handler — async emit 을 task 로 schedule."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.emit_async(record))
        except RuntimeError:
            # 이벤트 루프 외부에서 호출 시 (테스트 등) — 무시
            pass

    async def emit_async(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        level = record.levelname
        logger_name = record.name
        # SQLite 영속화
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO bot_logs (level, logger, message) VALUES (?, ?, ?)",
                    (level, logger_name, msg),
                )
                await db.commit()
        except Exception:
            pass  # 로그 핸들러는 어떤 경우에도 봇 코어 영향 X
        # WebSocket broadcast
        try:
            await ws_manager.broadcast(
                "logs",
                {"level": level, "logger": logger_name, "message": msg, "ts": record.created},
            )
        except Exception:
            pass
```

- [ ] **Step 5: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_log_emitter.py -v
```
Expected: 2 passed

- [ ] **Step 6: main.py 에 root logger 등록**

`main.py` 의 `_main_async` 안 `api_task = ...` 다음에 추가:

```python
    # Phase A4 — UI 로그 emitter (root logger 에 attach)
    from src.api.log_emitter import SQLiteLogHandler
    log_handler = SQLiteLogHandler(db_path=settings.db_path, level=logging.INFO)
    logging.getLogger().addHandler(log_handler)
```

상단 import 에 추가:
```python
import logging
```

- [ ] **Step 7: 커밋**

```bash
git add src/api/log_emitter.py src/models/database.py main.py tests/api/test_log_emitter.py
git commit -m "feat(api): SQLiteLogHandler — logging → SQLite + WebSocket broadcast"
```

---

### Task 4.2: /api/logs/export + /ws/logs

**Files:**
- Create: `src/api/routers/logs.py`
- Modify: `src/api/server.py` (라우터 등록)
- Create: `tests/api/test_logs_router.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_logs_router.py
import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.server import app
from src.config import settings
from src.models.database import init_db

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("DELETE FROM bot_logs")
        await db.executemany(
            "INSERT INTO bot_logs (level, logger, message) VALUES (?, ?, ?)",
            [
                ("INFO", "test", "first message"),
                ("WARNING", "test", "warning message"),
                ("ERROR", "test", "error happened"),
                ("INFO", "scheduler", "tier2 tick"),
            ],
        )
        await db.commit()
    yield


@pytest.mark.asyncio
async def test_export_returns_all_logs():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/logs/export?hours=24", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body["logs"]) >= 4


@pytest.mark.asyncio
async def test_export_filter_by_grep():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/logs/export?grep=warning", headers=AUTH)
    body = r.json()
    assert all("warning" in log["message"].lower() for log in body["logs"])


@pytest.mark.asyncio
async def test_export_filter_by_level():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/logs/export?level=ERROR", headers=AUTH)
    body = r.json()
    assert all(log["level"] == "ERROR" for log in body["logs"])
```

- [ ] **Step 2: 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_logs_router.py -v
```
Expected: FAIL

- [ ] **Step 3: logs.py 구현**

```python
# src/api/routers/logs.py
"""로그 export REST + 실시간 WebSocket stream."""

import aiosqlite
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from src.api.auth import verify_token
from src.api.ws_manager import ws_manager
from src.config import settings

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/export", dependencies=[Depends(verify_token)])
async def export(
    hours: int = Query(default=1, ge=1, le=168),
    grep: str | None = Query(default=None),
    level: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=10000),
) -> dict:
    """과거 hours 시간 로그 export. grep + level 필터."""
    where = [f"ts > datetime('now','-{hours} hours')"]
    params: list = []
    if grep:
        where.append("LOWER(message) LIKE ?")
        params.append(f"%{grep.lower()}%")
    if level:
        where.append("level = ?")
        params.append(level.upper())
    sql = f"""
        SELECT ts, level, logger, message FROM bot_logs
        WHERE {' AND '.join(where)}
        ORDER BY ts DESC LIMIT ?
    """
    params.append(limit)
    async with aiosqlite.connect(settings.db_path) as db:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    return {
        "logs": [
            {"ts": r[0], "level": r[1], "logger": r[2], "message": r[3]}
            for r in rows
        ],
        "count": len(rows),
    }


@router.websocket("/ws/logs")
async def ws_logs(ws: WebSocket, token: str = Query(default="")):
    """실시간 로그 stream — Detached Log Window 가 구독."""
    if token != settings.api_token:
        await ws.close(code=1008, reason="invalid token")
        return
    await ws_manager.connect("logs", ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect("logs", ws)
```

- [ ] **Step 4: server.py 갱신**

```python
from src.api.routers import bot, dashboard, logs, ops, sources
```
```python
    app.include_router(logs.router)
```

- [ ] **Step 5: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_logs_router.py -v
```
Expected: 3 passed

- [ ] **Step 6: 커밋**

```bash
git add src/api/routers/logs.py src/api/server.py tests/api/test_logs_router.py
git commit -m "feat(api): /api/logs/export + /ws/logs — 로그 query + 실시간 stream"
```

---

### Task 4.3: /api/diagnostics/snapshot — 진단 export

**Files:**
- Modify: `src/api/routers/ops.py` (snapshot 추가)
- Create: `tests/api/test_diagnostics.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_diagnostics.py
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.server import app
from src.config import settings
from src.models.database import init_db

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    yield


@pytest.mark.asyncio
async def test_diagnostics_snapshot_schema():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/diagnostics/snapshot", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "git_head" in body
    assert "uptime_sec" in body
    assert "db_stats" in body
    assert "decision_log_1h" in body
    assert "adapters" in body
    assert "recent_errors" in body
```

- [ ] **Step 2: 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_diagnostics.py -v
```
Expected: FAIL

- [ ] **Step 3: ops.py 에 snapshot 추가**

`src/api/routers/ops.py` 에 추가:

```python
import subprocess
import time
from pathlib import Path

import aiosqlite
from fastapi import Depends

from src.api.auth import verify_token
from src.api.source_state import is_source_active
from src.config import settings
from src.crawlers.registry import REGISTRY


@router.get("/diagnostics/snapshot", dependencies=[Depends(verify_token)])
async def diagnostics_snapshot() -> dict:
    """봇 진단 스냅샷 — Claude 한테 복붙해서 디버그 도움 요청 가능."""
    # git HEAD
    try:
        git_head = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%H %s"],
            cwd=Path(__file__).parents[3],
            text=True, timeout=2,
        ).strip()
    except Exception:
        git_head = "unknown"

    # DB 통계
    db_stats = {}
    async with aiosqlite.connect(settings.db_path) as db:
        for tbl in [
            "kream_products", "alert_sent", "decision_log",
            "kream_collect_queue", "kream_api_calls", "bot_logs",
        ]:
            try:
                cur = await db.execute(f"SELECT COUNT(*) FROM {tbl}")
                db_stats[tbl] = (await cur.fetchone())[0]
            except Exception:
                db_stats[tbl] = -1

        # decision_log 최근 1h breakdown
        cur = await db.execute("""
            SELECT stage, COUNT(*) FROM decision_log
            WHERE ts > datetime('now','-1 hour')
            GROUP BY stage ORDER BY 2 DESC
        """)
        decision_log_1h = [{"stage": r[0], "count": r[1]} for r in await cur.fetchall()]

        # 최근 ERROR 로그 20건
        cur = await db.execute("""
            SELECT ts, logger, message FROM bot_logs
            WHERE level IN ('ERROR', 'CRITICAL')
            ORDER BY ts DESC LIMIT 20
        """)
        recent_errors = [
            {"ts": r[0], "logger": r[1], "message": r[2]}
            for r in await cur.fetchall()
        ]

    adapters = []
    for name in REGISTRY.keys():
        adapters.append({"name": name, "active": await is_source_active(name)})

    return {
        "git_head": git_head,
        "uptime_sec": int(time.time() - _START_TIME),
        "db_stats": db_stats,
        "decision_log_1h": decision_log_1h,
        "adapters": adapters,
        "recent_errors": recent_errors,
        "ts": int(time.time()),
    }
```

- [ ] **Step 4: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_diagnostics.py -v
```
Expected: 1 passed

- [ ] **Step 5: 커밋**

```bash
git add src/api/routers/ops.py tests/api/test_diagnostics.py
git commit -m "feat(api): /api/diagnostics/snapshot — 진단 export (git+db+decision+errors)"
```

---

### Task 4.4: /api/bot/restart + /api/bot/debug + /api/bot/update

**Files:**
- Modify: `src/api/routers/bot.py` (3 endpoints 추가)
- Create: `tests/api/test_bot_ops.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/api/test_bot_ops.py
import pytest
from httpx import ASGITransport, AsyncClient

from src.api.server import app
from src.config import settings
from src.models.database import init_db

AUTH = {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    yield


@pytest.mark.asyncio
async def test_debug_toggle():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/bot/debug", json={"enabled": True}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["debug_mode"] is True


@pytest.mark.asyncio
async def test_restart_marks_request_flag():
    """restart 는 watchdog 협조용 flag. 실제 process kill 은 watchdog 가 감지."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/bot/restart", headers=AUTH)
    assert r.status_code == 202
    assert r.json()["status"] == "restart_requested"


@pytest.mark.asyncio
async def test_update_returns_git_info():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/api/bot/update", headers=AUTH)
    # update 는 dry-run 모드 또는 실제 git pull. 200 또는 202 반환.
    assert r.status_code in (200, 202)
    body = r.json()
    assert "git_status" in body or "status" in body
```

- [ ] **Step 2: 실패 확인**

```bash
PYTHONPATH=. pytest tests/api/test_bot_ops.py -v
```
Expected: FAIL — endpoints 없음

- [ ] **Step 3: bot.py 라우터 확장**

`src/api/routers/bot.py` 끝에 추가:

```python
import os
import subprocess
from pathlib import Path

from pydantic import BaseModel


class DebugPayload(BaseModel):
    enabled: bool


@router.post("/debug", dependencies=[Depends(verify_token)])
async def debug(payload: DebugPayload) -> dict:
    """디버그 모드 토글 — bot_state 에 저장. logger level 즉시 반영은 root logger 측에서."""
    await set_state("debug_mode", "true" if payload.enabled else "false")
    # Phase A4 확장: logging.getLogger().setLevel(logging.DEBUG if enabled else logging.INFO)
    import logging
    logging.getLogger().setLevel(logging.DEBUG if payload.enabled else logging.INFO)
    return {"debug_mode": payload.enabled}


@router.post("/restart", status_code=202, dependencies=[Depends(verify_token)])
async def restart() -> dict:
    """봇 재시작 요청 — bot_state 에 flag → watchdog 가 감지하고 respawn.

    또는 SIGTERM 직접 발사 (watchdog 가 즉시 respawn). 후자 채택.
    """
    await set_state("restart_requested", "true")
    pid = os.getpid()
    # 현재 프로세스에 SIGTERM (watchdog 가 자동 respawn)
    import signal
    os.kill(pid, signal.SIGTERM)
    return {"status": "restart_requested", "pid": pid}


@router.post("/update", dependencies=[Depends(verify_token)])
async def update() -> dict:
    """git pull + 변경 commit 표시. 실제 재시작은 별도 /restart 호출 필요."""
    repo_root = Path(__file__).parents[3]
    try:
        before = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, timeout=5
        ).strip()
        result = subprocess.run(
            ["git", "pull", "--ff-only"], cwd=repo_root, capture_output=True,
            text=True, timeout=30,
        )
        after = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, timeout=5
        ).strip()
        new_commits: list[str] = []
        if before != after:
            new_commits = subprocess.check_output(
                ["git", "log", f"{before}..{after}", "--pretty=%h %s"],
                cwd=repo_root, text=True, timeout=5,
            ).strip().split("\n")
        return {
            "git_status": "updated" if before != after else "up_to_date",
            "before": before[:7],
            "after": after[:7],
            "new_commits": new_commits,
            "pull_output": (result.stdout + result.stderr).strip(),
            "restart_required": before != after,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "git_status": "error"}
    except Exception as e:
        return {"status": "error", "git_status": "error", "detail": str(e)}
```

상단 import 에 추가:
```python
from src.api.state import get_state  # 이미 있을 수 있음
```

- [ ] **Step 4: 테스트 PASS**

```bash
PYTHONPATH=. pytest tests/api/test_bot_ops.py -v
```
Expected: 3 passed

(restart 테스트는 ASGITransport 에서 SIGTERM 보내도 실 프로세스 죽지 않음 — 테스트 환경에선 안전)

- [ ] **Step 5: 커밋**

```bash
git add src/api/routers/bot.py tests/api/test_bot_ops.py
git commit -m "feat(api): /api/bot/{restart,debug,update} — Phase A4 운영 endpoint"
```

### Task 4.5: Electron Diagnostics 페이지 + Settings 페이지

**Files:**
- Modify: `electron/src/renderer/pages/Diagnostics.tsx`
- Modify: `electron/src/renderer/pages/Settings.tsx`

- [ ] **Step 1: Diagnostics.tsx 구현**

```tsx
// electron/src/renderer/pages/Diagnostics.tsx
import { Download, RefreshCw, Bug, GitPullRequest } from "lucide-react";
import { useState } from "react";
import toast from "react-hot-toast";

import { api } from "../lib/api";

export default function Diagnostics() {
  const [snapshot, setSnapshot] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [debugMode, setDebugMode] = useState(false);

  const fetchSnapshot = async () => {
    setLoading(true);
    try {
      const r = await api.get("/api/diagnostics/snapshot");
      setSnapshot(r.data);
      toast.success("스냅샷 수집 완료");
    } catch (e) {
      toast.error(`실패: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  };

  const downloadSnapshot = () => {
    if (!snapshot) return;
    const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    a.download = `kream-snapshot-${ts}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const toggleDebug = async () => {
    try {
      const next = !debugMode;
      await api.post("/api/bot/debug", { enabled: next });
      setDebugMode(next);
      toast.success(`디버그 모드 ${next ? "ON" : "OFF"}`);
    } catch (e) {
      toast.error("실패");
    }
  };

  const restart = async () => {
    if (!confirm("봇을 재시작하시겠습니까? watchdog 가 자동 respawn 합니다.")) return;
    try {
      await api.post("/api/bot/restart");
      toast.success("재시작 요청 — 5~10초 후 복귀");
    } catch (e) {
      toast.error("실패");
    }
  };

  const update = async () => {
    if (!confirm("git pull 을 수행하시겠습니까?")) return;
    try {
      const r = await api.post("/api/bot/update");
      const data = r.data;
      toast.success(
        data.git_status === "updated"
          ? `${data.new_commits.length}개 커밋 적용 — 재시작 필요`
          : "이미 최신",
        { duration: 5000 },
      );
    } catch (e) {
      toast.error("실패");
    }
  };

  return (
    <div className="p-6 space-y-4">
      <h1 className="text-2xl font-semibold">Diagnostics</h1>

      <div className="grid grid-cols-2 gap-3">
        <button
          onClick={fetchSnapshot}
          disabled={loading}
          className="bg-card border border-border rounded p-3 hover:border-accent flex items-center gap-2 text-sm disabled:opacity-50"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          진단 스냅샷 수집
        </button>
        <button
          onClick={downloadSnapshot}
          disabled={!snapshot}
          className="bg-card border border-border rounded p-3 hover:border-accent flex items-center gap-2 text-sm disabled:opacity-50"
        >
          <Download size={14} />
          JSON 다운로드 (Claude 한테 복붙용)
        </button>
        <button
          onClick={toggleDebug}
          className={`border rounded p-3 flex items-center gap-2 text-sm ${
            debugMode ? "bg-warn/20 border-warn text-warn" : "bg-card border-border hover:border-accent"
          }`}
        >
          <Bug size={14} />
          디버그 로그 {debugMode ? "ON" : "OFF"}
        </button>
        <button
          onClick={update}
          className="bg-card border border-border rounded p-3 hover:border-accent flex items-center gap-2 text-sm"
        >
          <GitPullRequest size={14} />
          git pull (업데이트 확인)
        </button>
        <button
          onClick={restart}
          className="bg-err/20 border border-err rounded p-3 hover:bg-err/30 text-err flex items-center gap-2 text-sm col-span-2 justify-center"
        >
          <RefreshCw size={14} />
          봇 재시작 (watchdog 자동 respawn)
        </button>
      </div>

      {snapshot && (
        <div className="bg-card border border-border rounded p-4 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs uppercase tracking-wider text-text-muted">현재 스냅샷</span>
            <span className="text-[10px] text-text-muted font-mono">{snapshot.git_head}</span>
          </div>
          <div className="grid grid-cols-3 gap-2 text-xs">
            <div className="bg-bg p-2 rounded">
              <div className="text-text-muted text-[10px]">Uptime</div>
              <div>{Math.floor(snapshot.uptime_sec / 60)}분</div>
            </div>
            <div className="bg-bg p-2 rounded">
              <div className="text-text-muted text-[10px]">DB rows (alert_sent)</div>
              <div>{snapshot.db_stats.alert_sent}</div>
            </div>
            <div className="bg-bg p-2 rounded">
              <div className="text-text-muted text-[10px]">Recent errors</div>
              <div className={snapshot.recent_errors.length > 0 ? "text-err" : "text-ok"}>
                {snapshot.recent_errors.length}
              </div>
            </div>
          </div>
          {snapshot.decision_log_1h.length > 0 && (
            <div>
              <div className="text-[10px] text-text-muted mb-1">decision_log 1h</div>
              <div className="text-xs font-mono space-y-0.5">
                {snapshot.decision_log_1h.map((d: any) => (
                  <div key={d.stage}>
                    <span className="text-accent">{d.stage}</span> · {d.count}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Settings.tsx — API URL/토큰 변경 (재시작 필요 안내)**

```tsx
// electron/src/renderer/pages/Settings.tsx
export default function Settings() {
  const apiUrl = window.kream?.apiBaseUrl ?? "http://localhost:8000";
  const apiToken = window.kream?.apiToken ?? "";

  return (
    <div className="p-6 max-w-2xl space-y-4">
      <h1 className="text-2xl font-semibold">Settings</h1>
      <div className="bg-card border border-border rounded p-4 space-y-2">
        <div className="text-xs uppercase tracking-wider text-text-muted">API 연결</div>
        <div className="grid grid-cols-3 gap-3 items-center text-xs">
          <span className="text-text-muted">API URL</span>
          <span className="col-span-2 font-mono bg-bg p-2 rounded">{apiUrl}</span>
          <span className="text-text-muted">API Token</span>
          <span className="col-span-2 font-mono bg-bg p-2 rounded">
            {apiToken.slice(0, 8)}…{apiToken.slice(-4)}
          </span>
        </div>
        <p className="text-text-muted text-xs mt-3">
          변경하려면 환경 변수 <code className="bg-bg px-1.5 py-0.5 rounded">KREAM_API_URL</code> /
          <code className="bg-bg px-1.5 py-0.5 rounded ml-1">KREAM_API_TOKEN</code> 설정 후 Electron 재시작.
          미래 임대 서버 이전 시 URL 만 변경하면 됨.
        </p>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: e2e 검증**

봇 + Electron 가동:
- 사이드바 "Diagnostics" 클릭 → 5개 버튼 표시
- "진단 스냅샷 수집" 클릭 → 결과 카드 표시 (uptime/db/errors)
- "JSON 다운로드" 클릭 → `kream-snapshot-YYYY-MM-DD-HH-MM-SS.json` 다운로드
- "디버그 로그 ON" 클릭 → 색상 변경 + 토스트
- "git pull" 클릭 → 확인 다이얼로그 → 결과 토스트

- [ ] **Step 4: 커밋**

```bash
git add electron/src/renderer/pages/Diagnostics.tsx electron/src/renderer/pages/Settings.tsx
git commit -m "feat(electron): Diagnostics 페이지 (5 운영 버튼) + Settings 페이지"
```

---

### Task 4.6: Detached Log Window — log-window/main.tsx + LogWindow.tsx

**Files:**
- Create: `electron/src/log-window/main.tsx`
- Create: `electron/src/log-window/LogWindow.tsx`
- Modify: `electron/src/renderer/pages/Logs.tsx` (메인 윈도우 안 로그 패널 — Detached 와 동일 컴포넌트 재사용)

- [ ] **Step 1: log-window/main.tsx**

```tsx
// electron/src/log-window/main.tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { Toaster } from "react-hot-toast";

import LogWindow from "./LogWindow";
import "../renderer/index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LogWindow />
    <Toaster position="bottom-right" />
  </React.StrictMode>,
);
```

- [ ] **Step 2: LogWindow.tsx — 라이브 stream + grep + level 필터**

```tsx
// electron/src/log-window/LogWindow.tsx
import { useEffect, useRef, useState } from "react";

import { api } from "../renderer/lib/api";
import { subscribe } from "../renderer/lib/ws";

interface LogEntry {
  ts: string | number;
  level: string;
  logger: string;
  message: string;
}

const levelColor: Record<string, string> = {
  DEBUG: "text-text-muted",
  INFO: "text-accent",
  WARNING: "text-warn",
  WARN: "text-warn",
  ERROR: "text-err",
  CRITICAL: "text-err",
  OK: "text-ok",
};

function formatTs(ts: string | number) {
  let d: Date;
  if (typeof ts === "number") {
    d = new Date(ts * 1000);
  } else {
    d = new Date(ts);
  }
  return d.toTimeString().slice(0, 8);
}

export default function LogWindow() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [grep, setGrep] = useState("");
  const [levelFilter, setLevelFilter] = useState<string>("ALL");
  const [autoscroll, setAutoscroll] = useState(true);
  const [connected, setConnected] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // 초기 로드 — 최근 200줄
  useEffect(() => {
    api
      .get("/api/logs/export?hours=1&limit=200")
      .then((r) => setLogs(r.data.logs.reverse()))
      .catch(() => {});
  }, []);

  // WebSocket subscribe
  useEffect(() => {
    return subscribe<LogEntry>(
      "/api/logs/ws/logs",
      (msg) => {
        setLogs((prev) => [...prev, msg].slice(-2000));
      },
      setConnected,
    );
  }, []);

  // Auto-scroll
  useEffect(() => {
    if (autoscroll) bottomRef.current?.scrollIntoView({ behavior: "auto" });
  }, [logs, autoscroll]);

  const filtered = logs.filter((l) => {
    if (levelFilter !== "ALL" && l.level !== levelFilter) return false;
    if (grep && !l.message.toLowerCase().includes(grep.toLowerCase())) return false;
    return true;
  });

  const levels = ["ALL", "INFO", "WARNING", "ERROR", "DEBUG"];

  return (
    <div className="h-screen flex flex-col bg-black text-text font-mono">
      <header className="bg-card border-b border-border px-3 py-2 flex items-center gap-2 text-xs">
        <span className="font-semibold">⚪⚪⚪ Live Logs</span>
        <span className={`flex items-center gap-1 ${connected ? "text-ok" : "text-warn"}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${connected ? "bg-ok animate-pulse" : "bg-warn"}`} />
          {connected ? "LIVE" : "재연결 중..."}
        </span>
        <span className="ml-auto text-text-muted">{filtered.length} 줄</span>
        <button
          onClick={() => window.kream.closeLogWindow()}
          className="text-accent hover:text-text"
          title="메인으로 복귀"
        >
          ⇲ Attach
        </button>
      </header>
      <div className="bg-card/50 border-b border-border px-3 py-2 flex items-center gap-2">
        <input
          value={grep}
          onChange={(e) => setGrep(e.target.value)}
          placeholder="🔍 grep filter..."
          className="flex-1 bg-bg border border-border rounded px-2 py-1 text-xs text-text font-sans"
        />
        {levels.map((lv) => (
          <button
            key={lv}
            onClick={() => setLevelFilter(lv)}
            className={`px-2 py-1 text-[10px] rounded font-sans ${
              levelFilter === lv
                ? "bg-accent text-white"
                : "bg-card border border-border text-text-muted hover:text-text"
            }`}
          >
            {lv}
          </button>
        ))}
        <label className="text-[10px] flex items-center gap-1 cursor-pointer">
          <input
            type="checkbox"
            checked={autoscroll}
            onChange={(e) => setAutoscroll(e.target.checked)}
          />
          Auto-scroll
        </label>
      </div>
      <div className="flex-1 overflow-auto p-2 text-[11px] leading-relaxed">
        {filtered.map((l, i) => (
          <div key={i} className="flex gap-2 hover:bg-card/30 px-1 py-0.5">
            <span className="text-text-muted shrink-0">{formatTs(l.ts)}</span>
            <span className={`shrink-0 w-12 ${levelColor[l.level] ?? "text-text"}`}>
              [{l.level.slice(0, 4)}]
            </span>
            <span className="text-text-muted shrink-0">{l.logger}</span>
            <span className="break-all">{l.message}</span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Logs.tsx — 메인 윈도우 안 로그 페이지 (LogWindow 동일 컴포넌트 재사용)**

```tsx
// electron/src/renderer/pages/Logs.tsx
import LogWindow from "../../log-window/LogWindow";

export default function Logs() {
  return <LogWindow />;
}
```

- [ ] **Step 4: e2e 검증**

봇 + Electron 가동:
- 사이드바 "Logs" 클릭 → 메인 윈도우 안 로그 페이지 표시
- 사이드바 "Logs" 옆 ⇱ Detach 클릭 → 새 윈도우 열림
- 새 윈도우: LIVE 인디케이터 + 로그 5초 내 push 확인
- grep 입력: "throttle" → 필터링 동작 확인
- Level 필터: "ERROR" 클릭 → ERROR 만 표시
- ⇲ Attach 버튼 클릭 → 윈도우 닫힘

- [ ] **Step 5: 커밋**

```bash
git add electron/src/log-window/ electron/src/renderer/pages/Logs.tsx
git commit -m "feat(electron): Detached Log Window — 라이브 stream + grep + level 필터"
```

---

### Task 4.7: Phase A4 e2e + Phase A 전체 final checkpoint

- [ ] **Step 1: 24h 안정화 검증 시작**

봇 + Electron 가동 후 24시간 띄워둠. 다음 확인:
- UI crash 0회
- LIVE 인디케이터 24h 유지 (재연결 정상 작동)
- 로그 윈도우 분리 후 6h 이상 가동 무사고
- 봇 재시작 5회 성공 (watchdog respawn 정상)
- git pull 1회 성공 (변경사항 있을 때)

- [ ] **Step 2: 봇 회귀 테스트 전체**

```bash
PYTHONPATH=. pytest tests/ -v --ignore=tests/integration
```
Expected: 모든 테스트 PASS (회귀 0)

- [ ] **Step 3: 트랙 1 (소싱처 안정화) 영향 0 확인**

`/health` 슬래시 명령 또는 직접 SQL:
```bash
PYTHONPATH=. python -c "
import sqlite3
con = sqlite3.connect('data/kream_bot.db')
print(con.execute(\"SELECT COUNT(*) FROM decision_log WHERE ts > datetime('now','-2 hours') AND stage='profit_emitted'\").fetchone())
print(con.execute(\"SELECT COUNT(*) FROM alert_sent WHERE fired_at > strftime('%s','now') - 7200\").fetchone())
"
```
Expected: 매칭 + 알림 모두 정상 누적 (UI 작업 후에도 봇 코어 영향 0)

- [ ] **Step 4: Phase A 풀 완주 final 마커 commit**

```bash
git commit --allow-empty -m "checkpoint: Phase A 풀 완주 (Setup + A1 + A2 + A3 + A4) — UI 봇 전환 완료"
```

- [ ] **Step 5: 사용자 보고**

Discord 웹훅 또는 사용자 직접 알림:
> "Phase A 풀 완주 ✅ — UI 봇 전환 완료. 24h 검증 결과 첨부. 다음 단계 = Phase B 보관판매 자동등록 디테일 사용자 설명 받기."

---

## Self-Review (이 plan 작성 후 점검 결과)

**1. Spec coverage 검증**:
- ✅ §1 비전 → 모든 Phase 진입
- ✅ §2 결정 사항 → 옵션 D / Electron / 모바일 X / 풀 Phase A 모두 task 에 반영
- ✅ §3 아키텍처 → Setup tasks 0.5~0.6 (FastAPI + main.py 통합)
- ✅ §4 Phase A1~A4 → 각 Phase 별 섹션 존재
- ✅ §5 Phase B/C 메뉴 자리 → Sidebar.tsx 의 disabled 메뉴 + Placeholder 페이지
- ✅ §6 UI 디자인 → tailwind.config.js 컬러 + 컴포넌트들
- ✅ §7 데이터 흐름 → status broadcaster + WebSocket
- ✅ §8 에러 처리 → toast (renderer) + try/except (server) + WebSocket 재연결 (ws.ts)
- ✅ §9 테스트 전략 → 모든 task 에 TDD failing test 패턴
- ✅ §10 임대 서버 이전 → Settings.tsx 의 API URL 표시 + spec 명시
- ✅ §13 작업 순서 → Setup → A1 → A2 → A3 → A4 순차

**2. Placeholder 스캔**:
- "TBD" / "TODO" 검색 → 0건
- "implement later" → 0건
- "similar to" → 0건
- 모든 코드 블록 = 실제 동작 코드

**3. Type consistency**:
- `bot_state` 키: paused / mode / enabled_sources / debug_mode / restart_requested — 일관
- API endpoints: `/api/bot/{pause,resume,status,restart,debug,update}` `/api/sources/{list,enable,disable,mode}` `/api/dashboard/{snapshot,timeseries,ws/status,ws/alerts}` `/api/logs/{export,ws/logs}` `/api/diagnostics/snapshot` `/api/health` — 일관
- React store: useBotStore — 일관

**4. 알려진 변동 가능성** (구현 시 조정 필요):
- `src/crawlers/registry.py` 의 `REGISTRY` export 명 — 실제 attribute 명 확인 필요 (Task 2.1 Step 5 명시)
- `src/scheduler.py` 의 6개 루프 함수 명 — Task 1.2 Step 1 에서 확인
- `src/core/runtime.py` 의 adapter dispatch 진입점 — Task 1.3 Step 1 에서 확인
- 알림 발사 hook 위치 — Task 3.3 Step 1 에서 확인
- `settings.kream_daily_cap` 존재 여부 — Task 3.1 Step 3 명시 (없으면 추가)

이 5개 = 봇 코드베이스 의존이라 구현자가 grep 으로 정확히 식별 후 적용. plan 안에 검색 명령 + 조정 패턴 명시.

---

## Execution Handoff

Plan 작성 완료, `docs/superpowers/plans/2026-04-26-ui-bot-phase-a.md` 저장.

### 두 가지 실행 방식

**1. Subagent-Driven (권장)** — Phase 별 fresh subagent 디스패치, task 완료마다 메인 세션 검토. 빠른 iteration. 토큰 풀파워 정책 (`feedback_token_policy_by_phase.md`) 과 정합.

**2. Inline Execution** — 현재 세션에서 task 직접 실행, batch checkpoint 로 사용자 검토. 컨텍스트 유지 좋지만 한 세션 토큰 사용량 큼.

### 추가 권장 (선택)

- **git worktree 격리** (`superpowers:using-git-worktrees`) — Phase A 작업을 master 와 격리된 worktree 에서 진행. 트랙 1 (소싱처 안정화) fix 가 master 에 들어와도 충돌 0. **권장**: worktree 사용.

어떻게 진행할까요?
1. Subagent-Driven + worktree 격리 (권장)
2. Subagent-Driven + master 직접
3. Inline Execution + worktree 격리
4. Inline Execution + master 직접
