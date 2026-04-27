# Chrome 확장 무신사 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 무신사 PDP/결제 페이지에서 카드 즉시할인·시한 쿠폰·자동 매칭 쿠폰·실측 결제가를 100% catch → FastAPI POST → SQLite `coupon_catches` 저장 → `profit_calculator`가 매칭 검증(색·사이즈 일치) 통과 시 `final_price` 그대로 정확 계산에 사용.

**Architecture:** Chrome 확장(Manifest V3) Content Script(도메인별 plug-in) + Background Service Worker + Popup → FastAPI(localhost:8000) POST `/api/coupon/catch` → SQLite `coupon_catches` (sourcing+native_id+color+size 키) → `profit_calculator` hook이 매칭 검증 후 catch row 적용 (실측값 그대로, 추정 X).

**Tech Stack:** Chrome MV3, JavaScript ES2022, FastAPI(Python 3.12 async), aiosqlite, Pydantic v2, pytest, vitest, jest-dom.

**작업 위치:** Phase A worktree `.worktrees/ui-bot-phase-a` (branch `feature/ui-bot-phase-a`). 새 branch 분기 X — 동일 branch에 추가 커밋. 사용자 PC e2e 검증 후 Phase A + Phase 1 한 번에 master 머지.

**관련 spec:** `docs/superpowers/specs/2026-04-27-chrome-extension-musinsa-design.md`

---

## Task 0: Worktree 진입 + 환경 확인

**Files:**
- 작업 디렉터리: `.worktrees/ui-bot-phase-a`

- [ ] **Step 0-1: Worktree 위치 확인 + branch 확인**

```bash
cd /mnt/c/Users/USER/Desktop/크림봇/.worktrees/ui-bot-phase-a
git branch --show-current
ls src/api/routers/
```

Expected: branch=`feature/ui-bot-phase-a`, routers=`bot,dashboard,logs,ops,sources` (coupon 없음 = 정상).

- [ ] **Step 0-2: pytest + Python 환경 확인**

```bash
~/kream-venv/bin/python --version
~/kream-venv/bin/pytest --version
```

Expected: Python 3.12+, pytest 8.x.

- [ ] **Step 0-3: Electron node_modules 확인 (확장 vitest용)**

```bash
ls electron/node_modules/.bin/vitest 2>/dev/null && echo "vitest 가능" || echo "vitest 없음 — Task 6 직전 npm i 필요"
```

---

## Task 1: SQLite `coupon_catches` 테이블 마이그레이션

**Files:**
- Create: `src/migrations/0001_coupon_catches.sql`
- Modify: `src/models/database.py` (마이그레이션 호출 추가)
- Test: `tests/test_coupon_catches_migration.py`

- [ ] **Step 1-1: 마이그레이션 SQL 파일 작성**

Create `src/migrations/0001_coupon_catches.sql`:

```sql
CREATE TABLE IF NOT EXISTS coupon_catches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sourcing TEXT NOT NULL,
    native_id TEXT NOT NULL,
    color_code TEXT NOT NULL,
    size_code TEXT NOT NULL,
    page_type TEXT NOT NULL CHECK (page_type IN ('pdp', 'checkout')),
    payload TEXT NOT NULL,
    captured_at REAL NOT NULL,
    UNIQUE(sourcing, native_id, color_code, size_code, page_type)
);

CREATE INDEX IF NOT EXISTS idx_coupon_lookup
    ON coupon_catches(sourcing, native_id, color_code, size_code);

CREATE INDEX IF NOT EXISTS idx_coupon_captured
    ON coupon_catches(captured_at);
```

- [ ] **Step 1-2: 마이그레이션 호출 추가 (database.py)**

`src/models/database.py` 의 기존 마이그레이션 함수 (`run_migrations` 또는 `init_db`) 위치를 grep으로 찾고, 그 함수에 SQL 파일 read + execute 추가.

```bash
grep -n "CREATE TABLE\|run_migrations\|init_db\|migrate" src/models/database.py | head -20
```

추가 코드 (해당 함수 안에서 다른 CREATE TABLE 호출 직후에 삽입):

```python
async with aiosqlite.connect(DB_PATH) as conn:
    sql_path = Path(__file__).parent.parent / "migrations" / "0001_coupon_catches.sql"
    await conn.executescript(sql_path.read_text(encoding="utf-8"))
    await conn.commit()
logger.info("[migrate] coupon_catches 테이블 OK")
```

- [ ] **Step 1-3: 마이그레이션 테스트 작성**

Create `tests/test_coupon_catches_migration.py`:

```python
import aiosqlite
import pytest
from pathlib import Path
from src.models.database import init_db, DB_PATH

@pytest.mark.asyncio
async def test_coupon_catches_table_exists(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    monkeypatch.setattr("src.models.database.DB_PATH", str(test_db))
    await init_db()
    async with aiosqlite.connect(str(test_db)) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='coupon_catches'"
        )
        row = await cur.fetchone()
    assert row is not None, "coupon_catches 테이블이 생성되지 않음"

@pytest.mark.asyncio
async def test_coupon_catches_unique_constraint(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    monkeypatch.setattr("src.models.database.DB_PATH", str(test_db))
    await init_db()
    async with aiosqlite.connect(str(test_db)) as conn:
        await conn.execute(
            "INSERT INTO coupon_catches (sourcing, native_id, color_code, size_code, page_type, payload, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("musinsa", "5874434", "BLACK", "270", "checkout", "{}", 1777252745.0),
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO coupon_catches (sourcing, native_id, color_code, size_code, page_type, payload, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("musinsa", "5874434", "BLACK", "270", "checkout", "{}", 1777252800.0),
            )
        await conn.commit()
```

- [ ] **Step 1-4: 테스트 실패 확인**

```bash
~/kream-venv/bin/pytest tests/test_coupon_catches_migration.py -v
```

Expected: FAIL — `coupon_catches` 테이블 없음.

- [ ] **Step 1-5: 마이그레이션 호출 통합 (Step 1-2 적용 검증)**

```bash
~/kream-venv/bin/pytest tests/test_coupon_catches_migration.py -v
```

Expected: PASS 2/2.

- [ ] **Step 1-6: 커밋**

```bash
git add src/migrations/0001_coupon_catches.sql src/models/database.py tests/test_coupon_catches_migration.py
git commit -m "feat(db): coupon_catches 테이블 마이그레이션 — Chrome 확장 catch row 저장"
```

