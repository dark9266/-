"""ABC마트 API 상세 분석 스크립트 (GET 전용)."""
import asyncio
import json
import time

import httpx

BASE_URL = "https://abcmart.a-rt.com"
SEARCH_URL = BASE_URL + "/display/search-word/result-total/list"
DETAIL_URL = BASE_URL + "/product/info"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

# GrandStage channel
GS_CHANNEL = "10002"


async def search(client: httpx.AsyncClient, keyword: str, label: str) -> dict | None:
    """검색 API 호출."""
    print(f"\n{'='*80}")
    print(f"[검색] {label}: '{keyword}'")
    print(f"{'='*80}")
    try:
        resp = await client.get(SEARCH_URL, params={
            "searchWord": keyword,
            "channel": GS_CHANNEL,
            "page": "1",
            "perPage": "5",
            "tabGubun": "total",
        })
        print(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(f"  응답 본문: {resp.text[:500]}")
            return None
        data = resp.json()

        # 전체 최상위 키 출력
        print(f"  최상위 키: {list(data.keys())}")

        # SEARCH 배열 분석
        products = data.get("SEARCH", [])
        print(f"  SEARCH 배열 길이: {len(products)}")

        # 총 건수 관련 필드
        for k in ["TOTAL_COUNT", "totalCount", "SEARCH_TOTAL_COUNT", "total"]:
            if k in data:
                print(f"  {k}: {data[k]}")

        # 페이징 관련 필드
        paging = data.get("PAGING")
        if paging:
            print(f"  PAGING: {json.dumps(paging, ensure_ascii=False, indent=2)[:300]}")

        if products:
            first = products[0]
            print(f"\n  --- 첫 번째 상품 전체 필드 ---")
            for k, v in first.items():
                val_str = str(v)
                if len(val_str) > 150:
                    val_str = val_str[:150] + "..."
                print(f"    {k}: {val_str}")

            # 모델번호 관련 필드 확인
            print(f"\n  --- 모델번호/품번 관련 필드 ---")
            for k in ["STYLE_INFO", "styleInfo", "PRDT_NO", "prdtNo", "MODEL_NO", "modelNo",
                       "COLOR_ID", "colorId", "SKU", "sku", "STYLE_NO", "styleNo",
                       "GOODS_NO", "goodsNo", "ITEM_CD", "itemCd"]:
                if k in first:
                    print(f"    {k}: {first[k]}")

        return data
    except Exception as e:
        print(f"  에러: {e}")
        return None


async def get_detail(client: httpx.AsyncClient, prdt_no: str, label: str) -> dict | None:
    """상세 API 호출."""
    print(f"\n{'='*80}")
    print(f"[상세] {label}: prdtNo={prdt_no}")
    print(f"{'='*80}")
    try:
        resp = await client.get(DETAIL_URL, params={"prdtNo": prdt_no})
        print(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(f"  응답 본문: {resp.text[:500]}")
            return None
        data = resp.json()

        # 최상위 키
        print(f"  최상위 키: {list(data.keys())}")

        # 기본 정보
        for k in ["prdtName", "prdtNo", "styleInfo", "prdtColorInfo", "colorId",
                   "modelNo", "styleNo", "goodsNo", "itemCd", "brandName"]:
            if k in data:
                print(f"    {k}: {data[k]}")

        # brand 객체
        brand = data.get("brand")
        if brand:
            print(f"  brand 객체: {json.dumps(brand, ensure_ascii=False)[:300]}")

        # 가격
        price = data.get("productPrice")
        if price:
            print(f"  productPrice: {json.dumps(price, ensure_ascii=False)[:500]}")

        # 사이즈/옵션
        options = data.get("productOption", [])
        print(f"  productOption 길이: {len(options) if options else 0}")
        if options and len(options) > 0:
            print(f"\n  --- 첫 번째 옵션 전체 필드 ---")
            for k, v in options[0].items():
                print(f"    {k}: {v}")

            # 2번째도 보자
            if len(options) > 1:
                print(f"\n  --- 두 번째 옵션 ---")
                for k, v in options[1].items():
                    print(f"    {k}: {v}")

        # 이미지
        images = data.get("productImage") or data.get("images") or data.get("prdtImageUrl")
        if images:
            img_str = json.dumps(images, ensure_ascii=False)
            print(f"  이미지 정보: {img_str[:400]}")

        # 나머지 키 중 주요한 것들 출력
        skip_keys = {"prdtName", "prdtNo", "styleInfo", "prdtColorInfo", "brand",
                      "productPrice", "productOption", "productImage"}
        print(f"\n  --- 기타 주요 필드 ---")
        for k, v in data.items():
            if k in skip_keys:
                continue
            val_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            print(f"    {k}: {val_str}")

        return data
    except Exception as e:
        print(f"  에러: {e}")
        return None


async def main():
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True, verify=False) as client:

        # ============================================================
        # PART 1: 검색 API 테스트
        # ============================================================
        print("\n" + "#"*80)
        print("# PART 1: 검색 API 응답 구조 분석")
        print("#"*80)

        searches = [
            ("나이키 샥스", "한글 - 나이키 샥스"),
            ("아디다스 삼바", "한글 - 아디다스 삼바"),
            ("뉴발란스 530", "한글 - 뉴발란스 530"),
            ("AR3565-004", "모델번호 - 나이키 샥스"),
            ("IE3437", "모델번호 - 아디다스 삼바"),
            ("GY1759", "모델번호 - 아디다스 가젤"),
        ]

        search_results = {}
        detail_candidates = []

        for keyword, label in searches:
            data = await search(client, keyword, label)
            if data:
                search_results[keyword] = data
                products = data.get("SEARCH", [])
                if products and len(detail_candidates) < 3:
                    detail_candidates.append((
                        str(products[0].get("PRDT_NO", "")),
                        f"{label} 첫번째 결과"
                    ))
            await asyncio.sleep(2.5)

        # ============================================================
        # PART 2: 상세 API 테스트
        # ============================================================
        print("\n\n" + "#"*80)
        print("# PART 2: 상세 API 응답 구조 분석")
        print("#"*80)

        for prdt_no, label in detail_candidates:
            if prdt_no:
                await get_detail(client, prdt_no, label)
                await asyncio.sleep(2.5)

        # ============================================================
        # PART 3: 크림 hot 상품 검색 테스트
        # ============================================================
        print("\n\n" + "#"*80)
        print("# PART 3: 크림 hot 상품 검색 테스트")
        print("#"*80)

        hot_products = [
            ("AR3565-004", "나이키 샥스 R4", "나이키 샥스 R4"),
            ("HM4740-001", "나이키 에어맥스 95", "나이키 에어맥스 95"),
            ("553558-136", "에어 조던 1 로우", "조던 1 로우"),
            ("DC0774-001", "에어 조던 4", "조던 4"),
            ("IE3437", "아디다스 삼바", "아디다스 삼바"),
            ("GY1759", "아디다스 가젤", "아디다스 가젤"),
        ]

        print(f"\n{'상품':<25} {'모델번호 검색':<15} {'한글명 검색':<15} {'첫결과 상품명':<40} {'가격':<12} {'STYLE_INFO':<15} {'COLOR_ID':<10}")
        print("-" * 140)

        for model_no, name, kr_keyword in hot_products:
            # 모델번호로 검색
            await asyncio.sleep(2.5)
            try:
                resp = await client.get(SEARCH_URL, params={
                    "searchWord": model_no,
                    "channel": GS_CHANNEL,
                    "page": "1",
                    "perPage": "5",
                    "tabGubun": "total",
                })
                model_data = resp.json() if resp.status_code == 200 else {}
                model_count = len(model_data.get("SEARCH", []))
                model_first = model_data.get("SEARCH", [{}])[0] if model_count > 0 else {}
            except Exception:
                model_count = 0
                model_first = {}

            # 한글명으로 검색
            await asyncio.sleep(2.5)
            try:
                resp = await client.get(SEARCH_URL, params={
                    "searchWord": kr_keyword,
                    "channel": GS_CHANNEL,
                    "page": "1",
                    "perPage": "5",
                    "tabGubun": "total",
                })
                kr_data = resp.json() if resp.status_code == 200 else {}
                kr_count = len(kr_data.get("SEARCH", []))
            except Exception:
                kr_count = 0

            first_name = model_first.get("PRDT_NAME", "-")[:38] if model_first else "-"
            first_price = model_first.get("PRDT_DC_PRICE", "-") if model_first else "-"
            first_style = model_first.get("STYLE_INFO", "-") if model_first else "-"
            first_color = model_first.get("COLOR_ID", "-") if model_first else "-"

            print(f"{name:<25} {model_count:<15} {kr_count:<15} {first_name:<40} {str(first_price):<12} {str(first_style):<15} {str(first_color):<10}")

        # ============================================================
        # PART 4: 상세 API로 사이즈 구조 확인 (hot 상품 중 찾은 것)
        # ============================================================
        print("\n\n" + "#"*80)
        print("# PART 4: Hot 상품 상세 API - 사이즈 구조")
        print("#"*80)

        # 모델번호 검색에서 찾은 상품의 상세 확인
        for model_no, name, _ in hot_products[:3]:
            await asyncio.sleep(2.5)
            try:
                resp = await client.get(SEARCH_URL, params={
                    "searchWord": model_no,
                    "channel": GS_CHANNEL,
                    "page": "1",
                    "perPage": "1",
                    "tabGubun": "total",
                })
                if resp.status_code == 200:
                    data = resp.json()
                    products = data.get("SEARCH", [])
                    if products:
                        prdt_no = str(products[0].get("PRDT_NO", ""))
                        if prdt_no:
                            await asyncio.sleep(2.5)
                            await get_detail(client, prdt_no, f"{name} ({model_no})")
            except Exception as e:
                print(f"  {name} 에러: {e}")


if __name__ == "__main__":
    asyncio.run(main())
