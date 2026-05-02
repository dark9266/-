# Phase B-0 — Dry-Run Slot Capture Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 보관판매 슬롯 캐치 봇팅 워커 코어 구현 (실 POST X). dry-run mode 로 mock 응답 사용 → 시도 루프 / backoff / 종료 조건 / 안전장치 8종 단위 테스트.

**Architecture:** 신규 모듈 `src/storage_sale/` 격리. 봇/UI 와 분리. CLI 스크립트로 가동 (Phase A 머지 후 UI 연결).

**Tech Stack:** Python 3.12 async, httpx, dataclass, pytest

**Spec:** `docs/superpowers/specs/2026-05-01-phase-b-storage-sale-bot.md`

---

## File Structure

| 파일 | 책임 |
|---|---|
| `src/storage_sale/__init__.py` | 모듈 exports |
| `src/storage_sale/models.py` | SlotRequest / SlotResult / ConfirmRequest dataclass |
| `src/storage_sale/slot_worker.py` | Phase B1 — 1페이지 무한 시도 루프 (dry-run + 실 모드) |
| `src/storage_sale/confirm_worker.py` | Phase B3 — 2페이지 단일 시도 |
| `src/storage_sale/runner.py` | B1 → B3 파이프라인 + 시작/중지 신호 |
| `src/storage_sale/auth.py` | 세션 쿠키/헤더 관리 (dry-run 에선 mock) |
| `tests/storage_sale/test_slot_worker.py` | 시도 루프 / backoff / 종료 단위 테스트 |
| `tests/storage_sale/test_confirm_worker.py` | 단일 시도 / 재시도 / 실패 |
| `tests/storage_sale/test_runner.py` | B1 → B3 파이프라인 |

---

## Task 1: 데이터 모델 (TDD)

**Files:** `src/storage_sale/models.py`, `tests/storage_sale/test_models.py`

- [ ] **Step 1: 실패 테스트**

```python
# tests/storage_sale/test_models.py
def test_slot_request_defaults():
    from src.storage_sale.models import SlotRequest
    req = SlotRequest(product_id="123", product_option_key="265")
    assert req.quantity == 1
    assert req.max_duration_minutes == 30
    assert req.interval_seconds == 1.0


def test_slot_result_success():
    from src.storage_sale.models import SlotResult
    r = SlotResult(success=True, review_id="abc", attempts=42)
    assert r.success
    assert r.review_id == "abc"


def test_slot_result_failure():
    from src.storage_sale.models import SlotResult
    r = SlotResult(success=False, error_code="USER_STOP", attempts=10)
    assert not r.success
    assert r.review_id is None
```

- [ ] **Step 2: 실패 확인 + 모델 구현**

```python
# src/storage_sale/models.py
from dataclasses import dataclass


@dataclass
class SlotRequest:
    product_id: str
    product_option_key: str  # 사이즈 (예: "265")
    quantity: int = 1
    max_duration_minutes: int = 30
    interval_seconds: float = 1.0


@dataclass
class SlotResult:
    success: bool
    review_id: str | None = None
    error_code: str | None = None  # USER_STOP, TIMEOUT, BAN, AUTH_FAIL, SERVER_ERROR
    attempts: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class ConfirmRequest:
    review_id: str
    return_address_id: int
    return_shipping_memo: str = ""
    delivery_method: str = ""


@dataclass
class ConfirmResult:
    success: bool
    response_data: dict | None = None
    error_message: str | None = None
```

- [ ] **Step 3: 테스트 통과 + 커밋**

```bash
~/kream-venv/bin/python -m pytest tests/storage_sale/test_models.py -v
git add src/storage_sale/models.py src/storage_sale/__init__.py tests/storage_sale/__init__.py tests/storage_sale/test_models.py
git commit -m "feat(storage_sale): 데이터 모델 (Phase B-0)"
```

---

## Task 2: Mock Auth (dry-run only)

**Files:** `src/storage_sale/auth.py`, `tests/storage_sale/test_auth.py`

- [ ] **Step 1: 실패 테스트**

```python
def test_mock_session_returns_headers():
    from src.storage_sale.auth import MockSession
    s = MockSession()
    h = s.get_headers()
    assert "X-KREAM-DEVICE-ID" in h
    assert "X-KREAM-WEB-REQUEST-SECRET" in h
```

- [ ] **Step 2: 구현 + 통과 + 커밋**

```python
# src/storage_sale/auth.py
class MockSession:
    """Phase B-0 dry-run 용. 실 인증 X. Stage B-1 진입 시 RealSession 추가."""

    def get_headers(self) -> dict[str, str]:
        return {
            "X-KREAM-DEVICE-ID": "web;mock-did",
            "X-KREAM-WEB-REQUEST-SECRET": "kream-djscjsghdkd",
            "X-KREAM-API-VERSION": "57",
            "X-KREAM-CLIENT-DATETIME": "2026-05-01T14:00:00",
            "X-KREAM-WEB-BUILD-VERSION": "26.5.6",
        }
```

