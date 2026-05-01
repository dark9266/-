"""Phase B-0 — B1 → B3 파이프라인 오케스트레이터. 슬롯 워커 → 컨펌 워커 연결."""
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
    """B1 슬롯 잡기 → 성공 시 B3 컨펌 등록. 실패 시 B3 스킵 + None 반환."""
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