---

## Task 2: `coupon_store.py` — CRUD + 만료 + 매칭 검증

**Files:**
- Create: `src/coupon_store.py`
- Test: `tests/test_coupon_store.py`

- [ ] **Step 2-1: 테스트 작성 (TDD)**

Create `tests/test_coupon_store.py`:

```python
import json
import time
import pytest
from src.coupon_store import (
    save_catch,
    lookup_catch,
    expire_old_catches,
    CatchPayload,
)
from src.models.database import init_db

@pytest.fixture
async def db(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    monkeypatch.setattr("src.models.database.DB_PATH", str(test_db))
    monkeypatch.setattr("src.coupon_store.DB_PATH", str(test_db))
    await init_db()
    return test_db

@pytest.mark.asyncio
async def test_save_catch_inserts_new_row(db):
    payload = CatchPayload(
        sourcing="musinsa",
        native_id="5874434",
        color_code="BLACK",
        size_code="270",
        page_type="checkout",
        payload={"final_price": 174000, "card_discounts": []},
        captured_at=time.time(),
    )
    row_id = await save_catch(payload)
    assert row_id > 0

@pytest.mark.asyncio
async def test_save_catch_upserts_on_conflict(db):
    base = dict(
        sourcing="musinsa", native_id="5874434", color_code="BLACK",
        size_code="270", page_type="checkout",
    )
    p1 = CatchPayload(**base, payload={"final_price": 174000}, captured_at=1.0)
    p2 = CatchPayload(**base, payload={"final_price": 170000}, captured_at=2.0)
    await save_catch(p1)
    await save_catch(p2)
    found = await lookup_catch("musinsa", "5874434", "BLACK", "270")
    assert found.payload["final_price"] == 170000
    assert found.captured_at == 2.0

@pytest.mark.asyncio
async def test_lookup_catch_size_none_fallback(db):
    p_pdp = CatchPayload(
        sourcing="musinsa", native_id="5874434", color_code="BLACK",
        size_code="NONE", page_type="pdp",
        payload={"coupons": ["A"]}, captured_at=time.time(),
    )
    await save_catch(p_pdp)
    found = await lookup_catch("musinsa", "5874434", "BLACK", "270")
    assert found is not None
    assert found.size_code == "NONE"
    assert found.page_type == "pdp"

@pytest.mark.asyncio
async def test_lookup_checkout_preferred_over_pdp(db):
    base = dict(sourcing="musinsa", native_id="5874434", color_code="BLACK")
    await save_catch(CatchPayload(
        **base, size_code="NONE", page_type="pdp",
        payload={"coupons": ["A"]}, captured_at=1.0,
    ))
    await save_catch(CatchPayload(
        **base, size_code="270", page_type="checkout",
        payload={"final_price": 174000}, captured_at=2.0,
    ))
    found = await lookup_catch("musinsa", "5874434", "BLACK", "270")
    assert found.page_type == "checkout"
    assert found.payload["final_price"] == 174000

@pytest.mark.asyncio
async def test_expire_old_catches_removes_expired(db):
    now = time.time()
    p_old = CatchPayload(
        sourcing="musinsa", native_id="OLD", color_code="X", size_code="270",
        page_type="checkout", payload={"final_price": 1},
        captured_at=now - 8 * 86400,
    )
    p_fresh = CatchPayload(
        sourcing="musinsa", native_id="FRESH", color_code="X", size_code="270",
        page_type="checkout", payload={"final_price": 2},
        captured_at=now - 1 * 86400,
    )
    await save_catch(p_old)
    await save_catch(p_fresh)
    n_removed = await expire_old_catches(ttl_days=7)
    assert n_removed == 1
    assert await lookup_catch("musinsa", "OLD", "X", "270") is None
    assert await lookup_catch("musinsa", "FRESH", "X", "270") is not None
```

- [ ] **Step 2-2: 테스트 실패 확인**

```bash
~/kream-venv/bin/pytest tests/test_coupon_store.py -v
```

Expected: FAIL — `src.coupon_store` 모듈 없음.

- [ ] **Step 2-3: `coupon_store.py` 구현**

Create `src/coupon_store.py`:

```python
"""Chrome 확장 catch row 저장소.

매칭 키: (sourcing, native_id, color_code, size_code).
색·사이즈 일치 검증은 호출자(profit_calculator)가 담당.
페이지 우선순위: checkout > pdp (size_code='NONE' 폴백).
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiosqlite

from src.models.database import DB_PATH


@dataclass
class CatchPayload:
    sourcing: str
    native_id: str
    color_code: str
    size_code: str
    page_type: str  # 'pdp' | 'checkout'
    payload: dict[str, Any]
    captured_at: float


async def save_catch(p: CatchPayload) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """
            INSERT INTO coupon_catches
                (sourcing, native_id, color_code, size_code, page_type, payload, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sourcing, native_id, color_code, size_code, page_type)
            DO UPDATE SET
                payload = excluded.payload,
                captured_at = excluded.captured_at
            """,
            (p.sourcing, p.native_id, p.color_code, p.size_code,
             p.page_type, json.dumps(p.payload, ensure_ascii=False), p.captured_at),
        )
        await conn.commit()
        return cur.lastrowid or 0


async def lookup_catch(
    sourcing: str,
    native_id: str,
    color_code: str,
    size_code: str,
) -> Optional[CatchPayload]:
    """checkout 우선, 없으면 동일 color의 PDP(size='NONE') 폴백."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT * FROM coupon_catches
            WHERE sourcing = ? AND native_id = ? AND color_code = ?
              AND (size_code = ? OR (size_code = 'NONE' AND page_type = 'pdp'))
            ORDER BY
                CASE page_type WHEN 'checkout' THEN 0 ELSE 1 END,
                captured_at DESC
            LIMIT 1
            """,
            (sourcing, native_id, color_code, size_code),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return CatchPayload(
            sourcing=row["sourcing"],
            native_id=row["native_id"],
            color_code=row["color_code"],
            size_code=row["size_code"],
            page_type=row["page_type"],
            payload=json.loads(row["payload"]),
            captured_at=row["captured_at"],
        )


async def expire_old_catches(ttl_days: int = 7) -> int:
    cutoff = time.time() - ttl_days * 86400
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "DELETE FROM coupon_catches WHERE captured_at < ?",
            (cutoff,),
        )
        await conn.commit()
        return cur.rowcount
```

