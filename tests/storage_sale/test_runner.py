import pytest


@pytest.mark.asyncio
async def test_runner_b1_success_triggers_b3():
    from src.storage_sale.models import SlotRequest
    from src.storage_sale.runner import run_pipeline

    class MockSlotClient:
        async def post_slot_request(self, payload):
            return 200, {"id": "rv-1"}

    class MockConfirmClient:
        async def post_confirm(self, payload):
            return 200, {"status": "registered"}

    req = SlotRequest(product_id="123", product_option_key="265", interval_seconds=0.01)
    confirm_defaults = {"return_address_id": 1, "delivery_method": "courier"}
    slot_result, confirm_result = await run_pipeline(
        req,
        confirm_defaults,
        slot_client=MockSlotClient(),
        confirm_client=MockConfirmClient(),
    )
    assert slot_result.success
    assert confirm_result.success


@pytest.mark.asyncio
async def test_runner_b1_fail_skips_b3():
    from src.storage_sale.models import SlotRequest
    from src.storage_sale.runner import run_pipeline

    class MockSlotClient:
        async def post_slot_request(self, payload):
            return 429, {}

    req = SlotRequest(product_id="123", product_option_key="265", interval_seconds=0.01)
    slot_result, confirm_result = await run_pipeline(
        req,
        {"return_address_id": 1, "delivery_method": "courier"},
        slot_client=MockSlotClient(),
        confirm_client=None,
    )
    assert not slot_result.success
    assert slot_result.error_code == "BAN"
    assert confirm_result is None
