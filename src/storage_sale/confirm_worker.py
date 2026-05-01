"""Phase B-0 — 2페이지 등록 컨펌 워커. 단일 시도 + 1 재시도 (5xx 한정)."""
import asyncio

from src.storage_sale.models import ConfirmRequest, ConfirmResult


async def run_confirm_worker(
    req: ConfirmRequest,
    *,
    client,  # MockClient or RealKreamClient (Stage B-1 에서 추가)
    retry_delay_seconds: float = 2.0,
) -> ConfirmResult:
    """2페이지 등록 컨펌. B3 안전장치 3종:
    - 단일 시도 (무한 재시도 X — review_id 만료 전 1회)
    - 5xx 한정 1 재시도
    - 실패 시 error_message 반환 (사용자 마이페이지 수동 처리 fallback)
    """
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
                await asyncio.sleep(retry_delay_seconds)
                continue
            return ConfirmResult(success=False, error_message=str(e))

        if status == 200:
            return ConfirmResult(success=True, response_data=body)

        if status >= 500 and attempt == 0:
            await asyncio.sleep(retry_delay_seconds)
            continue

        return ConfirmResult(
            success=False,
            error_message=f"status={status} body={body}",
        )

    return ConfirmResult(success=False, error_message="unreachable")
