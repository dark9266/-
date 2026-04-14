"""Nike KR API endpoint probe script.

Tests various Nike API patterns to find working JSON endpoints
for search and product detail, replacing __NEXT_DATA__ HTML parsing.

GET requests only. 2-second delay between requests.
"""

import asyncio
import json
import re
import time
from urllib.parse import quote

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# Collected style_color for detail testing (will be filled from search)
found_style_color = None
found_product_name = None


def sep(title: str):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")


def dump_keys(d, depth=0, max_depth=2, prefix=""):
    """Print dict keys up to max_depth."""
    if depth > max_depth or not isinstance(d, dict):
        return
    for k, v in list(d.items())[:20]:
        typ = type(v).__name__
        size_info = ""
        if isinstance(v, list):
            size_info = f" (len={len(v)})"
        elif isinstance(v, dict):
            size_info = f" (keys={len(v)})"
        elif isinstance(v, str) and len(v) > 80:
            size_info = f' = "{v[:80]}..."'
        elif isinstance(v, (str, int, float, bool)):
            size_info = f" = {v!r}"
        print(f"  {prefix}{'  '*depth}{k}: {typ}{size_info}")
        if isinstance(v, dict):
            dump_keys(v, depth + 1, max_depth, prefix)


async def probe(client: httpx.AsyncClient, label: str, url: str,
                headers: dict | None = None, expect_json=True) -> dict | str | None:
    """Make GET request, print results, return data."""
    sep(label)
    print(f"  URL: {url[:150]}{'...' if len(url) > 150 else ''}")

    hdrs = {**HEADERS, **(headers or {})}

    try:
        resp = await client.get(url, headers=hdrs, follow_redirects=True)
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

    print(f"  Status: {resp.status_code}")
    ct = resp.headers.get("content-type", "")
    print(f"  Content-Type: {ct}")
    print(f"  Size: {len(resp.content):,} bytes")

    if resp.status_code >= 400:
        print(f"  Body preview: {resp.text[:300]}")
        return None

    if expect_json and "json" in ct:
        try:
            data = resp.json()
            print(f"  JSON parsed OK")
            dump_keys(data)
            return data
        except Exception as e:
            print(f"  JSON parse error: {e}")
            return None

    # If we got HTML, try to extract __NEXT_DATA__
    if "html" in ct:
        text = resp.text
        print(f"  HTML length: {len(text):,} chars")

        m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', text)
        if m:
            try:
                nd = json.loads(m.group(1))
                print(f"  __NEXT_DATA__ found: {len(m.group(1)):,} chars")
                dump_keys(nd)
                return nd
            except json.JSONDecodeError:
                print(f"  __NEXT_DATA__ found but JSON parse failed")
        else:
            print(f"  No __NEXT_DATA__ found in HTML")

        # Look for API endpoint hints
        api_hints = set()
        for pattern in [
            r'(https?://api\.nike\.com/[^\s"\']+)',
            r'(https?://unite\.nike\.com/[^\s"\']+)',
            r'(https?://[a-z]+\.nike\.com/[^\s"\'<>]+(?:v2|v3|feed|browse)[^\s"\'<>]*)',
        ]:
            for match in re.finditer(pattern, text):
                endpoint = match.group(1)[:200]
                api_hints.add(endpoint)

        if api_hints:
            print(f"\n  API endpoints found in HTML/JS ({len(api_hints)}):")
            for ep in sorted(api_hints)[:15]:
                print(f"    - {ep}")

        return text

    # Try JSON anyway
    if expect_json:
        try:
            data = resp.json()
            print(f"  JSON parsed (non-json content-type)")
            dump_keys(data)
            return data
        except Exception:
            pass

    print(f"  Body preview: {resp.text[:300]}")
    return resp.text


