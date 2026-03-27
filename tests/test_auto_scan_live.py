"""자동스캔 실전 테스트.

크림 인기상품 5개 수집 → 상세 조회 → 무신사 매칭까지 확인.
실제 네트워크 요청을 사용하므로 수동 실행 전용.
"""

import asyncio
import sys
from pathlib import Path

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crawlers.kream import kream_crawler
from src.crawlers.musinsa import musinsa_crawler
from src.matcher import model_numbers_match, normalize_model_number
from src.profit_calculator import _normalize_size, analyze_auto_scan_opportunity


async def test_live_auto_scan():
    """크림 인기상품 5개 → 무신사 매칭 실전 테스트."""
    print("=" * 60)
    print("자동스캔 실전 테스트 시작")
    print("=" * 60)

    # 1단계: 크림 인기상품 수집
    print("\n[1단계] 크림 인기상품 수집 (인기순 5개)...")
    try:
        popular = await kream_crawler.get_popular_products(
            category="sneakers", sort="popular", limit=5,
        )
        print(f"  → 수집 결과: {len(popular)}건")
        for i, p in enumerate(popular, 1):
            print(f"  {i}. [{p['product_id']}] {p['name']} | {p.get('brand', '?')}")
            if p.get("model_number"):
                print(f"     모델번호: {p['model_number']}")
    except Exception as e:
        print(f"  ❌ 수집 실패: {e}")
        # 폴백: 급상승 상품 시도
        print("\n  급상승 상품으로 재시도...")
        try:
            popular = await kream_crawler.get_trending_products(limit=5)
            print(f"  → 급상승 결과: {len(popular)}건")
            for i, p in enumerate(popular, 1):
                print(f"  {i}. [{p['product_id']}] {p['name']}")
        except Exception as e2:
            print(f"  ❌ 급상승도 실패: {e2}")
            popular = []

    if not popular:
        print("\n❌ 크림에서 상품을 수집하지 못했습니다.")
        return

    # 2단계: 각 상품 상세 조회
    print("\n[2단계] 크림 상품 상세 조회...")
    kream_products = []
    for item in popular[:5]:
        pid = item["product_id"]
        print(f"\n  조회 중: {pid} ({item.get('name', '?')[:30]}...)")
        try:
            product = await kream_crawler.get_full_product_info(pid)
            if product:
                print(f"  ✅ {product.name}")
                print(f"     모델번호: {product.model_number}")
                print(f"     브랜드: {product.brand}")
                print(f"     사이즈: {len(product.size_prices)}개")
                print(f"     7일 거래량: {product.volume_7d}")
                print(f"     가격 추세: {product.price_trend or 'N/A'}")

                if product.size_prices:
                    # 일부 사이즈 가격 출력
                    for sp in product.size_prices[:3]:
                        bid = f"{sp.sell_now_price:,}" if sp.sell_now_price else "N/A"
                        ask = f"{sp.buy_now_price:,}" if sp.buy_now_price else "N/A"
                        last = f"{sp.last_sale_price:,}" if sp.last_sale_price else "N/A"
                        print(f"     [{sp.size}] bid={bid} / ask={ask} / 최근={last}")

                kream_products.append(product)
            else:
                print(f"  ⚠️ 상세 정보 없음")
        except Exception as e:
            print(f"  ❌ 조회 실패: {e}")

    if not kream_products:
        print("\n❌ 크림 상품 상세 조회 실패.")
        return

    # 3단계: 무신사 매칭
    print("\n[3단계] 무신사 매칭 검색...")
    matched_count = 0
    opportunities = []

    for kp in kream_products:
        model = normalize_model_number(kp.model_number)
        if not model:
            print(f"\n  ⚠️ 모델번호 없음: {kp.name}")
            continue

        print(f"\n  검색: {model} ({kp.name[:30]}...)")

        try:
            # 무신사 검색
            results = await musinsa_crawler.search_products(model)
            print(f"  → 검색 결과: {len(results)}건")

            if not results:
                # 변형 검색
                import re
                alt = re.sub(r"[-]", " ", model)
                if alt != model:
                    results = await musinsa_crawler.search_products(alt)
                    print(f"  → 변형 검색 ({alt}): {len(results)}건")

            if not results:
                print(f"  ⚠️ 무신사 매칭 실패")
                continue

            # 상세 조회로 정확 매칭 확인
            for item in results[:3]:
                detail = await musinsa_crawler.get_product_detail(item["product_id"])
                if not detail:
                    continue

                if detail.model_number and model_numbers_match(detail.model_number, model):
                    matched_count += 1
                    print(f"  ✅ 매칭 성공! {detail.name}")
                    print(f"     모델번호: {detail.model_number}")
                    print(f"     URL: {detail.url}")

                    # 사이즈별 가격
                    in_stock = [s for s in detail.sizes if s.in_stock]
                    print(f"     재고 사이즈: {len(in_stock)}/{len(detail.sizes)}개")

                    if in_stock:
                        for s in in_stock[:3]:
                            orig = f" (정가 {s.original_price:,})" if s.original_price else ""
                            print(f"     [{s.size}] {s.price:,}원{orig}")

                    # 수익 분석
                    sizes_map = {}
                    for s in detail.sizes:
                        if s.in_stock and s.price > 0:
                            norm = _normalize_size(s.size)
                            if norm not in sizes_map or s.price < sizes_map[norm][0]:
                                sizes_map[norm] = (s.price, s.in_stock)

                    if sizes_map:
                        opp = analyze_auto_scan_opportunity(
                            kream_product=kp,
                            musinsa_sizes=sizes_map,
                            musinsa_url=detail.url,
                            musinsa_name=detail.name,
                            musinsa_product_id=detail.product_id,
                        )
                        if opp:
                            opportunities.append(opp)
                            print(f"\n  📊 수익 분석:")
                            print(f"     [확정] 최고 순수익: {opp.best_confirmed_profit:+,}원 (ROI {opp.best_confirmed_roi}%)")
                            print(f"     [예상] 최고 순수익: {opp.best_estimated_profit:+,}원 (ROI {opp.best_estimated_roi}%)")
                            print(f"     시세안정성: {opp.bid_ask_spread:,}원 차이")

                            # 사이즈별 상세
                            for sp in opp.size_profits[:3]:
                                conf = f"{sp.confirmed_profit:+,}" if sp.confirmed_profit else "N/A"
                                est = f"{sp.estimated_profit:+,}" if sp.estimated_profit else "N/A"
                                print(f"     [{sp.size}] 무신사 {sp.musinsa_price:,} → 확정 {conf} / 예상 {est}")
                    break
                else:
                    print(f"  ⚠️ 모델번호 불일치: {detail.model_number} ≠ {model}")

        except Exception as e:
            print(f"  ❌ 매칭 오류: {e}")

    # 결과 요약
    print("\n" + "=" * 60)
    print("실전 테스트 결과 요약")
    print("=" * 60)
    print(f"크림 수집: {len(popular)}건")
    print(f"상세 조회: {len(kream_products)}건")
    print(f"무신사 매칭: {matched_count}건")
    print(f"수익 기회: {len(opportunities)}건")

    if opportunities:
        print("\n수익 기회 목록:")
        for i, opp in enumerate(sorted(opportunities, key=lambda o: -o.best_confirmed_profit), 1):
            kp = opp.kream_product
            print(
                f"  {i}. {kp.name[:40]}\n"
                f"     확정: {opp.best_confirmed_profit:+,}원 (ROI {opp.best_confirmed_roi}%) | "
                f"예상: {opp.best_estimated_profit:+,}원 (ROI {opp.best_estimated_roi}%)\n"
                f"     7일 거래량: {opp.volume_7d} | 추세: {opp.price_trend or 'N/A'}"
            )


if __name__ == "__main__":
    asyncio.run(test_live_auto_scan())
