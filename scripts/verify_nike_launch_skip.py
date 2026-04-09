"""HQ4307-001 라이브 검증 — Nike LAUNCH 상품 스킵 동작 확인.

2026-04-08 보고된 버그:
  HQ4307-001 (나이키 마인드 001, consumerReleaseType=LAUNCH) size 240이
  품절 상태인데도 매수 알림 발생.

어제의 수정(threads API 재고 체크)은 라이브에서 동작하지 않았음 (HTTP 400).
오늘의 수정: LAUNCH 상품 자체를 get_product_detail()에서 None 반환.

사용법:
    PYTHONPATH=. python scripts/verify_nike_launch_skip.py
"""

import asyncio
import sys

from src.crawlers.nike import nike_crawler


async def main() -> int:
    # 1. LAUNCH 상품 스킵 확인
    launch_product = await nike_crawler.get_product_detail("HQ4307-001")
    if launch_product is not None:
        print(
            f"FAIL: HQ4307-001 (LAUNCH) 스킵되지 않음 — "
            f"sizes={[s.size for s in launch_product.sizes]}"
        )
        await nike_crawler.disconnect()
        return 1
    print("OK: HQ4307-001 (LAUNCH) 정상 스킵됨")

    # 2. 일반 RELEASE 상품은 정상 동작 확인 (air force 1 검색 상위)
    normal = await nike_crawler.get_product_detail("IR0951-002")
    if normal is None or not normal.sizes:
        print(f"FAIL: IR0951-002 (RELEASE) 조회 실패 — {normal}")
        await nike_crawler.disconnect()
        return 1
    print(
        f"OK: IR0951-002 (RELEASE) 정상 동작 — "
        f"{normal.name}, 사이즈 {len(normal.sizes)}개"
    )

    await nike_crawler.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
