"""Adidas KR API endpoint probe script.

Tests various endpoints to find working JSON APIs for search and product detail.
GET requests only. 3-second delay between requests.
"""

import asyncio
import json
import time

import httpx

BASE = "https://www.adidas.co.kr"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

JSON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.adidas.co.kr/",
    "Origin": "https://www.adidas.co.kr",
}

MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# Test model: samba OG - IE3437 (popular, likely in stock)
TEST_QUERY = "samba"
TEST_MODEL = "IE3437"

DELAY = 3.0


def truncate(text: str, n: int = 300) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"... [{len(text)} chars total]"


def analyze_json(text: str) -> str:
    """Try to parse as JSON and summarize structure."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            keys = list(data.keys())[:15]
            return f"JSON dict, keys: {keys}"
        elif isinstance(data, list):
            return f"JSON list, {len(data)} items, first keys: {list(data[0].keys())[:10] if data and isinstance(data[0], dict) else '?'}"
        return f"JSON: {type(data).__name__}"
    except (json.JSONDecodeError, ValueError):
        return "Not JSON"


async def probe(client: httpx.AsyncClient, label: str, url: str, headers: dict) -> dict:
    """Send a GET request and report results."""
    print(f"\n{'='*80}")
    print(f"[{label}]")
    print(f"  URL: {url}")
    print(f"  Headers: UA={headers.get('User-Agent', '?')[:40]}...")

    try:
        resp = await client.get(url, headers=headers, follow_redirects=True)
        ct = resp.headers.get("content-type", "unknown")
        body = resp.text

        print(f"  Status: {resp.status_code}")
        print(f"  Content-Type: {ct}")
        print(f"  Response size: {len(body)} chars")

        # Check for JSON
        json_info = analyze_json(body)
        if json_info != "Not JSON":
            print(f"  JSON structure: {json_info}")

        # Check for specific markers in HTML
        if "text/html" in ct:
            markers = {
                "__NEXT_DATA__": "__NEXT_DATA__" in body,
                "__STATE__": "__STATE__" in body,
                "schema.org": "application/ld+json" in body,
                "variation_list": "variation_list" in body,
                "product-card": "product-card" in body,
                "cf-error": "cf-error" in body.lower() or "cloudflare" in body.lower(),
                "captcha": "captcha" in body.lower(),
                "blocked": "blocked" in body.lower() or "access denied" in body.lower(),
            }
            found = [k for k, v in markers.items() if v]
            if found:
                print(f"  HTML markers: {found}")

        print(f"  Preview: {truncate(body)}")

        return {
            "label": label,
            "url": url,
            "status": resp.status_code,
            "content_type": ct,
            "size": len(body),
            "json_info": json_info,
            "works": resp.status_code == 200,
        }

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return {
            "label": label,
            "url": url,
            "status": -1,
            "error": str(e),
            "works": False,
        }


async def main():
    print("=" * 80)
    print("ADIDAS KR API PROBE")
    print(f"Test query: '{TEST_QUERY}', Test model: '{TEST_MODEL}'")
    print("=" * 80)

    results = []

    async with httpx.AsyncClient(timeout=15) as client:

        # ── SECTION 1: Search endpoints ──

        # 1a. Current approach - HTML search page (browser headers)
        r = await probe(client, "1a. HTML Search (browser)", f"{BASE}/search?q={TEST_QUERY}", BROWSER_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 1b. HTML search with mobile UA
        r = await probe(client, "1b. HTML Search (mobile)", f"{BASE}/search?q={TEST_QUERY}", MOBILE_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # ── SECTION 2: Known Adidas API patterns ──

        # 2a. /api/search/product
        r = await probe(client, "2a. /api/search/product", f"{BASE}/api/search/product?query={TEST_QUERY}&start=0", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 2b. /api/plp/content-engine/search
        r = await probe(client, "2b. /api/plp/content-engine/search", f"{BASE}/api/plp/content-engine/search?query={TEST_QUERY}", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 2c. /api/search
        r = await probe(client, "2c. /api/search", f"{BASE}/api/search?query={TEST_QUERY}&count=20&start=0", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 2d. /api/plp/content-engine
        r = await probe(client, "2d. /api/plp/content-engine", f"{BASE}/api/plp/content-engine?query={TEST_QUERY}&start=0", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 2e. /api/products/{query} (search by keyword path)
        r = await probe(client, "2e. /api/products/samba", f"{BASE}/api/products/{TEST_QUERY}?sitePath=kr", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 2f. GraphQL endpoint (some adidas sites use this)
        r = await probe(client, "2f. /api/graphql (search)", f"{BASE}/api/graphql?query=search&variables=%7B%22query%22%3A%22{TEST_QUERY}%22%7D", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 2g. Algolia-style search (adidas sometimes uses Algolia)
        r = await probe(client, "2g. /api/suggestions", f"{BASE}/api/suggestions?query={TEST_QUERY}", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 2h. Typeahead/autocomplete
        r = await probe(client, "2h. /api/typeahead", f"{BASE}/api/typeahead?query={TEST_QUERY}", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 2i. Search with sitePath param
        r = await probe(client, "2i. /api/search sitePath=kr", f"{BASE}/api/search?query={TEST_QUERY}&start=0&count=20&sitePath=kr", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # ── SECTION 3: Product detail endpoints ──

        # 3a. Current approach - HTML product page
        r = await probe(client, "3a. HTML Product page", f"{BASE}/{TEST_MODEL}.html", BROWSER_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 3b. Product page with mobile UA
        r = await probe(client, "3b. Product page (mobile)", f"{BASE}/{TEST_MODEL}.html", MOBILE_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 3c. API product detail
        r = await probe(client, "3c. /api/products/{id}", f"{BASE}/api/products/{TEST_MODEL}", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 3d. Product availability API
        r = await probe(client, "3d. /api/products/{id}/availability", f"{BASE}/api/products/{TEST_MODEL}/availability", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 3e. Product availability with sitePath
        r = await probe(client, "3e. /api/products/{id}/availability?sitePath=kr", f"{BASE}/api/products/{TEST_MODEL}/availability?sitePath=kr", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 3f. Product pricing
        r = await probe(client, "3f. /api/products/{id}/pricing", f"{BASE}/api/products/{TEST_MODEL}/pricing", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 3g. Product reviews (may reveal API structure)
        r = await probe(client, "3g. /api/products/{id}/reviews", f"{BASE}/api/products/{TEST_MODEL}/reviews", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # ── SECTION 4: Alternative domain endpoints ──

        # 4a. Global API with KR locale
        r = await probe(client, "4a. Global API search", "https://www.adidas.com/api/search?query=samba&sitePath=kr&count=5", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 4b. adidas.co.kr Next.js data routes (if Next.js is used)
        r = await probe(client, "4b. _next/data search", f"{BASE}/_next/data/search.json?q={TEST_QUERY}", JSON_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

        # 4c. shop.adidas.co.kr (sometimes separate shop subdomain)
        r = await probe(client, "4c. shop.adidas.co.kr search", f"https://shop.adidas.co.kr/search?q={TEST_QUERY}", BROWSER_HEADERS)
        results.append(r)
        await asyncio.sleep(DELAY)

    # ── SUMMARY ──
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    working = []
    blocked = []
    errors = []

    for r in results:
        status = r.get("status", -1)
        label = r["label"]
        if status == 200:
            json_info = r.get("json_info", "Not JSON")
            is_json = json_info != "Not JSON"
            marker = "JSON" if is_json else "HTML"
            working.append(f"  [OK]  {label} -> {status} ({marker}: {r.get('size', 0)} chars)")
        elif status == 403:
            blocked.append(f"  [403] {label} -> BLOCKED/WAF")
        elif status == -1:
            errors.append(f"  [ERR] {label} -> {r.get('error', 'unknown')}")
        else:
            blocked.append(f"  [{status}] {label}")

    print(f"\nWORKING ({len(working)}):")
    for w in working:
        print(w)

    print(f"\nBLOCKED/FAILED ({len(blocked)}):")
    for b in blocked:
        print(b)

    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for e in errors:
            print(e)

    print("\n" + "=" * 80)
    print("RECOMMENDATION:")
    json_working = [r for r in results if r.get("works") and r.get("json_info", "Not JSON") != "Not JSON"]
    if json_working:
        print("  Found working JSON APIs:")
        for r in json_working:
            print(f"    - {r['label']}: {r['url']}")
            print(f"      Structure: {r['json_info']}")
    else:
        html_working = [r for r in results if r.get("works")]
        if html_working:
            print("  No JSON APIs found. HTML endpoints that work:")
            for r in html_working:
                print(f"    - {r['label']}: {r['url']}")
        else:
            print("  All endpoints blocked. Consider:")
            print("    - Cookie-based session (fetch cookie from initial page load)")
            print("    - Different proxy/IP")
            print("    - Browser automation as last resort")


if __name__ == "__main__":
    asyncio.run(main())