async def main():
    global found_style_color, found_product_name

    print("Nike KR API Probe")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    client = httpx.AsyncClient(timeout=20)

    try:
        # ============================================================
        # 1. Current search approach: __NEXT_DATA__ Wall
        # ============================================================
        data = await probe(
            client,
            "TEST 1: Search via HTML __NEXT_DATA__ (current approach)",
            "https://www.nike.com/kr/w?q=dunk+low"
        )

        if isinstance(data, dict):
            # Try to extract products from Wall structure
            wall = (
                data.get("props", {})
                .get("pageProps", {})
                .get("initialState", {})
                .get("Wall", {})
            )
            if wall:
                groupings = wall.get("productGroupings", [])
                print(f"\n  Wall.productGroupings: {len(groupings)} groups")
                if groupings:
                    prods = groupings[0].get("products", [])
                    print(f"  First group products: {len(prods)}")
                    if prods:
                        p = prods[0]
                        code = p.get("productCode", "")
                        copy = p.get("copy", {})
                        prices = p.get("prices", {})
                        print(f"  First product:")
                        print(f"    code: {code}")
                        print(f"    name: {copy.get('title', '?')}")
                        print(f"    price: {prices.get('currentPrice', '?')}")
                        found_style_color = code
                        found_product_name = copy.get("title", "")
            else:
                print("\n  Wall structure NOT found in __NEXT_DATA__")
                # Try alternate paths
                page_props = data.get("props", {}).get("pageProps", {})
                if page_props:
                    print(f"  pageProps keys: {list(page_props.keys())[:20]}")
                    initial_state = page_props.get("initialState", {})
                    if initial_state:
                        print(f"  initialState keys: {list(initial_state.keys())[:20]}")

        await asyncio.sleep(2)

        # ============================================================
        # 2. Nike CIC Browse API v2 (common pattern)
        # ============================================================
        data = await probe(
            client,
            "TEST 2a: api.nike.com/cic/browse/v2 (rollup_threads search)",
            (
                "https://api.nike.com/cic/browse/v2"
                "?queryid=products"
                "&anonymousId="
                "&country=kr"
                "&endpoint=%2Fproduct_feed%2Frollup_threads%2Fv2"
                "%3Ffilter%3Dmarketplace(KR)"
                "%26filter%3Dlanguage(ko)"
                "%26filter%3DsearchTerms(dunk%20low)"
                "%26anchor%3D0%26count%3D30"
                "&language=ko"
                "&localizedRangeStr=%7BlowestPrice%7D+-+%7BhighestPrice%7D"
            ),
            headers={"nike-api-caller-id": "com.nike.commerce.nikedotcom.web"},
        )

        if isinstance(data, dict):
            products = data.get("data", {}).get("products", {}).get("products", [])
            if not products:
                products = data.get("data", {}).get("products", [])
            if products:
                print(f"\n  Products found: {len(products)}")
                p = products[0]
                sc = p.get("styleColor", "") or p.get("productCode", "")
                print(f"  First: {p.get('title', '?')} | {sc} | {p.get('currentPrice', '?')}")
                if sc and not found_style_color:
                    found_style_color = sc
                    found_product_name = p.get("title", "")

        await asyncio.sleep(2)

        # ============================================================
        # 2b: Direct rollup_threads endpoint
        # ============================================================
        data = await probe(
            client,
            "TEST 2b: api.nike.com/product_feed/rollup_threads/v2 (direct)",
            (
                "https://api.nike.com/product_feed/rollup_threads/v2"
                "?filter=marketplace(KR)"
                "&filter=language(ko)"
                "&filter=searchTerms(dunk%20low)"
                "&anchor=0&count=30"
            ),
        )

        if isinstance(data, dict):
            threads = data.get("objects", [])
            if threads:
                print(f"\n  Threads found: {len(threads)}")
                t = threads[0]
                pi = t.get("productInfo", [{}])
                if pi:
                    mp = pi[0].get("merchProduct", {})
                    print(f"  First: {mp.get('labelName', '?')} | "
                          f"{mp.get('styleColor', '?')} | "
                          f"status={mp.get('status', '?')}")
                    sc = mp.get("styleColor", "")
                    if sc and not found_style_color:
                        found_style_color = sc
                        found_product_name = mp.get("labelName", "")

        await asyncio.sleep(2)

        # ============================================================
        # 2c: CIC browse v2 with filteredProductsWithContext queryid
        # ============================================================
        data = await probe(
            client,
            "TEST 2c: api.nike.com/cic/browse/v2 (filteredProductsWithContext)",
            (
                "https://api.nike.com/cic/browse/v2"
                "?queryid=filteredProductsWithContext"
                "&anonymousId="
                "&country=kr"
                "&endpoint=%2Fproduct_feed%2Frollup_threads%2Fv2"
                "%3Ffilter%3Dmarketplace(KR)"
                "%26filter%3Dlanguage(ko)"
                "%26filter%3DsearchTerms(dunk%20low)"
                "%26anchor%3D0%26count%3D30"
                "&language=ko"
                "&localizedRangeStr=%7BlowestPrice%7D+-+%7BhighestPrice%7D"
            ),
            headers={"nike-api-caller-id": "com.nike.commerce.nikedotcom.web"},
        )

        await asyncio.sleep(2)

        # ============================================================
        # 3. Product detail APIs
        # ============================================================
        test_code = found_style_color or "FQ6965-121"
        print(f"\n  >>> Using style_color for detail tests: {test_code}")

        # 3a: product_feed/threads/v3
        data = await probe(
            client,
            f"TEST 3a: api.nike.com/product_feed/threads/v3 (style={test_code})",
            (
                f"https://api.nike.com/product_feed/threads/v3/"
                f"?filter=marketplace(KR)"
                f"&filter=language(ko)"
                f"&filter=channelId(d9a5bc94-4b9c-4976-858a-f159cf99c647)"
                f"&filter=productInfo.merchProduct.styleColor({test_code})"
            ),
        )

        if isinstance(data, dict):
            threads = data.get("objects", [])
            if threads:
                print(f"\n  Thread objects: {len(threads)}")
                t = threads[0]
                pi_list = t.get("productInfo", [])
                print(f"  productInfo items: {len(pi_list)}")
                if pi_list:
                    pi = pi_list[0]
                    mp = pi.get("merchProduct", {})
                    print(f"  merchProduct.styleColor: {mp.get('styleColor', '?')}")
                    print(f"  merchProduct.labelName: {mp.get('labelName', '?')}")
                    print(f"  merchProduct.status: {mp.get('status', '?')}")

                    # Check for SKU/size info
                    skus = pi.get("skus", [])
                    avail_skus = pi.get("availableSkus", [])
                    merch_skus = pi.get("merchSkus", [])
                    print(f"  skus: {len(skus)}, availableSkus: {len(avail_skus)}, merchSkus: {len(merch_skus)}")

                    if skus:
                        s = skus[0]
                        print(f"  First SKU: size={s.get('localizedSize', s.get('nikeSize', '?'))}, "
                              f"id={s.get('id', '?')}")
                    if avail_skus:
                        a = avail_skus[0]
                        print(f"  First available SKU: {a}")

                    # Price
                    price_info = pi.get("merchPrice", {})
                    print(f"  merchPrice: current={price_info.get('currentPrice', '?')}, "
                          f"full={price_info.get('fullPrice', '?')}, "
                          f"currency={price_info.get('currency', '?')}")

        await asyncio.sleep(2)

        # 3b: product_feed/threads/v3 WITHOUT channelId
        data = await probe(
            client,
            f"TEST 3b: threads/v3 without channelId (style={test_code})",
            (
                f"https://api.nike.com/product_feed/threads/v3/"
                f"?filter=marketplace(KR)"
                f"&filter=language(ko)"
                f"&filter=productInfo.merchProduct.styleColor({test_code})"
            ),
        )

        await asyncio.sleep(2)

        # 3c: Current PDP approach (__NEXT_DATA__)
        data = await probe(
            client,
            f"TEST 3c: PDP HTML __NEXT_DATA__ (current approach, style={test_code})",
            f"https://www.nike.com/kr/t/_/{test_code}",
            expect_json=False,
        )

        if isinstance(data, dict):
            product_state = (
                data.get("props", {})
                .get("pageProps", {})
                .get("initialState", {})
                .get("product", {})
            )
            if product_state:
                print(f"\n  product state keys: {list(product_state.keys())[:15]}")
                sel = product_state.get("selectedProduct", {})
                if sel:
                    print(f"  selectedProduct keys: {list(sel.keys())[:15]}")
                    print(f"  title: {sel.get('title', '?')}")
                    print(f"  currentPrice: {sel.get('currentPrice', '?')}")
                    skus = sel.get("skus", [])
                    avail = product_state.get("availableSkus", [])
                    print(f"  skus: {len(skus)}, availableSkus: {len(avail)}")
                    if skus:
                        print(f"  First SKU: {skus[0]}")
            else:
                print("\n  product state NOT found in __NEXT_DATA__")
                pp = data.get("props", {}).get("pageProps", {})
                if pp:
                    print(f"  pageProps keys: {list(pp.keys())[:20]}")
                    ist = pp.get("initialState", {})
                    if ist:
                        print(f"  initialState keys: {list(ist.keys())[:20]}")
        elif isinstance(data, str):
            # HTML without __NEXT_DATA__ - check for React/RSC patterns
            print(f"\n  Checking for alternative data patterns in HTML...")
            # Check for inline JSON data
            json_patterns = [
                (r'window\.__PRELOADED_STATE__\s*=\s*({.*?})\s*;', "__PRELOADED_STATE__"),
                (r'window\.__INITIAL_STATE__\s*=\s*({.*?})\s*;', "__INITIAL_STATE__"),
                (r'"product":\s*\{', "product JSON"),
                (r'"selectedProduct":\s*\{', "selectedProduct JSON"),
                (r'"skus":\s*\[', "skus array"),
            ]
            for pat, name in json_patterns:
                if re.search(pat, data):
                    print(f"  Found: {name}")

        await asyncio.sleep(2)

        # ============================================================
        # 4. Additional API patterns
        # ============================================================

        # 4a: unite.nike.com (auth/token service)
        data = await probe(
            client,
            "TEST 4a: unite.nike.com/getVisitorId",
            "https://unite.nike.com/getVisitorId",
            headers={"Origin": "https://www.nike.com"},
        )

        await asyncio.sleep(2)

        # 4b: Product detail via SKUs endpoint
        data = await probe(
            client,
            f"TEST 4b: api.nike.com/deliver/available_skus/v1 (style={test_code})",
            f"https://api.nike.com/deliver/available_skus/v1?filter=styleColor({test_code})&filter=country(KR)",
        )

        await asyncio.sleep(2)

        # 4c: Product Recommendations / Similar
        data = await probe(
            client,
            f"TEST 4c: api.nike.com/cic/browse/v2 product detail query",
            (
                f"https://api.nike.com/cic/browse/v2"
                f"?queryid=products"
                f"&anonymousId="
                f"&country=kr"
                f"&endpoint=%2Fproduct_feed%2Fthreads%2Fv3"
                f"%3Ffilter%3Dmarketplace(KR)"
                f"%26filter%3Dlanguage(ko)"
                f"%26filter%3DproductInfo.merchProduct.styleColor({test_code})"
                f"&language=ko"
            ),
            headers={"nike-api-caller-id": "com.nike.commerce.nikedotcom.web"},
        )

        await asyncio.sleep(2)

        # 4d: Try graphql endpoint
        data = await probe(
            client,
            "TEST 4d: api.nike.com/cic/graphql (GET, introspection check)",
            "https://api.nike.com/cic/graphql?query={__typename}",
            headers={
                "nike-api-caller-id": "com.nike.commerce.nikedotcom.web",
            },
        )

        await asyncio.sleep(2)

        # 4e: Directly try the SKU availability for the product
        data = await probe(
            client,
            f"TEST 4e: api.nike.com/commerce/skuAvailability/v1 (style={test_code})",
            f"https://api.nike.com/commerce/skuAvailability/v1?styleColors={test_code}&country=KR",
        )

        await asyncio.sleep(2)

        # 4f: Try v2 of product_feed threads
        data = await probe(
            client,
            f"TEST 4f: api.nike.com/product_feed/threads/v2 (style={test_code})",
            (
                f"https://api.nike.com/product_feed/threads/v2/"
                f"?filter=marketplace(KR)"
                f"&filter=language(ko)"
                f"&filter=productInfo.merchProduct.styleColor({test_code})"
            ),
        )

        # ============================================================
        # Summary
        # ============================================================
        sep("SUMMARY")
        print(f"  Test product: {found_product_name} ({found_style_color})")
        print()
        print("  See output above for detailed results per endpoint.")
        print("  Key findings will be evaluated from status codes and data structures.")

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