- [ ] **Step 2-4: 테스트 통과 확인**

```bash
~/kream-venv/bin/pytest tests/test_coupon_store.py -v
```

Expected: PASS 5/5.

- [ ] **Step 2-5: 커밋**

```bash
git add src/coupon_store.py tests/test_coupon_store.py
git commit -m "feat(coupon): coupon_store CRUD + size_code NONE 폴백 + 7일 만료"
```

---

## Task 3: FastAPI Router `/api/coupon/catch` + 토큰 인증

**Files:**
- Create: `src/api/routers/coupon.py`
- Modify: `src/api/auth.py` (확장 토큰 검증 추가)
- Test: `tests/test_coupon_router.py`

- [ ] **Step 3-1: 기존 auth.py 패턴 확인**

```bash
cat src/api/auth.py
```

`X-Local-Token` 헤더 + `EXTENSION_API_TOKEN` env 토큰 비교 함수 `verify_extension_token` 추가.

- [ ] **Step 3-2: 라우터 테스트 작성**

Create `tests/test_coupon_router.py`:

```python
import os
import pytest
from fastapi.testclient import TestClient
from src.api.server import create_app
from src.models.database import init_db

@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    monkeypatch.setattr("src.models.database.DB_PATH", str(test_db))
    monkeypatch.setattr("src.coupon_store.DB_PATH", str(test_db))
    monkeypatch.setenv("EXTENSION_API_TOKEN", "test-token-123")
    import asyncio
    asyncio.run(init_db())
    return TestClient(create_app())

VALID_PAYLOAD = {
    "sourcing": "musinsa",
    "native_id": "5874434",
    "color_code": "BLACK",
    "size_code": "270",
    "page_type": "checkout",
    "payload": {"final_price": 174000, "card_discounts": []},
    "captured_at": 1777252745.0,
}

def test_catch_post_requires_token(client):
    r = client.post("/api/coupon/catch", json=VALID_PAYLOAD)
    assert r.status_code == 401

def test_catch_post_with_valid_token(client):
    r = client.post(
        "/api/coupon/catch",
        json=VALID_PAYLOAD,
        headers={"X-Local-Token": "test-token-123"},
    )
    assert r.status_code == 200
    assert r.json()["row_id"] > 0

def test_catch_post_validates_page_type(client):
    bad = {**VALID_PAYLOAD, "page_type": "invalid"}
    r = client.post(
        "/api/coupon/catch",
        json=bad,
        headers={"X-Local-Token": "test-token-123"},
    )
    assert r.status_code == 422

def test_catch_post_rejects_wrong_token(client):
    r = client.post(
        "/api/coupon/catch",
        json=VALID_PAYLOAD,
        headers={"X-Local-Token": "wrong"},
    )
    assert r.status_code == 401
```

- [ ] **Step 3-3: 테스트 실패 확인**

```bash
~/kream-venv/bin/pytest tests/test_coupon_router.py -v
```

Expected: FAIL — coupon router 미등록 → 404 / 모듈 없음.

- [ ] **Step 3-4: auth.py 토큰 검증 함수 추가**

Modify `src/api/auth.py` (파일 끝에 추가):

```python
import os
from fastapi import Header, HTTPException


async def verify_extension_token(
    x_local_token: str = Header(default=""),
) -> None:
    expected = os.getenv("EXTENSION_API_TOKEN", "")
    if not expected or x_local_token != expected:
        raise HTTPException(status_code=401, detail="invalid extension token")
```

- [ ] **Step 3-5: coupon 라우터 구현**

Create `src/api/routers/coupon.py`:

```python
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.api.auth import verify_extension_token
from src.coupon_store import CatchPayload, save_catch

router = APIRouter(prefix="/api/coupon", tags=["coupon"])


class CatchRequest(BaseModel):
    sourcing: str = Field(min_length=1, max_length=32)
    native_id: str = Field(min_length=1, max_length=64)
    color_code: str = Field(min_length=1, max_length=64)
    size_code: str = Field(min_length=1, max_length=32)
    page_type: Literal["pdp", "checkout"]
    payload: dict[str, Any]
    captured_at: float


class CatchResponse(BaseModel):
    row_id: int
    status: Literal["ok"] = "ok"


@router.post("/catch", response_model=CatchResponse,
             dependencies=[Depends(verify_extension_token)])
async def catch_coupon(req: CatchRequest) -> CatchResponse:
    row_id = await save_catch(CatchPayload(
        sourcing=req.sourcing,
        native_id=req.native_id,
        color_code=req.color_code,
        size_code=req.size_code,
        page_type=req.page_type,
        payload=req.payload,
        captured_at=req.captured_at,
    ))
    return CatchResponse(row_id=row_id)
```

- [ ] **Step 3-6: server.py에 router 등록**

Modify `src/api/server.py`:

```python
from src.api.routers import bot, coupon, dashboard, logs, ops, sources
# ... 기존 코드 ...
    app.include_router(coupon.router)  # ops/bot/sources 다음 줄에 추가
```

- [ ] **Step 3-7: 테스트 통과 확인**

```bash
~/kream-venv/bin/pytest tests/test_coupon_router.py -v
```

Expected: PASS 4/4.

- [ ] **Step 3-8: 커밋**

```bash
git add src/api/routers/coupon.py src/api/auth.py src/api/server.py tests/test_coupon_router.py
git commit -m "feat(api): POST /api/coupon/catch + EXTENSION_API_TOKEN 인증"
```

---

## Task 4: `.env.example` + CORS 정밀화

