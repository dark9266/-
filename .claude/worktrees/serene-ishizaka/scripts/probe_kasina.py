"""카시나(https://www.kasina.co.kr) API 탐색 스크립트.

발견 사항
---------
* 사이트 엔진: Next.js (pages router, buildId=202604080416) + NHN Commerce shopby
  백엔드. `__NEXT_DATA__`는 껍데기만 SSR, 데이터는 전부 CSR로 shopby API 호출.
* 검색/상세 API 도메인: `https://shop-api-secondary.shopby.co.kr`
  (참고: `shop-api.kasina.co.kr`은 회원/주문 전용, 검색에는 C003 에러 반환)
* 필수 헤더 4종 (쿠키/토큰 불필요 — 완전 비로그인 GET):
    company:  "Kasina/Request"
    clientid: "183SVEgDg5nHbILW//3jvg=="
    platform: "PC"        # PC / MOBILE_WEB / AOS / IOS
    version:  "1.0"
* 검색은 키워드 기반이 아님. 카시나 사이트 자체에 전역 키워드 검색 UI 가
  존재하지 않는다. `/products/search`는 카테고리/브랜드 필터링 + 정렬용.
  - 유효 필터: brandNos, categoryNos, filter.productManagementCd (EXACT ONLY)
  - 정렬: order.by=POPULAR|SALE_CNT|LOW_PRICE|HIGH_PRICE, order.direction=DESC|ASC
  - 페이지: pageNumber (1-base), pageSize (최대 100)
* 크림 모델번호 매칭:
  - 나이키: productManagementCd가 99% 크림 포맷 그대로 (`IB7862-100`, `HF3704-003`).
    → 크림 모델번호로 `filter.productManagementCd=<코드>` 직접 EXACT 조회 가능.
  - 뉴발란스: productManagementCd가 내부 SKU(`NBPDGS169E`)지만
    productName/productNameEn에 크림 포맷 모델이 포함됨 (`뉴발란스 U2000ETC 라이트 블루`).
    → 브랜드 전량(73건) 덤프 후 상품명에서 regex 추출해 역매칭.
  - 아디다스: 취급 (brandNo=40331389). 나이키와 동일 패턴 추정.
  - 아식스: 카시나 취급 브랜드 목록(204개)에 ASICS 없음 — 매칭 대상 아님.
* 상품 상세: GET `/products/{productNo}` → baseInfo.productManagementCd,
  price.salePrice, status.saleStatusType, stock.stockCnt.
* 사이즈별 재고: GET `/products/{productNo}/options` → multiLevelOptions[*].children[*]
  - value: 사이즈 (예 "255","260"..."290")
  - saleType: "AVAILABLE" | "SOLDOUT"
  - stockCnt: 재고수 (-999 = 숨김/무제한)
  - forcedSoldOut: 강제 품절 플래그
  - optionManagementCd: 옵션별 관리코드
  → 크림 스타일 사이즈 매칭에 완벽히 대응.
* 차단/레이트리밋:
  - 순수 httpx(http/1.1) 완벽 작동, 평균 120–160ms 응답.
  - curl_cffi 불필요, TLS impersonation 필요 없음.
  - 6회 연속 burst 모두 200 OK, 서킷/WAF 징후 없음.
  - 권장: max_concurrent=2, min_interval=1.5s (보수적)

샘플 저장 경로
--------------
/tmp/kasina_sample_search.json   — 나이키 브랜드 리스팅 5건
/tmp/kasina_sample_detail.json   — SB Dunk Low Pro 상세 (productNo=133292773)
/tmp/kasina_sample_options.json  — 동일 상품 사이즈별 재고

구현 가능 여부
--------------
CONDITIONAL. 키워드 검색 엔드포인트가 없는 것이 결정적 제약이므로
역방향 스캐너는 다음 두 전략 중 하나를 선택해야 한다.

  (A) EXACT 모델번호 질의 (나이키/아디다스 전용)
      `filter.productManagementCd=<크림모델번호>`로 1회 조회 → 0 또는 1건 반환
      → 나이키/아디다스 크림 상품(크림 DB의 ~60%)에 대해 O(1) 역방향 가능.

  (B) 브랜드 전량 덤프 + 로컬 정규식 매칭 (뉴발란스 등)
      brandNos=<brandNo>로 pageSize=100 페이지네이션 → 전 상품 수집 후
      productName/productNameEn에서 `[A-Z]{1,4}\d{3,5}[A-Z0-9]{0,4}` 추출해
      크림 DB와 대조. 뉴발란스 73건, 주기적 캐시(6h)로 충분.

두 전략 모두 GET-only, 인증 불필요, 안정적. crawler-builder 투입 권장.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

BASE = "https://shop-api-secondary.shopby.co.kr"
HEADERS = {
    "company": "Kasina/Request",
    "clientid": "183SVEgDg5nHbILW//3jvg==",
    "platform": "PC",
    "version": "1.0",
    "Accept": "application/json",
    "Origin": "https://www.kasina.co.kr",
    "Referer": "https://www.kasina.co.kr/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# 카시나 브랜드 No (brands 목록에서 추출)
BRAND_NIKE = 40331479
BRAND_ADIDAS = 40331389
BRAND_NEWBALANCE = 40331363
# ASICS, Salomon 등은 카시나 취급 브랜드 아님.

# 크림 모델번호 정규식 (나이키/아디다스 포맷)
KREAM_MODEL_RE = re.compile(r"^[A-Z]{2,4}\d{4,5}-\d{3}$")
# 상품명 안에 등장하는 뉴발란스/기타 포맷 (U574LGG, ML860XA 등)
NB_INLINE_RE = re.compile(r"[A-Z]{1,4}\d{3,5}[A-Z0-9]{0,4}")


async def search_products(
    client: httpx.AsyncClient,
    *,
    brand_nos: int | None = None,
    category_nos: int | None = None,
    management_cd: str | None = None,
    page_number: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    params: dict[str, str] = {
        "pageNumber": str(page_number),
        "pageSize": str(page_size),
        "order.by": "POPULAR",
        "order.direction": "DESC",
    }
    if brand_nos is not None:
        params["brandNos"] = str(brand_nos)
    if category_nos is not None:
        params["categoryNos"] = str(category_nos)
    if management_cd is not None:
        params["filter.productManagementCd"] = management_cd
    r = await client.get(f"{BASE}/products/search", params=params)
    r.raise_for_status()
    return r.json()


async def get_product_detail(client: httpx.AsyncClient, product_no: int) -> dict[str, Any]:
    r = await client.get(f"{BASE}/products/{product_no}")
    r.raise_for_status()
    return r.json()


async def get_product_options(client: httpx.AsyncClient, product_no: int) -> dict[str, Any]:
    r = await client.get(f"{BASE}/products/{product_no}/options")
    r.raise_for_status()
    return r.json()


def parse_size_stock(options: dict[str, Any]) -> list[dict[str, Any]]:
    """multiLevelOptions → [{size, saleType, stockCnt, optionNo}, ...]"""
    rows: list[dict[str, Any]] = []
    for color_node in options.get("multiLevelOptions") or []:
        color = color_node.get("value")
        for size_node in color_node.get("children") or []:
            rows.append(
                {
                    "color": color,
                    "size": size_node.get("value"),
                    "sale_type": size_node.get("saleType"),
                    "stock_cnt": size_node.get("stockCnt"),
                    "forced_soldout": size_node.get("forcedSoldOut"),
                    "option_no": size_node.get("optionNo"),
                    "option_mgmt_cd": size_node.get("optionManagementCd"),
                    "buy_price": size_node.get("buyPrice"),
                }
            )
    return rows


async def probe() -> None:
    out_dir = Path("/tmp")
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
        print("=" * 60)
        print("[1] EXACT 모델번호 조회 (나이키 포맷)")
        print("=" * 60)
        for mc in ("HF3704-003", "IB7862-100", "DQ8423-100"):
            data = await search_products(client, management_cd=mc, page_size=3)
            hits = data.get("items") or []
            print(f"  {mc:15s} → totalCount={data.get('totalCount')}, items={len(hits)}")
            if hits:
                it = hits[0]
                print(f"    {it['productNo']} | {it['productName']} | ₩{it['salePrice']:,.0f}")
            await asyncio.sleep(1.5)

        print()
        print("=" * 60)
        print("[2] 크림 모델번호 미적중 (뉴발란스/아식스 포맷)")
        print("=" * 60)
        for mc in ("U574LGG", "1011B974-002", "ML860XA"):
            data = await search_products(client, management_cd=mc, page_size=3)
            print(f"  {mc:15s} → totalCount={data.get('totalCount')}")
            await asyncio.sleep(1.5)

        print()
        print("=" * 60)
        print("[3] 브랜드 전량 덤프 — 뉴발란스 → 상품명 정규식 추출")
        print("=" * 60)
        data = await search_products(
            client, brand_nos=BRAND_NEWBALANCE, page_number=1, page_size=100
        )
        items = data.get("items") or []
        print(f"  NEW BALANCE 총 {data.get('totalCount')}건, 첫 페이지 {len(items)}건")
        extracted = 0
        for it in items[:15]:
            name = it.get("productName") or ""
            en = it.get("productNameEn") or ""
            codes = NB_INLINE_RE.findall(en) or NB_INLINE_RE.findall(name)
            if codes:
                extracted += 1
                print(f"    {it['productNo']} | {codes[0]:10s} | {name[:45]}")
        print(f"  → 첫 15건 중 {extracted}건에서 크림 포맷 모델번호 추출 성공")

        print()
        print("=" * 60)
        print("[4] 상세 + 사이즈별 재고")
        print("=" * 60)
        # 샘플: SB Dunk Low Pro
        pn = 133292773
        detail = await get_product_detail(client, pn)
        options = await get_product_options(client, pn)
        base = detail.get("baseInfo") or {}
        print(f"  productNo={pn}")
        print(f"  name={base.get('productName')}")
        print(f"  mgmtCd={base.get('productManagementCd')}")
        print(f"  salePrice=₩{detail.get('price', {}).get('salePrice'):,.0f}")
        print(f"  saleStatus={detail.get('status', {}).get('saleStatusType')}")
        rows = parse_size_stock(options)
        print(f"  options: {len(rows)}개")
        for r in rows:
            flag = "O" if r["sale_type"] == "AVAILABLE" else "X"
            print(
                f"    [{flag}] {r['color']:30s} size={r['size']:>4} "
                f"type={r['sale_type']:9s} stock={r['stock_cnt']}"
            )

        # 샘플 저장
        (out_dir / "kasina_sample_search.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "kasina_sample_detail.json").write_text(
            json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "kasina_sample_options.json").write_text(
            json.dumps(options, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n  샘플 저장: {out_dir}/kasina_sample_*.json")


if __name__ == "__main__":
    asyncio.run(probe())