```bash
git add src/storage_sale/auth.py tests/storage_sale/test_auth.py
git commit -m "feat(storage_sale): MockSession (dry-run, Phase B-0)"
```

---

## Task 3: SlotWorker — 시도 루프 (Mock 응답)

**Files:** `src/storage_sale/slot_worker.py`, `tests/storage_sale/test_slot_worker.py`

- [ ] **Step 1: Mock client 정의 + 실패 테스트**

```python
import asyncio
import pytest
from src.storage_sale.models import SlotRequest, SlotResult


class MockKreamClient:
    """응답 시퀀스를 미리 큐로 받아 차례로 반환."""
    def __init__(self, responses: list[dict | int]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post_slot_request(self, payload: dict) -> tuple[int, dict]:
        self.calls.append(payload)
        r = self.responses.pop(0)
        if isinstance(r, int):  # HTTP status 만 (예: 429)
            return r, {}
        return 200, r


@pytest.mark.asyncio
async def test_slot_worker_first_try_success():
    from src.storage_sale.slot_worker import run_slot_worker
    client = MockKreamClient([{"id": "review-001"}])
    req = SlotRequest(product_id="123", product_option_key="265", interval_seconds=0.01)
    result = await run_slot_worker(req, client=client)
    assert result.success
    assert result.review_id == "review-001"
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_slot_worker_retry_on_9999():
    from src.storage_sale.slot_worker import run_slot_worker
    client = MockKreamClient([
        {"code": 9999, "message": "창고 가득"},
        {"code": 9999},
        {"id": "review-002"},
    ])
    req = SlotRequest(product_id="123", product_option_key="265", interval_seconds=0.01)
    result = await run_slot_worker(req, client=client)
    assert result.success
    assert result.review_id == "review-002"
    assert result.attempts == 3


@pytest.mark.asyncio
async def test_slot_worker_ban_on_429():
    from src.storage_sale.slot_worker import run_slot_worker
    client = MockKreamClient([429])
    req = SlotRequest(product_id="123", product_option_key="265", interval_seconds=0.01)
    result = await run_slot_worker(req, client=client)
    assert not result.success
    assert result.error_code == "BAN"


@pytest.mark.asyncio
async def test_slot_worker_timeout():
    from src.storage_sale.slot_worker import run_slot_worker
    client = MockKreamClient([{"code": 9999}] * 100)
    req = SlotRequest(
        product_id="123", product_option_key="265",
        interval_seconds=0.01, max_duration_minutes=0,  # 즉시 timeout
    )
    result = await run_slot_worker(req, client=client)
    assert not result.success
    assert result.error_code == "TIMEOUT"


@pytest.mark.asyncio
async def test_slot_worker_user_stop():
    from src.storage_sale.slot_worker import run_slot_worker
    client = MockKreamClient([{"code": 9999}] * 100)
    stop_event = asyncio.Event()
    req = SlotRequest(product_id="123", product_option_key="265", interval_seconds=0.05)

    async def trigger_stop():
        await asyncio.sleep(0.15)
        stop_event.set()

    asyncio.create_task(trigger_stop())
    result = await run_slot_worker(req, client=client, stop_event=stop_event)
    assert not result.success
    assert result.error_code == "USER_STOP"
```

- [ ] **Step 2: SlotWorker 구현**

```python
# src/storage_sale/slot_worker.py
import asyncio
import time
from src.storage_sale.models import SlotRequest, SlotResult


async def run_slot_worker(
    req: SlotRequest,
    *,
    client,  # MockKreamClient or RealKreamClient
    stop_event: asyncio.Event | None = None,
) -> SlotResult:
    started = time.monotonic()
    deadline = started + req.max_duration_minutes * 60
    attempts = 0
    payload = {
        "items": [
            {"product_option": {"key": req.product_option_key}, "quantity": req.quantity}
        ]
    }

    while True:
        if stop_event and stop_event.is_set():
            return SlotResult(
                success=False, error_code="USER_STOP", attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )
        if time.monotonic() > deadline:
            return SlotResult(
                success=False, error_code="TIMEOUT", attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )

        attempts += 1
        try:
            status, body = await client.post_slot_request(payload)
        except Exception as e:
            return SlotResult(
                success=False, error_code=f"CLIENT_ERROR: {e}", attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )

        if status == 429:
            return SlotResult(
                success=False, error_code="BAN", attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )
        if status in (401, 403):
            return SlotResult(
                success=False, error_code="AUTH_FAIL", attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )
        if status >= 500:
            await asyncio.sleep(30)
            continue
        if status == 200:
            if body.get("id"):
                return SlotResult(
                    success=True, review_id=body["id"], attempts=attempts,
                    elapsed_seconds=time.monotonic() - started,
                )
            if body.get("code") == 9999:
                await asyncio.sleep(req.interval_seconds)
                continue

        return SlotResult(
            success=False, error_code=f"UNKNOWN: status={status}", attempts=attempts,
            elapsed_seconds=time.monotonic() - started,
        )
```

