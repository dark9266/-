"""Phase B-0 — 1페이지 슬롯 시도 루프. dry-run + 실 모드 공용 (client 교체로 전환)."""
import asyncio
import time

from src.storage_sale.models import SlotRequest, SlotResult


async def run_slot_worker(
    req: SlotRequest,
    *,
    client,  # MockKreamClient or RealKreamClient (Stage B-1 에서 추가)
    stop_event: asyncio.Event | None = None,
) -> SlotResult:
    """슬롯 잡힐 때까지 반복 POST. 종료 조건: 성공 / BAN / AUTH_FAIL / TIMEOUT / USER_STOP."""
    started = time.monotonic()
    deadline = started + req.max_duration_minutes * 60
    attempts = 0
    payload = {
        "items": [
            {"product_option": {"key": req.product_option_key}, "quantity": req.quantity}
        ]
    }

    while True:
        # 사용자 중지 체크
        if stop_event and stop_event.is_set():
            return SlotResult(
                success=False,
                error_code="USER_STOP",
                attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )

        # 타임아웃 체크
        if time.monotonic() >= deadline:
            return SlotResult(
                success=False,
                error_code="TIMEOUT",
                attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )

        attempts += 1
        try:
            status, body = await client.post_slot_request(payload)
        except Exception as e:
            return SlotResult(
                success=False,
                error_code=f"CLIENT_ERROR: {e}",
                attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )

        if status == 429:
            return SlotResult(
                success=False,
                error_code="BAN",
                attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )

        if status in (401, 403):
            return SlotResult(
                success=False,
                error_code="AUTH_FAIL",
                attempts=attempts,
                elapsed_seconds=time.monotonic() - started,
            )

        if status >= 500:
            # 서버 장애 — 30초 backoff 후 재시도 (안전장치)
            await asyncio.sleep(30)
            continue

        if status == 200:
            if body.get("id"):
                # 슬롯 확보 성공
                return SlotResult(
                    success=True,
                    review_id=body["id"],
                    attempts=attempts,
                    elapsed_seconds=time.monotonic() - started,
                )
            if body.get("code") == 9999:
                # 창고 가득 — interval 후 재시도
                await asyncio.sleep(req.interval_seconds)
                continue

        # 예상 외 응답
        return SlotResult(
            success=False,
            error_code=f"UNKNOWN: status={status} body={body}",
            attempts=attempts,
            elapsed_seconds=time.monotonic() - started,
        )
