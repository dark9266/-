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
        product_id="123",
        product_option_key="265",
        interval_seconds=0.01,
        max_duration_minutes=0,  # 즉시 timeout
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