- [ ] **Step 3: 테스트 통과 + 커밋**

```bash
~/kream-venv/bin/python -m pytest tests/storage_sale/test_slot_worker.py -v
git commit -m "feat(storage_sale): SlotWorker 시도 루프 + 안전장치 5종 (Phase B-0)"
```

---

## Task 4: ConfirmWorker — 단일 시도

**Files:** `src/storage_sale/confirm_worker.py`, `tests/storage_sale/test_confirm_worker.py`

- [ ] **Step 1: 실패 테스트**

```python
@pytest.mark.asyncio
async def test_confirm_first_try_success():
    from src.storage_sale.confirm_worker import run_confirm_worker
    from src.storage_sale.models import ConfirmRequest

    class MockClient:
        async def post_confirm(self, payload):
            return 200, {"status": "registered"}

    req = ConfirmRequest(review_id="r-001", return_address_id=1, delivery_method="courier")
    result = await run_confirm_worker(req, client=MockClient())
    assert result.success


@pytest.mark.asyncio
async def test_confirm_retry_once_on_5xx():
    from src.storage_sale.confirm_worker import run_confirm_worker
    from src.storage_sale.models import ConfirmRequest

    class MockClient:
        def __init__(self):
            self.calls = 0
        async def post_confirm(self, payload):
            self.calls += 1
            return (500, {}) if self.calls == 1 else (200, {"status": "ok"})

    req = ConfirmRequest(review_id="r-001", return_address_id=1, delivery_method="courier")
    client = MockClient()
    result = await run_confirm_worker(req, client=client)
    assert result.success
    assert client.calls == 2


@pytest.mark.asyncio
async def test_confirm_fail_after_retry():
    from src.storage_sale.confirm_worker import run_confirm_worker
    from src.storage_sale.models import ConfirmRequest

    class MockClient:
        async def post_confirm(self, payload):
            return 500, {}

    req = ConfirmRequest(review_id="r-001", return_address_id=1, delivery_method="courier")
    result = await run_confirm_worker(req, client=MockClient())
    assert not result.success
```

- [ ] **Step 2: 구현**

```python
# src/storage_sale/confirm_worker.py
import asyncio
from src.storage_sale.models import ConfirmRequest, ConfirmResult


async def run_confirm_worker(req: ConfirmRequest, *, client) -> ConfirmResult:
    payload = {
        "review_id": req.review_id,
        "return_address_id": req.return_address_id,
        "return_shipping_memo": req.return_shipping_memo,
        "delivery_method": req.delivery_method,
    }
    for attempt in range(2):  # 최초 + 1 재시도
        try:
            status, body = await client.post_confirm(payload)
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(2)
                continue
            return ConfirmResult(success=False, error_message=str(e))

        if status == 200:
            return ConfirmResult(success=True, response_data=body)
        if status >= 500 and attempt == 0:
            await asyncio.sleep(2)
            continue
        return ConfirmResult(
            success=False,
            error_message=f"status={status} body={body}",
        )

    return ConfirmResult(success=False, error_message="unreachable")
```

- [ ] **Step 3: 통과 + 커밋**

```bash
~/kream-venv/bin/python -m pytest tests/storage_sale/test_confirm_worker.py -v
git commit -m "feat(storage_sale): ConfirmWorker 단일 시도 + 1 재시도 (Phase B-0)"
```

---

## Task 5: Runner — B1 → B3 파이프라인

**Files:** `src/storage_sale/runner.py`, `tests/storage_sale/test_runner.py`

- [ ] **Step 1: 실패 테스트**