**Files:**
- Modify: `.env.example` (또는 신규)
- Modify: `src/api/server.py` (CORS allow_origins 좁히기 — 단, MV3 chrome-extension:// origin 허용 필요)

- [ ] **Step 4-1: .env.example 갱신**

```bash
grep -q "EXTENSION_API_TOKEN" .env.example 2>/dev/null || echo "
# Chrome 확장 인증 토큰 (확장 popup에서 1회 설정)
EXTENSION_API_TOKEN=
" >> .env.example
```

- [ ] **Step 4-2: CORS 패턴 검토 + 적용**

Modify `src/api/server.py`의 `add_middleware(CORSMiddleware, ...)`:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(http://localhost(:\d+)?|chrome-extension://[a-z]+)$",
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*", "X-Local-Token"],
)
```

- [ ] **Step 4-3: 기존 라우터 회귀 확인**

```bash
~/kream-venv/bin/pytest tests/test_api_*.py tests/test_coupon_router.py -v
```

Expected: 전 PASS.

- [ ] **Step 4-4: 커밋**

```bash
git add .env.example src/api/server.py
git commit -m "feat(api): EXTENSION_API_TOKEN .env.example + CORS chrome-extension:// 허용"
```

---

## Task 5: `profit_calculator` catch hook + 매칭 검증 게이트

**Files:**
- Modify: `src/profit_calculator.py`
- Test: `tests/test_profit_calculator_with_catch.py`

- [ ] **Step 5-1: 기존 profit_calculator 시그너처 확인**

```bash
grep -n "def calculate\|def compute\|sell_now\|final_price\|class.*Calculator" src/profit_calculator.py | head -20
```

진입점 함수명 확인 (예: `calculate_profit`).

- [ ] **Step 5-2: hook 테스트 작성**

Create `tests/test_profit_calculator_with_catch.py`:

```python
import time
import pytest
from src.coupon_store import CatchPayload, save_catch
from src.models.database import init_db
from src.profit_calculator import apply_catch_to_buy_price

@pytest.fixture
async def db(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    monkeypatch.setattr("src.models.database.DB_PATH", str(test_db))
    monkeypatch.setattr("src.coupon_store.DB_PATH", str(test_db))
    await init_db()
    return test_db

@pytest.mark.asyncio
async def test_no_catch_returns_original_price(db):
    result = await apply_catch_to_buy_price(
        sourcing="musinsa", native_id="X", color_code="BLACK", size_code="270",
        original_price=200000,
    )
    assert result.buy_price == 200000
    assert result.catch_applied is False

@pytest.mark.asyncio
async def test_checkout_catch_uses_final_price_directly(db):
    await save_catch(CatchPayload(
        sourcing="musinsa", native_id="5874434", color_code="BLACK",
        size_code="270", page_type="checkout",
        payload={"final_price": 174000}, captured_at=time.time(),
    ))
    result = await apply_catch_to_buy_price(
        sourcing="musinsa", native_id="5874434", color_code="BLACK", size_code="270",
        original_price=200000,
    )
    assert result.buy_price == 174000
    assert result.catch_applied is True
    assert result.catch_source == "checkout"

@pytest.mark.asyncio
async def test_color_mismatch_no_catch(db):
    await save_catch(CatchPayload(
        sourcing="musinsa", native_id="5874434", color_code="BLACK",
        size_code="270", page_type="checkout",
        payload={"final_price": 174000}, captured_at=time.time(),
    ))
    result = await apply_catch_to_buy_price(
        sourcing="musinsa", native_id="5874434", color_code="WHITE", size_code="270",
        original_price=200000,
    )
    assert result.buy_price == 200000
    assert result.catch_applied is False

@pytest.mark.asyncio
async def test_size_mismatch_falls_back_to_pdp_none(db):
    await save_catch(CatchPayload(
        sourcing="musinsa", native_id="5874434", color_code="BLACK",
        size_code="NONE", page_type="pdp",
        payload={"coupons": [{"discount": 5000}], "final_price": None},
        captured_at=time.time(),
    ))
    result = await apply_catch_to_buy_price(
        sourcing="musinsa", native_id="5874434", color_code="BLACK", size_code="270",
        original_price=200000,
    )
    assert result.catch_applied is True
    assert result.catch_source == "pdp"
    # PDP catch는 final_price 없을 수 있음 → 쿠폰 합산
    assert result.buy_price == 195000
```

- [ ] **Step 5-3: 테스트 실패 확인**

```bash
~/kream-venv/bin/pytest tests/test_profit_calculator_with_catch.py -v
```

Expected: FAIL — `apply_catch_to_buy_price` 미구현.

- [ ] **Step 5-4: hook 함수 구현**

`src/profit_calculator.py` 파일 끝에 추가:

```python
from dataclasses import dataclass
from typing import Optional

from src.coupon_store import lookup_catch


@dataclass
class CatchAppliedPrice:
    buy_price: int
    catch_applied: bool
    catch_source: Optional[str] = None  # 'checkout' | 'pdp' | None


async def apply_catch_to_buy_price(
    *,
    sourcing: str,
    native_id: str,
    color_code: str,
    size_code: str,
    original_price: int,
) -> CatchAppliedPrice:
    """매칭 검증(색·사이즈 일치) 통과한 catch row가 있으면 그 값으로 정확 계산.

    - checkout catch: payload.final_price 그대로 사용 (할인 합산 X)
    - pdp catch (size='NONE' 폴백): payload.coupons[].discount 합산
    - catch 없음 또는 색 불일치: original_price 그대로
    """
    catch = await lookup_catch(sourcing, native_id, color_code, size_code)
    if catch is None:
        return CatchAppliedPrice(buy_price=original_price, catch_applied=False)

    if catch.page_type == "checkout":
        final = catch.payload.get("final_price")
        if isinstance(final, (int, float)) and final > 0:
            return CatchAppliedPrice(
                buy_price=int(final),
                catch_applied=True,
                catch_source="checkout",
            )

    if catch.page_type == "pdp":
        coupons = catch.payload.get("coupons", [])
        total_discount = sum(
            int(c.get("discount", 0))
            for c in coupons
            if isinstance(c, dict)
        )
        if total_discount > 0:
            return CatchAppliedPrice(
                buy_price=max(original_price - total_discount, 0),
                catch_applied=True,
                catch_source="pdp",
            )

    return CatchAppliedPrice(buy_price=original_price, catch_applied=False)
```

- [ ] **Step 5-5: 테스트 통과 확인**

```bash
~/kream-venv/bin/pytest tests/test_profit_calculator_with_catch.py -v
```

Expected: PASS 4/4.

- [ ] **Step 5-6: 기존 profit_calculator 회귀 확인**

```bash
~/kream-venv/bin/pytest tests/test_profit_calculator.py -v
```

Expected: 전 PASS (hook은 추가 함수, 기존 흐름 영향 X).

- [ ] **Step 5-7: 커밋**

```bash
git add src/profit_calculator.py tests/test_profit_calculator_with_catch.py
git commit -m "feat(profit): apply_catch_to_buy_price hook — checkout final_price 그대로, pdp 쿠폰 합산"
```

---

## Task 6: Chrome 확장 골격 (manifest + background + lib)

**Files:**
- Create: `extension/manifest.json`
- Create: `extension/background.js`
- Create: `extension/lib/api.js`
- Create: `extension/lib/extractor.js`
- Create: `extension/package.json`
- Create: `extension/vitest.config.js`
- Test: `extension/tests/api.test.js`

- [ ] **Step 6-1: 디렉터리 + package.json**

Create `extension/package.json`:

```json
{
  "name": "kream-bot-extension",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "devDependencies": {
    "vitest": "^2.0.0",
    "jsdom": "^25.0.0",
    "@vitest/coverage-v8": "^2.0.0"
  }
}
```

```bash
cd extension && npm install && cd ..
```

- [ ] **Step 6-2: manifest.json (MV3)**

Create `extension/manifest.json`:

```json
{
  "manifest_version": 3,
  "name": "크림봇 무신사 쿠폰 Catch",
  "version": "0.1.0",
  "description": "무신사 PDP/결제 페이지에서 카드 즉시할인·쿠폰을 catch하여 봇에 전송",
  "permissions": ["storage", "activeTab"],
  "host_permissions": [
    "https://www.musinsa.com/*",
    "https://goods-detail.musinsa.com/*",
    "http://localhost:8000/*"
  ],
  "background": {
    "service_worker": "background.js",
    "type": "module"
  },
  "content_scripts": [
    {
      "matches": ["https://www.musinsa.com/*"],
      "js": ["content/musinsa.js"],
      "run_at": "document_idle"
    }
  ],
  "action": {
    "default_popup": "popup/popup.html",
    "default_title": "크림봇 쿠폰 Catch"
  }
}
```

- [ ] **Step 6-3: lib/api.js 테스트**

Create `extension/tests/api.test.js`:

```javascript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { postCatch } from "../lib/api.js";

describe("postCatch", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
    globalThis.chrome = {
      storage: {
        local: {
          get: vi.fn().mockResolvedValue({ apiToken: "test-token" }),
        },
      },
    };
  });

  it("posts to localhost:8000 with X-Local-Token header", async () => {
    fetch.mockResolvedValue({ ok: true, json: async () => ({ row_id: 42 }) });
    const result = await postCatch({
      sourcing: "musinsa",
      native_id: "5874434",
      color_code: "BLACK",
      size_code: "270",
      page_type: "checkout",
      payload: { final_price: 174000 },
      captured_at: 1.0,
    });
    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/coupon/catch",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          "X-Local-Token": "test-token",
        }),
      }),
    );
    expect(result.row_id).toBe(42);
  });

  it("throws when token missing", async () => {
    chrome.storage.local.get.mockResolvedValue({});
    await expect(postCatch({})).rejects.toThrow(/token/i);
  });
});
```

- [ ] **Step 6-4: lib/api.js 구현**

Create `extension/lib/api.js`:

```javascript
const API_BASE = "http://localhost:8000";

