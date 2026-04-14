"""Adidas KR API probe - Phase 2.

Deeper investigation based on Phase 1 findings:
- _next/data returned 452KB HTML with __NEXT_DATA__ (Next.js confirmed)
- Need to extract buildId for proper _next/data routes
- /api/products/{model} returned JSON 404 "Product not found" (API exists!)
- Try Akamai WAF bypass patterns
"""

import asyncio
import json
import re
import time

import httpx

BASE = "https://www.adidas.co.kr"
DELAY = 3.0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

JSON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://www.adidas.co.kr/search?q=samba",
    "Origin": "https://www.adidas.co.kr",
}

TEST_QUERY = "samba"
TEST_MODEL = "IE3437"


def truncate(text: str, n: int = 500) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"... [{len(text)} chars total]"


async def probe(client: httpx.AsyncClient, label: str, url: str, headers: dict, extra_info: str = "") -> dict:
    print(f"\n{'='*80}")
    print(f"[{label}]")
    print(f"  URL: {url}")
    if extra_info:
        print(f"  Note: {extra_info}")

    try:
        resp = await client.get(url, headers=headers, follow_redirects=True)
        ct = resp.headers.get("content-type", "?")
        body = resp.text
        print(f"  Status: {resp.status_code} | Content-Type: {ct} | Size: {len(body)}")

        # Try JSON parse
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                keys = list(data.keys())[:20]
                print(f"  JSON keys: {keys}")
                # Show interesting nested keys
                for k in keys[:5]:
                    v = data[k]
                    if isinstance(v, dict):
                        print(f"    {k} -> dict keys: {list(v.keys())[:10]}")
                    elif isinstance(v, list):
                        print(f"    {k} -> list[{len(v)}]")
                    elif isinstance(v, str) and len(v) > 100:
                        print(f"    {k} -> str[{len(v)}]")
                    else:
                        print(f"    {k} -> {repr(v)[:100]}")
            elif isinstance(data, list):
                print(f"  JSON list[{len(data)}]")
                if data and isinstance(data[0], dict):
                    print(f"    First item keys: {list(data[0].keys())[:10]}")
            return {"label": label, "status": resp.status_code, "json": True, "data": data, "size": len(body)}
        except (json.JSONDecodeError, ValueError):
            pass

        # HTML analysis
        if len(body) > 1000:
            # Extract __NEXT_DATA__
            nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.DOTALL)
            if nd:
                try:
                    next_data = json.loads(nd.group(1))
                    build_id = next_data.get("buildId", "?")
                    page = next_data.get("page", "?")
                    props_keys = list(next_data.get("props", {}).get("pageProps", {}).keys())[:10]
                    print(f"  __NEXT_DATA__ found!")
                    print(f"    buildId: {build_id}")
                    print(f"    page: {page}")
                    print(f"    pageProps keys: {props_keys}")
                    return {"label": label, "status": resp.status_code, "next_data": next_data, "build_id": build_id, "size": len(body)}
                except json.JSONDecodeError:
                    print(f"  __NEXT_DATA__ found but failed to parse")

        print(f"  Preview: {truncate(body)}")
        return {"label": label, "status": resp.status_code, "size": len(body)}

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return {"label": label, "status": -1, "error": str(e)}


