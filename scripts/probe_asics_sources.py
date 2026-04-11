"""ASICS 소싱처 6곳 라이브 프로브.

크림 인기 ASICS 모델 3개로 실제 API 호출하여
각 소싱처에서 검색 가능 여부를 확인한다.

테스트 모델:
  - 1203A879-021 (Gel-Kayano 14)
  - 1201A256-105 (Gel-Nimbus 9)
  - TH7S2N-100  (Mexico 66)

Usage:
    PYTHONPATH=. python scripts/probe_asics_sources.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx

REPORT_PATH = Path("/tmp/asics_probe_report.json")

TEST_MODELS = [
    ("1203A879-021", "Gel-Kayano 14"),
    ("1201A256-105", "Gel-Nimbus 9"),
    ("TH7S2N-100", "Mexico 66"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def sep(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


# ---------------------------------------------------------------------------
# 1. 무신사
# ---------------------------------------------------------------------------

async def probe_musinsa(client: httpx.AsyncClient) -> dict:
    sep("무신사 (Musinsa)")
    results = {}
    for model, label in TEST_MODELS:
        await asyncio.sleep(2)
        url = "https://api.musinsa.com/api2/dp/v1/plp/goods"
        params = {
            "keyword": model,
            "caller": "SEARCH",
            "page": "1",
            "size": "10",
        }
        try:
            r = await client.get(url, params=params, headers=HEADERS)
            print(f"  [{model}] HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", {}).get("list", [])
                print(f"    results: {len(items)}")
                model_in_name = False
                for item in items[:3]:
                    name = item.get("goodsName", "")
                    print(f"    - {name[:80]}")
                    if model.replace("-", "").upper() in name.replace("-", "").upper():
                        model_in_name = True
                results[model] = {
                    "status": r.status_code,
                    "count": len(items),
                    "model_in_name": model_in_name,
                }
            else:
                results[model] = {"status": r.status_code, "count": 0}
        except Exception as e:
            print(f"    ERROR: {e}")
            results[model] = {"error": str(e)}

    has_results = any(r.get("count", 0) > 0 for r in results.values())
    return {"works": has_results, "details": results}


# ---------------------------------------------------------------------------
# 2. 29CM
# ---------------------------------------------------------------------------

async def probe_29cm(client: httpx.AsyncClient) -> dict:
    sep("29CM")
    results = {}
    for model, label in TEST_MODELS:
        await asyncio.sleep(2.5)
        url = "https://search-api.29cm.co.kr/api/v4/products"
        params = {
            "keyword": model,
            "page": "1",
            "limit": "10",
        }
        try:
            r = await client.get(url, params=params, headers=HEADERS)
            print(f"  [{model}] HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", {}).get("products", [])
                print(f"    results: {len(items)}")
                for item in items[:3]:
                    name = item.get("itemName", "")
                    print(f"    - {name[:80]}")
                results[model] = {"status": r.status_code, "count": len(items)}
            else:
                results[model] = {"status": r.status_code, "count": 0}
        except Exception as e:
            print(f"    ERROR: {e}")
            results[model] = {"error": str(e)}

    has_results = any(r.get("count", 0) > 0 for r in results.values())
    return {"works": has_results, "details": results}


# ---------------------------------------------------------------------------
# 3. ABCMart (a-rt.com grandstage)
# ---------------------------------------------------------------------------

async def probe_abcmart(client: httpx.AsyncClient) -> dict:
    sep("ABCMart / 그랜드스테이지 (a-rt.com)")
    results = {}
    for model, label in TEST_MODELS:
        await asyncio.sleep(2)
        # prefix 검색 (하이픈 앞)
        prefix = model.split("-")[0]
        url = "https://grandstage.a-rt.com/display/search-word/result-total/list"
        params = {
            "searchWord": prefix,
            "channel": "10002",
            "page": "1",
            "perPage": "10",
            "tabGubun": "total",
        }
        try:
            r = await client.get(
                url, params=params,
                headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"},
            )
            print(f"  [{model} → prefix={prefix}] HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                items = data.get("SEARCH", [])
                print(f"    results: {len(items)}")
                asics_items = []
                for item in items[:10]:
                    name = item.get("PRDT_NAME", "")
                    brand = item.get("BRAND_NAME", "")
                    style = item.get("STYLE_INFO", "")
                    color = item.get("COLOR_ID", "")
                    print(f"    - [{brand}] {name[:60]} | style={style} color={color}")
                    if "ASICS" in brand.upper() or "아식스" in name:
                        asics_items.append(item)
                results[model] = {
                    "status": r.status_code,
                    "count": len(items),
                    "asics_count": len(asics_items),
                }
            else:
                results[model] = {"status": r.status_code, "count": 0}
        except Exception as e:
            print(f"    ERROR: {e}")
            results[model] = {"error": str(e)}

    has_results = any(r.get("count", 0) > 0 for r in results.values())
    return {"works": has_results, "details": results}


# ---------------------------------------------------------------------------
# 4. 카시나 (NHN shopby)
# ---------------------------------------------------------------------------

async def probe_kasina(client: httpx.AsyncClient) -> dict:
    sep("카시나 (Kasina)")
    results = {}
    api_base = "https://api.e-kasina.com"
    for model, label in TEST_MODELS:
        await asyncio.sleep(2)
        # productManagementCd 검색
        url = f"{api_base}/products/search"
        params = {
            "hasTotalCount": "true",
            "pageNumber": "1",
            "pageSize": "10",
            "filter.keywords": model,
        }
        try:
            r = await client.get(
                url, params=params,
                headers={**HEADERS, "clientId": "t07i0UjkEe"},
            )
            print(f"  [{model}] HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                items = data.get("items", [])
                print(f"    results: {len(items)}")
                for item in items[:3]:
                    name = item.get("productName", "")
                    mgmt_cd = item.get("productManagementCd", "")
                    print(f"    - {name[:60]} | mgmtCd={mgmt_cd}")
                results[model] = {"status": r.status_code, "count": len(items)}
            else:
                results[model] = {"status": r.status_code, "count": 0}
        except Exception as e:
            print(f"    ERROR: {e}")
            results[model] = {"error": str(e)}

    has_results = any(r.get("count", 0) > 0 for r in results.values())
    return {"works": has_results, "details": results}


# ---------------------------------------------------------------------------
# 5. asics.co.kr — NetFunnel 우회 가능 API 탐색
# ---------------------------------------------------------------------------

async def probe_asics_kr(client: httpx.AsyncClient) -> dict:
    sep("asics.co.kr (공식몰 — NetFunnel 탐색)")

    # curl_cffi로 TLS 우회 시도
    from curl_cffi import requests as cc

    sess = cc.Session(impersonate="chrome131")

    endpoints = [
        # 1) 검색 API 후보들
        {
            "name": "search_api_v1",
            "url": "https://www.asics.co.kr/api/search",
            "params": {"q": "kayano", "page": "1"},
        },
        {
            "name": "search_suggest",
            "url": "https://www.asics.co.kr/api/suggest",
            "params": {"q": "kayano"},
        },
        {
            "name": "search_graphql",
            "url": "https://www.asics.co.kr/graphql",
            "params": {"query": "kayano"},
        },
        # 2) 제품 카탈로그 API
        {
            "name": "catalog_shoes",
            "url": "https://www.asics.co.kr/api/catalog/shoes",
            "params": {},
        },
        {
            "name": "product_list",
            "url": "https://www.asics.co.kr/api/products",
            "params": {"category": "shoes", "page": "1"},
        },
        # 3) SSR 페이지 (NetFunnel 게이트 밖인지 확인)
        {
            "name": "category_page",
            "url": "https://www.asics.co.kr/shoes",
            "params": {},
            "accept": "text/html",
        },
        # 4) 알려진 모델 직접 조회
        {
            "name": "product_direct",
            "url": "https://www.asics.co.kr/1203A879-021.html",
            "params": {},
            "accept": "text/html",
        },
        {
            "name": "product_direct_path",
            "url": "https://www.asics.co.kr/p/1203A879-021",
            "params": {},
            "accept": "text/html",
        },
    ]

    results = {}
    for ep in endpoints:
        time.sleep(2)
        name = ep["name"]
        url = ep["url"]
        accept = ep.get("accept", "application/json")
        try:
            r = sess.get(
                url,
                params=ep["params"],
                headers={
                    "Accept": accept,
                    "Accept-Language": "ko-KR,ko;q=0.9",
                    "Referer": "https://www.asics.co.kr/",
                },
                timeout=15,
            )
            ct = r.headers.get("content-type", "")
            body_preview = r.text[:500] if r.text else ""
            has_netfunnel = "NetFunnel" in body_preview or "netfunnel" in body_preview.lower()
            is_json = "json" in ct

            print(f"  [{name}] HTTP {r.status_code} | ct={ct[:40]} | len={len(r.text)}")
            if has_netfunnel:
                print(f"    ⚠️  NetFunnel detected")
            if is_json:
                try:
                    j = r.json()
                    print(f"    JSON keys: {list(j.keys())[:10] if isinstance(j, dict) else type(j).__name__}")
                except Exception:
                    pass

            results[name] = {
                "status": r.status_code,
                "content_type": ct,
                "body_len": len(r.text),
                "has_netfunnel": has_netfunnel,
                "is_json": is_json,
                "body_preview": body_preview[:200],
            }
        except Exception as e:
            print(f"  [{name}] ERROR: {e}")
            results[name] = {"error": str(e)}

    # 어떤 엔드포인트가 JSON 데이터를 반환하는지
    working_apis = [
        k for k, v in results.items()
        if v.get("is_json") and v.get("status") == 200 and not v.get("has_netfunnel")
    ]

    return {
        "works": len(working_apis) > 0,
        "working_apis": working_apis,
        "details": results,
    }


# ---------------------------------------------------------------------------
# 6. asics.com 글로벌 API
# ---------------------------------------------------------------------------

async def probe_asics_global(client: httpx.AsyncClient) -> dict:
    sep("asics.com 글로벌 API (ko-KR 로케일)")

    endpoints = [
        {
            "name": "global_search",
            "url": "https://www.asics.com/kr/ko-kr/search",
            "params": {"q": "kayano"},
        },
        {
            "name": "global_api_search",
            "url": "https://api.asics.com/product/v1/search",
            "params": {"q": "kayano", "locale": "ko-KR", "page": "1"},
        },
        {
            "name": "global_api_products",
            "url": "https://api.asics.com/product/v1/products",
            "params": {"locale": "ko-KR", "page": "1", "pageSize": "10"},
        },
        {
            "name": "global_graphql",
            "url": "https://www.asics.com/kr/ko-kr/graphql",
            "params": {"query": "{search(keyword:\"kayano\"){products{name}}}"},
        },
        {
            "name": "global_kr_search_page",
            "url": "https://www.asics.com/kr/ko-kr/search?q=1203A879",
            "params": {},
            "accept": "text/html",
        },
    ]

    results = {}
    for ep in endpoints:
        await asyncio.sleep(2)
        name = ep["name"]
        url = ep["url"]
        accept = ep.get("accept", "application/json")
        try:
            r = await client.get(
                url,
                params=ep["params"],
                headers={
                    **HEADERS,
                    "Accept": accept,
                    "Referer": "https://www.asics.com/kr/ko-kr/",
                },
            )
            ct = r.headers.get("content-type", "")
            body_preview = r.text[:500] if r.text else ""
            is_json = "json" in ct

            print(f"  [{name}] HTTP {r.status_code} | ct={ct[:40]} | len={len(r.text)}")
            if is_json:
                try:
                    j = r.json()
                    keys = list(j.keys())[:10] if isinstance(j, dict) else type(j).__name__
                    print(f"    JSON keys: {keys}")
                except Exception:
                    pass

            results[name] = {
                "status": r.status_code,
                "content_type": ct,
                "body_len": len(r.text),
                "is_json": is_json,
                "body_preview": body_preview[:200],
            }
        except Exception as e:
            print(f"  [{name}] ERROR: {e}")
            results[name] = {"error": str(e)}

    working = [
        k for k, v in results.items()
        if v.get("is_json") and v.get("status") == 200
    ]

    return {
        "works": len(working) > 0,
        "working_apis": working,
        "details": results,
    }


# ---------------------------------------------------------------------------
# W컨셉 (기존 등록 소싱처)
# ---------------------------------------------------------------------------

async def probe_wconcept(client: httpx.AsyncClient) -> dict:
    sep("W컨셉 (wconcept)")
    api_base = "https://gw-front.wconcept.co.kr"
    api_key = "VWmkUPgs6g2fviPZ5JQFQ3pERP4tIXv/J2jppLqSRBk="
    results = {}
    for model, label in TEST_MODELS:
        await asyncio.sleep(2)
        payload = json.dumps({
            "keyword": model,
            "gender": "unisex",
            "pageNo": 1,
            "pageSize": 10,
            "sort": "RECOMMEND",
            "searchType": "KEYWORD",
            "platform": "PC",
        })
        try:
            r = await client.post(
                f"{api_base}/display/api/v3/search/result/product",
                content=payload,
                headers={
                    **HEADERS,
                    "DISPLAY-API-KEY": api_key,
                    "Content-Type": "application/json",
                    "Origin": "https://www.wconcept.co.kr",
                    "Referer": "https://www.wconcept.co.kr/",
                },
            )
            print(f"  [{model}] HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", {}).get("product", {}).get("items", [])
                print(f"    results: {len(items)}")
                for item in items[:3]:
                    name = item.get("itemName", "")
                    print(f"    - {name[:80]}")
                results[model] = {"status": r.status_code, "count": len(items)}
            else:
                results[model] = {"status": r.status_code, "count": 0}
        except Exception as e:
            print(f"    ERROR: {e}")
            results[model] = {"error": str(e)}

    has_results = any(r.get("count", 0) > 0 for r in results.values())
    return {"works": has_results, "details": results}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    report = {}

    async with httpx.AsyncClient(
        timeout=15, follow_redirects=True, verify=False,
    ) as client:
        # 기존 소싱처
        report["musinsa"] = await probe_musinsa(client)
        report["29cm"] = await probe_29cm(client)
        report["abcmart"] = await probe_abcmart(client)
        report["kasina"] = await probe_kasina(client)
        report["wconcept"] = await probe_wconcept(client)

        # 신규 후보
        report["asics_kr"] = await probe_asics_kr(client)
        report["asics_global"] = await probe_asics_global(client)

    # 리포트 저장
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # 요약
    sep("SUMMARY")
    for source, data in report.items():
        works = data.get("works", False)
        icon = "✅" if works else "❌"
        extra = ""
        if "working_apis" in data:
            extra = f" apis={data['working_apis']}"
        print(f"  {icon} {source}: works={works}{extra}")

    print(f"\nReport saved to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
