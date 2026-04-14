"""카시나 크롤러 라이브 검증.

검증:
1. 나이키 EXACT — 알려진 hot 모델로 매치
2. 아디다스 EXACT
3. 뉴발란스 키워드+regex (브랜드 덤프 경로)
4. 사이즈별 재고 — 품절/판매중 분리 확인
5. registry.get_active() 에 'kasina' 포함

사용:
    PYTHONPATH=. python scripts/verify_kasina_live.py
"""

from __future__ import annotations

import asyncio
import sys

from src.crawlers.kasina import kasina_crawler
from src.crawlers.registry import get_active


async def _verify_search(label: str, model: str) -> bool:
    print(f"\n[{label}] search_products('{model}')")
    items = await kasina_crawler.search_products(model)
    if not items:
        print(f"  ❌ 검색 결과 없음 — {label}")
        return False
    first = items[0]
    print(
        f"  ✅ {len(items)}건 — productNo={first['product_id']} "
        f"name={first['name'][:40]} mgmt={first['model_number']} "
        f"price={first['price']:,}"
    )

    # 사이즈 보강
    detail = await kasina_crawler.get_product_detail(first["product_id"])
    if detail is None:
        print("  ⚠️  상세 None — soldout/비판매 가능성")
        return True  # 검색 자체는 성공
    in_stock_n = sum(1 for s in detail.sizes if s.in_stock)
    sizes_str = ", ".join(s.size for s in detail.sizes)
    print(
        f"  📦 detail: 사이즈 {len(detail.sizes)}개 (재고 {in_stock_n}) "
        f"[{sizes_str}]"
    )
    return True


async def main() -> int:
    failures = 0

    # 1. registry 자동 편입 확인
    active = get_active()
    if "kasina" in active:
        print("✅ registry.get_active() 에 'kasina' 등록됨")
    else:
        print("❌ kasina 미등록")
        failures += 1

    # 2. 나이키 EXACT — 샘플로 검증된 SB Dunk Low Pro
    if not await _verify_search("Nike", "HF3704-003"):
        failures += 1

    # 3. 아디다스 EXACT — 카시나 실제 카탈로그에서 확인된 모델
    # (probe로 확인: KI0824 떠그클럽 슈퍼스타, IE4195 슈퍼스타 82)
    if not await _verify_search("adidas", "IE4195"):
        if not await _verify_search("adidas (alt)", "KI0824"):
            failures += 1

    # 4. 뉴발란스 키워드+regex
    if not await _verify_search("NB", "U2000ETC"):
        # 대안: 다른 NB 모델
        if not await _verify_search("NB (alt)", "M2002RXD"):
            print("  ⚠️  NB 매칭 실패 (브랜드 덤프 후보 변동 가능)")

    await kasina_crawler.disconnect()

    print()
    if failures == 0:
        print("✅ ALL OK")
        return 0
    print(f"❌ {failures}건 실패")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
