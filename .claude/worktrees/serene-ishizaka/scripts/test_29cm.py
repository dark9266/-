"""29CM 크롤러 테스트 스크립트."""

import asyncio

from src.crawlers.twentynine_cm import twentynine_cm_crawler


async def main():
    try:
        # 1) 검색 테스트
        print("=" * 60)
        print("[1] 29CM 검색: '나이키 덩크'")
        print("=" * 60)

        results = await twentynine_cm_crawler.search_products("나이키 덩크", limit=10)

        if not results:
            print("  검색 결과 없음. '나이키'로 재시도...")
            results = await twentynine_cm_crawler.search_products("나이키", limit=10)

        for i, r in enumerate(results, 1):
            sold = " [품절]" if r.get("is_sold_out") else ""
            print(
                f"  {i:>2}. {r['name'][:50]}\n"
                f"      브랜드: {r['brand']} | 모델번호: {r['model_number'] or '(없음)'}\n"
                f"      가격: {r['price']:,}원 (정가 {r['original_price']:,}원){sold}\n"
                f"      URL: {r['url']}"
            )

        # 2) 상세 조회 테스트 (품절 아닌 첫 번째 상품)
        target = None
        for r in results:
            if not r.get("is_sold_out"):
                target = r
                break

        if target:
            print(f"\n{'=' * 60}")
            print(f"[2] 상세 조회: {target['name'][:50]}")
            print(f"{'=' * 60}")

            product = await twentynine_cm_crawler.get_product_detail(target["product_id"])
            if product:
                print(f"  상품명: {product.name}")
                print(f"  모델번호: {product.model_number}")
                print(f"  브랜드: {product.brand}")
                print(f"  URL: {product.url}")
                print(f"  이미지: {product.image_url[:80]}..." if product.image_url else "  이미지: 없음")
                print(f"\n  재고 있는 사이즈 ({len(product.sizes)}개):")
                for s in product.sizes:
                    disc = f" ({s.discount_type} {s.discount_rate:.0%})" if s.discount_type else ""
                    print(f"    - {s.size:>5} | {s.price:>10,}원{disc}")
            else:
                print("  상세 조회 실패")
        else:
            print("\n  품절 아닌 상품이 없어 상세 조회 스킵")

        print(f"\n{'=' * 60}")
        print("테스트 완료!")
        print(f"{'=' * 60}")

    finally:
        await twentynine_cm_crawler.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
