"""Nike KR probe part 2: Deep-dive into PDP __NEXT_DATA__ new structure
and threads/v3 channelId variations."""

import asyncio
import json
import re
import time

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

STYLE_COLOR = "IB6651-003"


def sep(title):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")


async def main():
    client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    try:
        # ============================================================
        # 1. Deep-dive PDP __NEXT_DATA__ new structure
        # ============================================================
        sep(f"PDP __NEXT_DATA__ deep-dive ({STYLE_COLOR})")
        resp = await client.get(
            f"https://www.nike.com/kr/t/_/{STYLE_COLOR}",
            headers=HEADERS,
        )
        print(f"  Status: {resp.status_code}")

        m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', resp.text)
        nd = json.loads(m.group(1)) if m else {}
        pp = nd.get("props", {}).get("pageProps", {})

        # selectedProduct structure
        sel = pp.get("selectedProduct", {})
        print(f"\n  selectedProduct keys ({len(sel)}):")
        for k in sorted(sel.keys()):
            v = sel[k]
            typ = type(v).__name__
            extra = ""
            if isinstance(v, list):
                extra = f" (len={len(v)})"
            elif isinstance(v, dict):
                extra = f" (keys={len(v)})"
            elif isinstance(v, str) and len(v) > 60:
                extra = f' = "{v[:60]}..."'
            elif isinstance(v, (str, int, float, bool, type(None))):
                extra = f" = {v!r}"
            print(f"    {k}: {typ}{extra}")

        # Key fields
        print(f"\n  --- Key fields ---")
        print(f"  title: {sel.get('title', '?')}")
        print(f"  subtitle: {sel.get('subtitle', '?')}")
        print(f"  styleColor: {sel.get('styleColor', '?')}")
        print(f"  currentPrice: {sel.get('currentPrice', '?')}")
        print(f"  fullPrice: {sel.get('fullPrice', '?')}")
        print(f"  currency: {sel.get('currency', '?')}")
        print(f"  inStock: {sel.get('inStock', '?')}")
        print(f"  isExcluded: {sel.get('isExcluded', '?')}")
        print(f"  productType: {sel.get('productType', '?')}")

        # SKUs
        skus = sel.get("skus", [])
        print(f"\n  skus: {len(skus)} items")
        if skus:
            print(f"  First SKU keys: {list(skus[0].keys())}")
            for s in skus[:3]:
                print(f"    size={s.get('localizedSize', s.get('nikeSize', '?'))}, "
                      f"available={s.get('available', '?')}, "
                      f"id={s.get('skuId', s.get('id', '?'))}")

        # availableSkus
        avail = sel.get("availableSkus", [])
        print(f"\n  availableSkus: {len(avail)} items")
        if avail:
            print(f"  First available SKU: {avail[0]}")

        # colorways
        colorways = pp.get("colorwayImages", [])
        print(f"\n  colorwayImages: {len(colorways)} items")
        if colorways:
            cw = colorways[0]
            print(f"  First colorway keys: {list(cw.keys()) if isinstance(cw, dict) else type(cw).__name__}")

        # productGroups
        groups = pp.get("productGroups", [])
        print(f"\n  productGroups: {len(groups)} items")
        if groups:
            g = groups[0]
            if isinstance(g, dict):
                print(f"  First group keys: {list(g.keys())[:15]}")
                products = g.get("products", [])
                print(f"  Products in group: {len(products)}")
                if products:
                    p = products[0]
                    print(f"  First product keys: {list(p.keys())[:20]}")
                    print(f"  styleColor: {p.get('styleColor', '?')}")
                    p_skus = p.get("skus", [])
                    print(f"  skus: {len(p_skus)}")

        await asyncio.sleep(2)

        # ============================================================
        # 2. threads/v3 with various channelIds
        # ============================================================
        channel_ids = [
            "d9a5bc94-4b9c-4976-858a-f159cf99c647",  # Standard Nike.com web
            "010794e5-35fe-4e32-aaff-cd2c74f89d61",  # Another common one
            "64c8b1e0-10c4-4f44-8736-45f5e3c1c90c",  # SNKRS
        ]

        for cid in channel_ids:
            sep(f"threads/v3 channelId={cid[:8]}...")
            resp = await client.get(
                f"https://api.nike.com/product_feed/threads/v3/"
                f"?filter=marketplace(KR)"
                f"&filter=language(ko)"
                f"&filter=channelId({cid})"
                f"&filter=productInfo.merchProduct.styleColor({STYLE_COLOR})",
                headers=HEADERS,
            )
            print(f"  Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                objs = data.get("objects", [])
                total = data.get("pages", {}).get("totalResources", 0)
                print(f"  totalResources: {total}, objects: {len(objs)}")
                if objs:
                    pi_list = objs[0].get("productInfo", [])
                    print(f"  productInfo: {len(pi_list)} items")
                    if pi_list:
                        mp = pi_list[0].get("merchProduct", {})
                        print(f"  styleColor: {mp.get('styleColor')}")
                        print(f"  status: {mp.get('status')}")
                        skus = pi_list[0].get("skus", [])
                        print(f"  skus: {len(skus)}")
            else:
                print(f"  Body: {resp.text[:200]}")
            await asyncio.sleep(2)

        # ============================================================
        # 3. Try threads/v3 with a well-known globally popular shoe
        # ============================================================
        test_codes = ["DD1391-100", "FQ6965-121", "CW1590-100"]
        for tc in test_codes:
            sep(f"threads/v3 with style={tc}")
            resp = await client.get(
                f"https://api.nike.com/product_feed/threads/v3/"
                f"?filter=marketplace(KR)"
                f"&filter=language(ko)"
                f"&filter=channelId(d9a5bc94-4b9c-4976-858a-f159cf99c647)"
                f"&filter=productInfo.merchProduct.styleColor({tc})",
                headers=HEADERS,
            )
            print(f"  Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                total = data.get("pages", {}).get("totalResources", 0)
                objs = data.get("objects", [])
                print(f"  totalResources: {total}, objects: {len(objs)}")
                if objs:
                    pi = objs[0].get("productInfo", [{}])[0]
                    mp = pi.get("merchProduct", {})
                    print(f"  name: {mp.get('labelName')}")
                    print(f"  status: {mp.get('status')}")
                    price = pi.get("merchPrice", {})
                    print(f"  price: {price.get('currentPrice')} / {price.get('fullPrice')}")
                    skus = pi.get("skus", [])
                    avail_skus = pi.get("availableSkus", [])
                    print(f"  skus: {len(skus)}, availableSkus: {len(avail_skus)}")
                    if skus:
                        print(f"  First SKU: {skus[0]}")
            else:
                print(f"  Body: {resp.text[:200]}")
            await asyncio.sleep(2)

        # ============================================================
        # 4. Try threads/v2 with channel filter
        # ============================================================
        sep(f"threads/v2 with channel filter ({STYLE_COLOR})")
        resp = await client.get(
            f"https://api.nike.com/product_feed/threads/v2/"
            f"?filter=marketplace(KR)"
            f"&filter=language(ko)"
            f"&filter=channelId(d9a5bc94-4b9c-4976-858a-f159cf99c647)"
            f"&filter=productInfo.merchProduct.styleColor({STYLE_COLOR})",
            headers=HEADERS,
        )
        print(f"  Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"  totalResources: {data.get('pages', {}).get('totalResources', 0)}")
            objs = data.get("objects", [])
            if objs:
                pi = objs[0].get("productInfo", [{}])[0]
                skus = pi.get("skus", [])
                print(f"  skus: {len(skus)}")
        else:
            print(f"  Body: {resp.text[:200]}")

        await asyncio.sleep(2)

        # ============================================================
        # 5. Search: rollup_threads with channel filter
        # ============================================================
        sep("rollup_threads/v2 search with channelId")
        resp = await client.get(
            "https://api.nike.com/product_feed/rollup_threads/v2"
            "?filter=marketplace(KR)"
            "&filter=language(ko)"
            "&filter=channelId(d9a5bc94-4b9c-4976-858a-f159cf99c647)"
            "&filter=searchTerms(dunk%20low)"
            "&anchor=0&count=10",
            headers=HEADERS,
        )
        print(f"  Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            objs = data.get("objects", [])
            print(f"  objects: {len(objs)}")
        else:
            print(f"  Body: {resp.text[:200]}")

        await asyncio.sleep(2)

        # ============================================================
        # 6. Try product_feed/search/v2
        # ============================================================
        sep("product_feed/search endpoint variants")
        for path in [
            "/product_feed/search/v2?filter=marketplace(KR)&filter=language(ko)&query=dunk%20low&count=10",
            "/product_feed/products/v2?filter=marketplace(KR)&filter=language(ko)&filter=searchTerms(dunk%20low)",
        ]:
            url = f"https://api.nike.com{path}"
            print(f"\n  Trying: {url[:120]}...")
            resp = await client.get(url, headers=HEADERS)
            print(f"  Status: {resp.status_code}")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    print(f"  Keys: {list(data.keys())[:10]}")
                except Exception:
                    print(f"  Body: {resp.text[:150]}")
            else:
                print(f"  Body: {resp.text[:150]}")
            await asyncio.sleep(2)

        # ============================================================
        # SUMMARY
        # ============================================================
        sep("SUMMARY OF FINDINGS")
        print("""
  SEARCH:
  - __NEXT_DATA__ Wall: WORKS (200 OK, products in Wall.productGroupings)
    Path: props.pageProps.initialState.Wall.productGroupings[].products[]
    Fields: productCode, copy.title, prices.currentPrice

  PDP (Product Detail):
  - __NEXT_DATA__ structure CHANGED:
    OLD: props.pageProps.initialState.product.selectedProduct
    NEW: props.pageProps.selectedProduct (direct, no initialState wrapper)
    Fields: title, styleColor, currentPrice, fullPrice, skus[], availableSkus[]

  API ENDPOINTS:
  - threads/v3 with channelId: Returns 200 but 0 results for tested products
  - threads/v2: Requires channelId (400 without it)
  - rollup_threads/v2: searchTerms filter invalid (400)
  - deliver/available_skus/v1: 403 Access Denied
  - commerce/skuAvailability/v1: 401 Unauthorized
  - cic/browse/v2: 404 (queryids no longer valid?)
  - cic/graphql: 503 DNS failure

  RECOMMENDATION:
  - Search: Keep __NEXT_DATA__ Wall parsing (it works)
  - Detail: Update PDP parser path from initialState.product to pageProps.selectedProduct
  - API fallback for detail: threads/v3 returns empty - may need correct channelId
""")

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