export async function postCatch(payload) {
  const stored = await chrome.storage.local.get(["apiToken"]);
  if (!stored.apiToken) {
    throw new Error("EXTENSION_API_TOKEN not set in storage");
  }
  const res = await fetch(`${API_BASE}/api/coupon/catch`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Local-Token": stored.apiToken,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`POST /api/coupon/catch failed: ${res.status}`);
  }
  return await res.json();
}

export async function setApiToken(token) {
  await chrome.storage.local.set({ apiToken: token });
}

export async function getApiToken() {
  const stored = await chrome.storage.local.get(["apiToken"]);
  return stored.apiToken || null;
}
```

- [ ] **Step 6-5: lib/extractor.js (DOM scan 헬퍼)**

Create `extension/lib/extractor.js`:

```javascript
export function safeText(el) {
  return el ? (el.textContent || "").trim() : "";
}

export function safeNumber(text) {
  if (!text) return null;
  const cleaned = String(text).replace(/[^\d.-]/g, "");
  const n = parseFloat(cleaned);
  return Number.isFinite(n) ? n : null;
}

export function querySelectorAllText(root, selector) {
  return Array.from(root.querySelectorAll(selector)).map(safeText);
}

export function captureNetworkResponses(urlPattern, callback) {
  // chrome.webRequest는 background에서만 가능 — content script는 직접 fetch 가로채기
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    try {
      const url = typeof args[0] === "string" ? args[0] : args[0].url;
      if (urlPattern.test(url)) {
        const cloned = res.clone();
        const data = await cloned.json().catch(() => null);
        if (data) callback({ url, data });
      }
    } catch (e) {}
    return res;
  };
}
```

- [ ] **Step 6-6: background.js (메시지 라우팅)**

Create `extension/background.js`:

```javascript
import { postCatch } from "./lib/api.js";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "catch") {
    postCatch(msg.payload)
      .then((res) => sendResponse({ ok: true, ...res }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  return false;
});
```

- [ ] **Step 6-7: vitest 실행**

```bash
cd extension && npm test
```

Expected: PASS 2/2.

- [ ] **Step 6-8: 커밋**

```bash
cd /mnt/c/Users/USER/Desktop/크림봇/.worktrees/ui-bot-phase-a
git add extension/
git commit -m "feat(extension): MV3 골격 + lib/api + lib/extractor + background 메시지 라우팅"
```

---

## Task 7: 무신사 plug-in (PDP + 결제 페이지 catch)

**Files:**
- Create: `extension/content/musinsa.js`
- Test: `extension/tests/musinsa.test.js`

- [ ] **Step 7-1: musinsa plug-in 테스트**

Create `extension/tests/musinsa.test.js`:

```javascript
import { describe, it, expect, beforeEach } from "vitest";
import { JSDOM } from "jsdom";
import {
  detectPageType,
  extractNativeId,
  extractColorCode,
  extractSizeCode,
  scanCheckoutFromDom,
} from "../content/musinsa.js";

describe("musinsa plug-in", () => {
  it("detects PDP from URL", () => {
    expect(detectPageType("https://www.musinsa.com/products/5874434")).toBe("pdp");
  });

  it("detects checkout from URL", () => {
    expect(detectPageType("https://www.musinsa.com/order/checkout?orderId=...")).toBe("checkout");
  });

  it("returns null for unknown page", () => {
    expect(detectPageType("https://www.musinsa.com/")).toBe(null);
  });

  it("extracts native_id from PDP URL", () => {
    expect(extractNativeId("https://www.musinsa.com/products/5874434")).toBe("5874434");
  });

  it("extracts color_code from selected option DOM", () => {
    const dom = new JSDOM(`
      <div data-mds-component="ProductOptionSelect">
        <button aria-pressed="true" data-color-code="BLACK">블랙</button>
        <button aria-pressed="false" data-color-code="WHITE">화이트</button>
      </div>
    `);
    expect(extractColorCode(dom.window.document)).toBe("BLACK");
  });

  it("extracts size_code from selected size button", () => {
    const dom = new JSDOM(`
      <div data-mds-component="SizeSelector">
        <button aria-pressed="true" data-size="270">270</button>
      </div>
    `);
    expect(extractSizeCode(dom.window.document)).toBe("270");
  });

  it("size_code returns NONE when not selected", () => {
    const dom = new JSDOM(`<div></div>`);
    expect(extractSizeCode(dom.window.document)).toBe("NONE");
  });

  it("scanCheckoutFromDom captures final_price + card_discounts", () => {
    const dom = new JSDOM(`
      <div data-test="final-price">174,000원</div>
      <ul data-test="card-discounts">
        <li data-card="BC" data-pay="musinsa_pay" data-discount="4000">BC카드 4,000원 할인</li>
      </ul>
    `);
    const result = scanCheckoutFromDom(dom.window.document);
    expect(result.final_price).toBe(174000);
    expect(result.card_discounts).toEqual([
      { card: "BC", pay: "musinsa_pay", discount: 4000 },
    ]);
  });
});
```

- [ ] **Step 7-2: 테스트 실패 확인**

```bash
cd extension && npm test -- musinsa
```

Expected: FAIL — `content/musinsa.js` 없음.

- [ ] **Step 7-3: musinsa plug-in 구현**

Create `extension/content/musinsa.js`:

```javascript
import { safeText, safeNumber, captureNetworkResponses } from "../lib/extractor.js";

export function detectPageType(url) {
  if (/\/products\/\d+/.test(url)) return "pdp";
  if (/\/order\/(checkout|pay)/.test(url)) return "checkout";
  return null;
}

export function extractNativeId(url) {
  const m = url.match(/\/products\/(\d+)/);
  if (m) return m[1];
  const params = new URLSearchParams(new URL(url, "https://www.musinsa.com").search);
  return params.get("goodsNo") || params.get("orderNo") || "";
}

export function extractColorCode(doc) {
  const sel = doc.querySelector(
    '[data-mds-component="ProductOptionSelect"] [aria-pressed="true"]',
  );
  if (sel?.dataset?.colorCode) return sel.dataset.colorCode;
  return safeText(sel) || "NONE";
}

export function extractSizeCode(doc) {
  const sel = doc.querySelector(
    '[data-mds-component="SizeSelector"] [aria-pressed="true"]',
  );
  if (sel?.dataset?.size) return sel.dataset.size;
  return "NONE";
}

export function scanCheckoutFromDom(doc) {
  const finalPriceEl = doc.querySelector('[data-test="final-price"]');
  const cardEls = doc.querySelectorAll('[data-test="card-discounts"] li');
  const card_discounts = Array.from(cardEls).map((el) => ({
    card: el.dataset.card || "",
    pay: el.dataset.pay || "",
    discount: safeNumber(el.dataset.discount) || 0,
  }));
  const couponEls = doc.querySelectorAll('[data-test="timed-coupons"] li');
  const timed_coupons = Array.from(couponEls).map((el) => ({
    name: safeText(el.querySelector("[data-name]")) || safeText(el),
    discount: safeNumber(el.dataset.discount) || 0,
  }));
  const autoEls = doc.querySelectorAll('[data-test="auto-matched-coupons"] li');
  const auto_matched_coupons = Array.from(autoEls).map((el) => ({
    name: safeText(el),
    discount: safeNumber(el.dataset.discount) || 0,
  }));
  return {
    final_price: safeNumber(safeText(finalPriceEl)),
    card_discounts,
    timed_coupons,
    auto_matched_coupons,
  };
}

export function scanPdpFromDom(doc) {
  const couponEls = doc.querySelectorAll('[data-test="issuable-coupons"] li');
  const coupons = Array.from(couponEls).map((el) => ({
    name: safeText(el.querySelector("[data-name]")) || safeText(el),
    discount: safeNumber(el.dataset.discount) || 0,
  }));
  return { coupons };
}

async function dispatchCatch(pageType, payload) {
  const url = window.location.href;
  const native_id = extractNativeId(url);
  if (!native_id) return;
  const color_code = extractColorCode(document);
  const size_code = pageType === "checkout"
    ? extractSizeCode(document)
    : "NONE";
  const msg = {
    type: "catch",
    payload: {
      sourcing: "musinsa",
      native_id,
      color_code,
      size_code,
      page_type: pageType,
      payload,
      captured_at: Date.now() / 1000,
    },
  };
  await chrome.runtime.sendMessage(msg);
}

function init() {
  const pageType = detectPageType(window.location.href);
  if (!pageType) return;

  if (pageType === "pdp") {
    const payload = scanPdpFromDom(document);
    dispatchCatch("pdp", payload);
    captureNetworkResponses(/\/api2\/goods\/\d+/, ({ data }) => {
      const enriched = { ...payload, raw_pdp_api: data };
      dispatchCatch("pdp", enriched);
    });
  }

  if (pageType === "checkout") {
    const observer = new MutationObserver(() => {
      const payload = scanCheckoutFromDom(document);
      if (payload.final_price) {
        dispatchCatch("checkout", payload);
      }
    });
    observer.observe(document.body, { subtree: true, childList: true, attributes: true });
    const payload = scanCheckoutFromDom(document);
    if (payload.final_price) dispatchCatch("checkout", payload);
  }
}

if (typeof window !== "undefined" && typeof chrome !== "undefined") {
  init();
}
```

- [ ] **Step 7-4: 테스트 통과 확인**

```bash
cd extension && npm test -- musinsa
```

Expected: PASS 8/8.

- [ ] **Step 7-5: 커밋**

```bash
cd /mnt/c/Users/USER/Desktop/크림봇/.worktrees/ui-bot-phase-a
git add extension/content/musinsa.js extension/tests/musinsa.test.js
git commit -m "feat(extension): musinsa plug-in PDP+checkout catch — final_price/card/timed/auto coupon"
```

---

## Task 8: Popup UI (카드 toggle 가이드 + catch 카운터)

**Files:**
- Create: `extension/popup/popup.html`
- Create: `extension/popup/popup.css`
- Create: `extension/popup/popup.js`

- [ ] **Step 8-1: popup.html**

Create `extension/popup/popup.html`:

```html
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <link rel="stylesheet" href="popup.css" />
  <title>크림봇 쿠폰 Catch</title>
</head>
<body>
  <header>
    <h1>크림봇 쿠폰 Catch</h1>
    <span id="status-dot" class="dot off"></span>
  </header>
  <section id="token-section">
    <label for="token-input">EXTENSION_API_TOKEN</label>
    <input id="token-input" type="password" placeholder=".env 와 동일한 토큰 입력" />
    <button id="token-save">저장</button>
    <p id="token-msg" class="msg"></p>
  </section>
  <section id="counter-section">
    <h2>이번 페이지 catch</h2>
    <ul id="catch-list"></ul>
  </section>
  <section id="card-guide">
    <h2>카드 toggle 가이드</h2>
    <p>결제 페이지에서 아래 카드를 한 번씩 toggle 해주세요 (catch 완료 시 ✅).</p>
    <ul id="card-checklist">
      <li data-card="BC-musinsa">무신사페이 × BC카드</li>
      <li data-card="NH-musinsa">무신사페이 × 농협카드</li>
      <li data-card="SS-toss">토스페이 × 삼성카드</li>
      <li data-card="LT-toss">토스페이 × 롯데카드</li>
      <li data-card="ACC-toss">토스페이 × 계좌</li>
      <li data-card="WR-kakao">카카오페이 × 우리카드</li>
      <li data-card="PM-kakao">카카오페이 × 페이머니</li>
    </ul>
  </section>
  <script type="module" src="popup.js"></script>
</body>
</html>
```

- [ ] **Step 8-2: popup.css (Tailwind 없이 단순 inline-ish)**

Create `extension/popup/popup.css`:

```css
* { box-sizing: border-box; }
body {
  font-family: -apple-system, system-ui, sans-serif;
  width: 320px;
  margin: 0;
  padding: 12px;
  font-size: 13px;
  color: #1a1a1a;
}
header { display: flex; justify-content: space-between; align-items: center; }
h1 { font-size: 14px; margin: 0; }
h2 { font-size: 12px; margin: 12px 0 4px; color: #555; }
.dot { width: 8px; height: 8px; border-radius: 50%; }
.dot.on { background: #2ecc71; }
.dot.off { background: #e74c3c; }
#token-input { width: 100%; padding: 4px; margin: 4px 0; }
#token-save { padding: 4px 12px; }
.msg { font-size: 11px; margin: 4px 0 0; min-height: 14px; }
.msg.ok { color: #2ecc71; }
.msg.err { color: #e74c3c; }
ul { list-style: none; padding: 0; margin: 0; }
li { padding: 2px 0; display: flex; justify-content: space-between; }
li.done::after { content: "✅"; }
```

- [ ] **Step 8-3: popup.js**

Create `extension/popup/popup.js`:

```javascript
import { setApiToken, getApiToken } from "../lib/api.js";

const tokenInput = document.getElementById("token-input");
const tokenBtn = document.getElementById("token-save");
const tokenMsg = document.getElementById("token-msg");
const statusDot = document.getElementById("status-dot");
const catchList = document.getElementById("catch-list");

async function refreshToken() {
  const t = await getApiToken();
  if (t) {
    tokenInput.value = "*".repeat(t.length);
    statusDot.classList.replace("off", "on");
  }
}

tokenBtn.addEventListener("click", async () => {
  const v = tokenInput.value.trim();
  if (!v) {
    tokenMsg.textContent = "토큰을 입력하세요";
    tokenMsg.className = "msg err";
    return;
  }
  await setApiToken(v);
  tokenMsg.textContent = "저장됨";
  tokenMsg.className = "msg ok";
  statusDot.classList.replace("off", "on");
});

async function refreshCatchList() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url?.includes("musinsa.com")) {
    catchList.innerHTML = '<li>무신사 페이지가 아닙니다</li>';
    return;
  }
  const stored = await chrome.storage.local.get(["lastCatch"]);
  const last = stored.lastCatch;
  if (!last) {
    catchList.innerHTML = '<li>아직 catch 없음</li>';
    return;
  }
  catchList.innerHTML = `
    <li>page: ${last.page_type}</li>
    <li>color: ${last.color_code}</li>
    <li>size: ${last.size_code}</li>
    <li>final: ${last.payload.final_price ?? "—"}</li>
  `;
  if (last.page_type === "checkout" && Array.isArray(last.payload.card_discounts)) {
    for (const cd of last.payload.card_discounts) {
      const key = `${cd.card}-${cd.pay.split("_")[0]}`;
      const li = document.querySelector(`[data-card="${key}"]`);
      if (li) li.classList.add("done");
    }
  }
}

refreshToken();
refreshCatchList();
```

- [ ] **Step 8-4: background.js — lastCatch 저장 추가**

Modify `extension/background.js`:

```javascript
import { postCatch } from "./lib/api.js";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "catch") {
    chrome.storage.local.set({ lastCatch: msg.payload });
    postCatch(msg.payload)
      .then((res) => sendResponse({ ok: true, ...res }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  return false;
});
```

- [ ] **Step 8-5: 커밋**

```bash
git add extension/popup/ extension/background.js
git commit -m "feat(extension): popup UI — 토큰 저장 + catch 카운터 + 카드 7개 toggle 가이드"
```

---

## Task 9: e2e manual 검증 가이드 + 회귀 + 머지 준비

**Files:**
- Create: `docs/extension-manual-verify-musinsa.md`

- [ ] **Step 9-1: 검증 가이드 문서**

Create `docs/extension-manual-verify-musinsa.md`:

```markdown
# Chrome 확장 무신사 Phase 1 — manual e2e 검증 가이드

## 사전 준비
1. `.worktrees/ui-bot-phase-a` 에서 `python main.py` 가동 (FastAPI 8000 포함)
2. `.env` 에 `EXTENSION_API_TOKEN=<랜덤 32자>` 추가
3. Chrome → `chrome://extensions` → 개발자 모드 → "압축해제된 확장 로드" → `.worktrees/ui-bot-phase-a/extension/`
4. 확장 popup → 토큰 입력 → 녹색 점 확인

## Step 1 — PDP catch
1. 무신사 신발 1개 페이지 진입 (예: `https://www.musinsa.com/products/5874434`)
2. 색상 + 사이즈 선택
3. 확장 popup → "이번 페이지 catch" 항목 = `pdp / BLACK / 270` 등 표시 확인
4. SQLite 확인:
   ```sql
   SELECT * FROM coupon_catches WHERE page_type='pdp' ORDER BY id DESC LIMIT 1;
   ```
   → row 1건 + payload.coupons JSON 포함

## Step 2 — 결제 페이지 catch (카드 7개)
1. 동일 상품 → 결제 페이지 진입 (구매 버튼 → 결제하기, 결제 직전까지만)
2. 카드 7개를 한 번씩 toggle. 각 toggle 후 popup 열어 "card_discounts" row 누적 확인
3. 7개 모두 toggle 완료 → popup 카드 checklist 7개 ✅
4. SQLite:
   ```sql
   SELECT count(*) FROM coupon_catches WHERE page_type='checkout' AND native_id='5874434';
   ```
   → 1 (UPSERT 라 마지막 결제가만)
5. **결제 진행 X — 페이지 닫기**

## Step 3 — 봇 알림 적용 확인
1. 봇 decision_log 모니터링:
   ```bash
   tail -f logs/bot.out | grep "5874434"
   ```
2. 매칭 후보 발생 시 `decision_log.reason='catch_applied'` 또는 Discord embed에 "실측 결제가 174,000원 (확장 catch)" 표시 확인

## Step 4 — 색상/사이즈 mismatch 회귀
1. 다른 색상으로 매칭 후보 시뮬 → catch row 색상 불일치 → catch 미적용 → 보수 추정 그대로 확인
2. `tests/test_profit_calculator_with_catch.py::test_color_mismatch_no_catch` 가 PASS 인지 재확인

## Step 5 — 만료 회귀
1. SQLite 직접 8일 전 row 삽입 → `expire_old_catches(7)` 호출 → row 삭제 확인
```

- [ ] **Step 9-2: 전체 회귀 테스트**

```bash
~/kream-venv/bin/pytest tests/ -v -x
cd extension && npm test
```

Expected: 전 PASS.

- [ ] **Step 9-3: 봇 정상 가동 회귀 확인**

```bash
cd /mnt/c/Users/USER/Desktop/크림봇/.worktrees/ui-bot-phase-a
~/kream-venv/bin/python -c "from src.api.server import create_app; app = create_app(); print('routes:', [r.path for r in app.routes])"
```

Expected: `/api/coupon/catch` 포함.

- [ ] **Step 9-4: 커밋 + push (worktree branch)**

```bash
git add docs/extension-manual-verify-musinsa.md
git commit -m "docs(extension): manual e2e 검증 가이드 — PDP/결제/색상mismatch/만료 회귀"
git push origin feature/ui-bot-phase-a
```

- [ ] **Step 9-5: 사용자 PC e2e 검증 안내**

사용자에게:
> Phase 1 무신사 Chrome 확장 코드 + 테스트 완료. 사용자 PC 에서 `docs/extension-manual-verify-musinsa.md` Step 1~5 수행 후 결과 보고. 통과 시 Phase A + Phase 1 한 번에 master 머지 → Phase 2 (나이키 plug-in, D-day) 진입.

---

## Self-Review 체크 (plan 작성자 확인)

### 1. Spec 커버리지
| Spec 섹션 | 구현 Task |
|---|---|
| §1 목적 + 불변 원칙 (사이즈·컬러 매칭 1순위) | T5 (매칭 검증 게이트), T7 (color/size 추출) |
| §2 비목적 (자동 결제/매크로 X) | 전 task — 카트 add 코드 없음 |
| §3 우선순위 (무신사 → 나이키 D-day) | T6 plug-in 골격 = 나이키 plug-in 1파일 추가만 |
| §4 아키텍처 | T6 (manifest+background+lib), T7 (musinsa) |
| §5 catch 항목 + 5.0 매칭 검증 게이트 | T5 + T7 (PDP/결제 페이지 scan 함수) |
| §6 매칭 키 + size_code | T1 (스키마), T2 (NONE 폴백), T5 (게이트), T7 (추출) |
| §6.4 final_price 그대로 적용 | T5 (test_checkout_catch_uses_final_price_directly) |
| §7 보안 (localhost + 토큰) | T3 (verify_extension_token), T4 (CORS regex) |
| §8 22 소싱처 확장 골격 | T6 plug-in 인터페이스 (detectPageType/extractNativeId/scanPdp/scanCheckout) |
| §9 테스트 전략 | T1-T8 단위 + T9 manual e2e |
| §10 파일 list | 전 task에 명시 |
| §11 미해결 질문 | T9 검증 단계에서 카드 toggle 시점/사이즈 정규화 직접 확인 |

→ 갭 없음.

### 2. 플레이스홀더 스캔
- "TBD"/"TODO"/"implement later" 검색 → 없음 ✓
- 모든 코드 블록은 실제 구현 (No Placeholders 원칙) ✓
- 테스트는 assert 패턴 + 실제 시그니처 ✓

### 3. 타입 일관성
- `CatchPayload` (dataclass) — Task 2 정의 → Task 3 import → Task 5 lookup_catch 반환 ✓
- `CatchAppliedPrice` — Task 5에서만 사용 ✓
- `apply_catch_to_buy_price` (kw-only) — Task 5 정의, 외부 호출 X (이번 plan 범위 밖, profit_calculator 메인 흐름과의 통합은 Phase 1.5 follow-up) ✓
- 확장 plug-in 인터페이스: `detectPageType`/`extractNativeId`/`extractColorCode`/`extractSizeCode`/`scanPdpFromDom`/`scanCheckoutFromDom` — Task 7 정의, Task 6 lib는 호출만 ✓

→ 일관성 OK. 실제 통합 (`apply_catch_to_buy_price` 를 profit_calculator 메인 calculate 흐름에서 호출)은 결제 페이지 selector가 manual 검증 후 확정되어야 안전 → Phase 1.5 별도 task로 이월.

---

## 다음 단계

Plan complete and saved to `docs/superpowers/plans/2026-04-27-chrome-extension-musinsa-phase1.md`.

**실행 옵션 2 가지:**

1. **Subagent-Driven (권장)** — task당 fresh subagent, task 사이 사용자 리뷰 체크포인트, 빠른 반복
2. **Inline Execution** — 현 세션에서 executing-plans 스킬로 batch 실행, 체크포인트 단위 사용자 리뷰

어느 방식으로 진행할까요?
