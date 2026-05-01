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