```python
@pytest.mark.asyncio
async def test_runner_b1_success_triggers_b3():
    from src.storage_sale.runner import run_pipeline
    from src.storage_sale.models import SlotRequest

    class MockSlotClient:
        async def post_slot_request(self, payload):
            return 200, {"id": "rv-1"}

    class MockConfirmClient:
        async def post_confirm(self, payload):
            return 200, {"status": "registered"}

    req = SlotRequest(product_id="123", product_option_key="265", interval_seconds=0.01)
    confirm_defaults = {"return_address_id": 1, "delivery_method": "courier"}
    slot_result, confirm_result = await run_pipeline(
        req, confirm_defaults,
        slot_client=MockSlotClient(), confirm_client=MockConfirmClient(),
    )
    assert slot_result.success
    assert confirm_result.success


@pytest.mark.asyncio
async def test_runner_b1_fail_skips_b3():
    from src.storage_sale.runner import run_pipeline
    from src.storage_sale.models import SlotRequest

    class MockSlotClient:
        async def post_slot_request(self, payload):
            return 429, {}

    req = SlotRequest(product_id="123", product_option_key="265", interval_seconds=0.01)
    slot_result, confirm_result = await run_pipeline(
        req, {"return_address_id": 1, "delivery_method": "courier"},
        slot_client=MockSlotClient(), confirm_client=None,
    )
    assert not slot_result.success
    assert slot_result.error_code == "BAN"
    assert confirm_result is None
```

- [ ] **Step 2: 구현**

```python
# src/storage_sale/runner.py
import asyncio
from src.storage_sale.confirm_worker import run_confirm_worker
from src.storage_sale.models import ConfirmRequest, ConfirmResult, SlotRequest, SlotResult
from src.storage_sale.slot_worker import run_slot_worker


async def run_pipeline(
    slot_req: SlotRequest,
    confirm_defaults: dict,  # return_address_id / delivery_method 등
    *,
    slot_client,
    confirm_client,
    stop_event: asyncio.Event | None = None,
) -> tuple[SlotResult, ConfirmResult | None]:
    slot_result = await run_slot_worker(slot_req, client=slot_client, stop_event=stop_event)
    if not slot_result.success:
        return slot_result, None

    confirm_req = ConfirmRequest(
        review_id=slot_result.review_id,
        return_address_id=confirm_defaults["return_address_id"],
        return_shipping_memo=confirm_defaults.get("return_shipping_memo", ""),
        delivery_method=confirm_defaults["delivery_method"],
    )
    confirm_result = await run_confirm_worker(confirm_req, client=confirm_client)
    return slot_result, confirm_result
```

- [ ] **Step 3: 통과 + 커밋**

```bash
~/kream-venv/bin/python -m pytest tests/storage_sale/test_runner.py -v
git commit -m "feat(storage_sale): Runner B1 → B3 파이프라인 (Phase B-0)"
```

---

## Task 6: 전체 검증

- [ ] **Step 1: pytest 전체**

```bash
PYTHONPATH=. ~/kream-venv/bin/python -m pytest tests/ -q --no-header 2>&1 | tail -5
```

Expected: 1060+ PASS (1060 baseline + 신규 ~15)

- [ ] **Step 2: canary**

```bash
PYTHONPATH=. ~/kream-venv/bin/python scripts/run_canary.py 2>&1 | tail -5
```

Expected: PASS 20/20

- [ ] **Step 3: verify**

```bash
PYTHONPATH=. ~/kream-venv/bin/python scripts/verify.py 2>&1 | tail -10
```

Expected: 62+/63

- [ ] **Step 4: 최종 커밋 (필요 시)**

전부 통과 시 추가 commit 불필요. 위 5 task 의 commit 으로 종료.

---

## Self-Review

| 항목 | 매핑 |
|---|---|
| 시도 루프 무한 / interval | Task 3 step 2 (run_slot_worker while + asyncio.sleep) |
| code 9999 재시도 | Task 3 test_slot_worker_retry_on_9999 |
| BAN (HTTP 429) 즉시 중단 | Task 3 test_slot_worker_ban_on_429 |
| 타임아웃 | Task 3 test_slot_worker_timeout |
| 사용자 중지 | Task 3 test_slot_worker_user_stop |
| AUTH_FAIL (401/403) | Task 3 step 2 |
| 5xx backoff 30초 | Task 3 step 2 |
| confirm 단일 시도 + 1 재시도 | Task 4 |
| pipeline B1 → B3 | Task 5 |
| dry-run 격리 (실 POST X) | 모든 테스트 Mock 클라이언트 사용 |

명시 결정:
- 실 HTTP 클라이언트 (RealKreamClient) 는 Stage B-1 진입 시 추가 (본 plan 범위 X)
- UI 연결 = Phase A 머지 후 (본 plan 범위 X)
- CLI runner = Stage B-1 직전 추가 (본 plan 범위 X — dry-run 만으로 단위 검증)

---

## 커밋 5개 예정

1. `feat(storage_sale): 데이터 모델 (Phase B-0)`
2. `feat(storage_sale): MockSession (dry-run, Phase B-0)`
3. `feat(storage_sale): SlotWorker 시도 루프 + 안전장치 5종 (Phase B-0)`
4. `feat(storage_sale): ConfirmWorker 단일 시도 + 1 재시도 (Phase B-0)`
5. `feat(storage_sale): Runner B1 → B3 파이프라인 (Phase B-0)`
