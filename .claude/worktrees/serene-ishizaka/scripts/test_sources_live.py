"""소싱처별 실전 GET 테스트 (커밋 안함).

각 소싱처의 search_products + get_product_detail 동작 확인.
"""

import asyncio
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def test_source(name, crawler, keyword="dunk low"):
    """소싱처 1개 테스트."""
    print(f"\n{'='*50}")
    print(f"[{name}] 검색: '{keyword}'")
    print(f"{'='*50}")

    # 1. search_products
    t0 = time.monotonic()
    try:
        results = await crawler.search_products(keyword, limit=3)
        elapsed = time.monotonic() - t0
        print(f"  검색 결과: {len(results)}건 ({elapsed:.1f}초)")
        for i, r in enumerate(results[:3]):
            print(f"    [{i+1}] {r.get('name','')[:40]} | 모델: {r.get('model_number','')} | 가격: {r.get('price',0):,}원")
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"  ❌ 검색 실패 ({elapsed:.1f}초): {e}")
        return False

    if not results:
        print(f"  ⚠️ 검색 결과 0건 — 상세 조회 스킵")
        return False

    # 2. get_product_detail
    first = results[0]
    pid = first.get("product_id", "")
    if not pid:
        print(f"  ⚠️ product_id 없음 — 상세 조회 스킵")
        return False

    t0 = time.monotonic()
    try:
        detail = await crawler.get_product_detail(pid)
        elapsed = time.monotonic() - t0
        if detail:
            print(f"  상세 조회: {detail.name[:40]} | 사이즈: {len(detail.sizes)}개 ({elapsed:.1f}초)")
            for s in detail.sizes[:3]:
                print(f"    {s.size}: {s.price:,}원 {'(재고)' if s.in_stock else '(품절)'}")
        else:
            print(f"  ⚠️ 상세 조회 결과 None ({elapsed:.1f}초)")
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"  ❌ 상세 조회 실패 ({elapsed:.1f}초): {e}")
        return False

    # 3. disconnect
    try:
        await crawler.disconnect()
    except Exception:
        pass

    return True


async def main():
    print("=" * 60)
    print("  소싱처 실전 GET 테스트")
    print("=" * 60)

    from src.crawlers.musinsa_httpx import musinsa_crawler
    from src.crawlers.twentynine_cm import twentynine_cm_crawler
    from src.crawlers.nike import nike_crawler
    from src.crawlers.abcmart import abcmart_crawler
    from src.crawlers.adidas import adidas_crawler

    sources = [
        ("무신사", musinsa_crawler, "dunk low"),
        ("29CM", twentynine_cm_crawler, "dunk low"),
        ("나이키", nike_crawler, "dunk low"),
        ("ABC마트", abcmart_crawler, "dunk"),
        ("아디다스", adidas_crawler, "samba"),
    ]

    results = {}
    for name, crawler, kw in sources:
        ok = await test_source(name, crawler, kw)
        results[name] = "✅" if ok else "❌"

    print(f"\n{'='*60}")
    print("  결과 요약")
    print(f"{'='*60}")
    for name, status in results.items():
        print(f"  {status} {name}")


if __name__ == "__main__":
    asyncio.run(main())