async def main():
    print("=" * 80)
    print("ADIDAS KR API PROBE - PHASE 2")
    print("=" * 80)

    async with httpx.AsyncClient(timeout=20) as client:

        # ── Step 1: Get the Next.js buildId from a page that loads ──
        # The 404 page returned __NEXT_DATA__, let's extract buildId
        print("\n>>> STEP 1: Extract Next.js buildId")
        r = await probe(client, "1. 404 page for buildId", f"{BASE}/_next/data/search.json", BROWSER_HEADERS,
                        "404 page should contain __NEXT_DATA__ with buildId")
        await asyncio.sleep(DELAY)

        build_id = r.get("build_id", "")
        if build_id and build_id != "?":
            print(f"\n  >>> Got buildId: {build_id}")
        else:
            # Try homepage
            r2 = await probe(client, "1b. Homepage for buildId", f"{BASE}/", BROWSER_HEADERS)
            await asyncio.sleep(DELAY)
            build_id = r2.get("build_id", "")
            if build_id:
                print(f"\n  >>> Got buildId from homepage: {build_id}")

        # ── Step 2: Try _next/data with correct buildId ──
        if build_id and build_id != "?":
            print(f"\n>>> STEP 2: _next/data routes with buildId={build_id}")

            # Search page data route
            r = await probe(client, "2a. _next/data search",
                           f"{BASE}/_next/data/{build_id}/search.json?q={TEST_QUERY}",
                           {**BROWSER_HEADERS, "Accept": "*/*", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                            "Sec-Fetch-Site": "same-origin", "x-nextjs-data": "1"},
                           "Next.js data route for search page")
            await asyncio.sleep(DELAY)

            # Product page data route
            r = await probe(client, "2b. _next/data product",
                           f"{BASE}/_next/data/{build_id}/{TEST_MODEL}.html.json",
                           {**BROWSER_HEADERS, "Accept": "*/*", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                            "Sec-Fetch-Site": "same-origin", "x-nextjs-data": "1"},
                           "Next.js data route for product page")
            await asyncio.sleep(DELAY)

            # Try without .html
            r = await probe(client, "2c. _next/data product (no .html)",
                           f"{BASE}/_next/data/{build_id}/{TEST_MODEL}.json",
                           {**BROWSER_HEADERS, "Accept": "*/*", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                            "Sec-Fetch-Site": "same-origin", "x-nextjs-data": "1"},
                           "Next.js data route - alt path")
            await asyncio.sleep(DELAY)

        # ── Step 3: Try /api/products with different model format ──
        print("\n>>> STEP 3: /api/products with various model ID formats")

        # The previous probe showed /api/products/{model}?sitePath=kr returns JSON (404 = product not found)
        # Try with actual product IDs (not search keywords)
        for model_id in [TEST_MODEL, "IE3437", "GW2871", "HP5586", "IF8065", "IH7755"]:
            r = await probe(client, f"3. /api/products/{model_id}",
                           f"{BASE}/api/products/{model_id}",
                           JSON_HEADERS)
            await asyncio.sleep(DELAY)
            if r.get("status") == 200:
                print(f"  >>> WORKING: /api/products/{model_id}")
                break

        # ── Step 4: Try with Akamai cookie / different header combos ──
        print("\n>>> STEP 4: WAF bypass attempts on /api/products/{id}")

        # Fetch initial page to get cookies
        print("\n  Fetching homepage for cookies...")
        try:
            resp = await client.get(f"{BASE}/", headers=BROWSER_HEADERS, follow_redirects=True)
            cookies = dict(resp.cookies)
            print(f"  Got cookies: {list(cookies.keys())}")
            await asyncio.sleep(DELAY)

            if cookies:
                # Retry /api/products with cookies
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                r = await probe(client, "4a. /api/products with cookies",
                               f"{BASE}/api/products/{TEST_MODEL}",
                               {**JSON_HEADERS, "Cookie": cookie_str})
                await asyncio.sleep(DELAY)

                r = await probe(client, "4b. /api/products/availability with cookies",
                               f"{BASE}/api/products/{TEST_MODEL}/availability",
                               {**JSON_HEADERS, "Cookie": cookie_str})
                await asyncio.sleep(DELAY)
        except Exception as e:
            print(f"  Cookie fetch failed: {e}")

        # ── Step 5: Check glass-plp API (adidas uses this internally) ──
        print("\n>>> STEP 5: glass/plp internal API patterns")

        for path in [
            f"/api/plp/content-engine/search?query={TEST_QUERY}&start=0&count=20",
            f"/plp-app/api/search?query={TEST_QUERY}",
            f"/glass/search?query={TEST_QUERY}",
            f"/api/catalog/search?query={TEST_QUERY}",
        ]:
            r = await probe(client, f"5. {path[:50]}...", f"{BASE}{path}", JSON_HEADERS)
            await asyncio.sleep(DELAY)

        # ── Step 6: Check for sitePath=ko or different locale patterns ──
        print("\n>>> STEP 6: /api/products with sitePath variations")
        for sp in ["ko", "kr", "KR", ""]:
            param = f"?sitePath={sp}" if sp else ""
            r = await probe(client, f"6. /api/products/{TEST_MODEL}{param}",
                           f"{BASE}/api/products/{TEST_MODEL}{param}",
                           JSON_HEADERS)
            await asyncio.sleep(DELAY)
            if r.get("json") and r.get("status") == 200:
                print(f"  >>> FOUND WORKING ENDPOINT with sitePath={sp}")
                # Dump more detail
                data = r.get("data", {})
                print(f"  Full keys: {list(data.keys())}")
                break

    print("\n" + "=" * 80)
    print("PHASE 2 COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
