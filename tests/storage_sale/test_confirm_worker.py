import pytest


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
