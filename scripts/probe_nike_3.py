"""Nike KR probe part 3: Extract the actual sizes/prices from new PDP structure."""

import asyncio
import json
import re

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}


async def main():
    client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    try:
        # Test with two products
        for style in ["IB6651-003", "DD1391-100", "CW1590-100"]:
            print(f"\n{'='*80}")
            print(f"  PDP: {style}")
            print(f"{'='*80}")

            resp = await client.get(
                f"https://www.nike.com/kr/t/_/{style}", headers=HEADERS
            )
            print(f"  Status: {resp.status_code}")
            if resp.status_code != 200:
                print(f"  Redirect/error to: {resp.url}")
                continue

            m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', resp.text)
            if not m:
                print("  No __NEXT_DATA__")
                continue

            nd = json.loads(m.group(1))
            pp = nd.get("props", {}).get("pageProps", {})
            sel = pp.get("selectedProduct", {})

            if not sel:
                print("  No selectedProduct!")
                continue

            # Product info
            pi = sel.get("productInfo", {})
            print(f"\n  productInfo keys: {list(pi.keys())}")
            print(f"  productInfo.title: {pi.get('title', '?')}")
            print(f"  productInfo.subtitle: {pi.get('subtitle', '?')}")

            # Prices
            prices = sel.get("prices", {})
            print(f"\n  prices keys: {list(prices.keys())}")
            for k, v in prices.items():
                print(f"    {k}: {v}")

            # Sizes
            sizes = sel.get("sizes", [])
            print(f"\n  sizes: {len(sizes)} items")
            if sizes:
                print(f"  Size keys: {list(sizes[0].keys())}")
                for s in sizes[:5]:
                    print(f"    label={s.get('label', '?')}, "
                          f"available={s.get('available', '?')}, "
                          f"localizedLabel={s.get('localizedLabel', '?')}, "
                          f"skuId={s.get('skuId', '?')}")

            # statusModifier
            print(f"\n  statusModifier: {sel.get('statusModifier', '?')}")

            # Check for SKUs in productGroups
            groups = pp.get("productGroups", [])
            if groups and isinstance(groups[0], dict):
                g_prods = groups[0].get("products", [])
                print(f"\n  productGroups[0].products: {len(g_prods)} colorways")
                for gp in g_prods[:2]:
                    if isinstance(gp, dict):
                        print(f"    styleColor={gp.get('styleColor', '?')}, "
                              f"sizes={len(gp.get('sizes', []))}, "
                              f"statusModifier={gp.get('statusModifier', '?')}")
                        gp_prices = gp.get("prices", {})
                        if gp_prices:
                            print(f"    prices: {gp_prices}")

            await asyncio.sleep(2)

        # ============================================================
        # Also test threads/v3 with different approach
        # ============================================================
        print(f"\n{'='*80}")
        print(f"  threads/v3 experiments")
        print(f"{'='*80}")

        # Maybe the channelId for KR web is different
        # Try without channelId filter, with ONLY marketplace and styleColor
        for style in ["DD1391-100", "IB6651-003"]:
            print(f"\n  --- threads/v3 channelId=d9a5... style={style} ---")
            resp = await client.get(
                f"https://api.nike.com/product_feed/threads/v3/"
                f"?filter=marketplace(KR)"
                f"&filter=language(ko)"
                f"&filter=channelId(d9a5bc94-4b9c-4976-858a-f159cf99c647)"
                f"&filter=productInfo.merchProduct.styleColor({style})",
                headers={**HEADERS, "nike-api-caller-id": "com.nike.commerce.nikedotcom.web"},
            )
            print(f"  Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"  totalResources: {data.get('pages', {}).get('totalResources', 0)}")
                objs = data.get("objects", [])
                if objs:
                    pi = objs[0].get("productInfo", [{}])[0]
                    mp = pi.get("merchProduct", {})
                    print(f"  status: {mp.get('status')}, channels: {mp.get('channels', [])[:3]}")
            await asyncio.sleep(2)

        # Try US marketplace to see if API works at all, to confirm it's KR-specific
        print(f"\n  --- threads/v3 marketplace=US style=DD1391-100 ---")
        resp = await client.get(
            "https://api.nike.com/product_feed/threads/v3/"
            "?filter=marketplace(US)"
            "&filter=language(en)"
            "&filter=channelId(d9a5bc94-4b9c-4976-858a-f159cf99c647)"
            "&filter=productInfo.merchProduct.styleColor(DD1391-100)",
            headers={**HEADERS, "nike-api-caller-id": "com.nike.commerce.nikedotcom.web"},
        )
        print(f"  Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            total = data.get("pages", {}).get("totalResources", 0)
            print(f"  totalResources: {total}")
            objs = data.get("objects", [])
            if objs:
                pi = objs[0].get("productInfo", [{}])[0]
                mp = pi.get("merchProduct", {})
                skus = pi.get("skus", [])
                price = pi.get("merchPrice", {})
                print(f"  name: {mp.get('labelName')}")
                print(f"  skus: {len(skus)}")
                print(f"  price: {price.get('currentPrice')}/{price.get('fullPrice')}")
                if skus:
                    print(f"  SKU sample: {skus[0]}")

        await asyncio.sleep(2)

        # ============================================================
        # Try KR with nike-api-caller-id and accept header
        # ============================================================
        print(f"\n  --- threads/v3 KR with full nike headers ---")
        resp = await client.get(
            "https://api.nike.com/product_feed/threads/v3/"
            "?filter=marketplace(KR)"
            "&filter=language(ko)"
            "&filter=channelId(d9a5bc94-4b9c-4976-858a-f159cf99c647)"
            "&filter=productInfo.merchProduct.styleColor(DD1391-100)",
            headers={
                **HEADERS,
                "nike-api-caller-id": "com.nike.commerce.nikedotcom.web",
                "Accept": "application/json",
                "Referer": "https://www.nike.com/",
                "Origin": "https://www.nike.com",
            },
        )
        print(f"  Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            total = data.get("pages", {}).get("totalResources", 0)
            print(f"  totalResources: {total}")

        # ============================================================
        # FINAL SUMMARY
        # ============================================================
        print(f"\n{'='*80}")
        print(f"  FINAL SUMMARY")
        print(f"{'='*80}")
        print("""
  SEARCH (__NEXT_DATA__ Wall):
  - WORKS. Path unchanged: props.pageProps.initialState.Wall.productGroupings

  PDP (__NEXT_DATA__):
  - Structure CHANGED. New path:
    props.pageProps.selectedProduct

  New selectedProduct fields:
  - productInfo.title / productInfo.subtitle   (name)
  - prices.currentPrice / prices.fullPrice     (pricing)
  - sizes[]  (array of size objects)
    - label, localizedLabel, available, skuId
  - styleColor                                  (model number)
  - statusModifier                              (BUYABLE_BUY = available)
  - productType                                 (FOOTWEAR, etc)

  Old path (BROKEN):
    props.pageProps.initialState.product.selectedProduct
    (initialState.product no longer exists)

  API endpoints (threads/v3):
  - Returns 200 but empty for KR marketplace
  - May work for US marketplace (different channelId mapping)
  - NOT a viable alternative for KR product detail

  RECOMMENDATION:
  1. Search: NO CHANGE needed (Wall parsing works)
  2. Detail: Update _parse_sizes_from_pdp() to use new path:
     - selectedProduct = pageProps["selectedProduct"]
     - title = selectedProduct["productInfo"]["title"]
     - price = selectedProduct["prices"]["currentPrice"]
     - sizes = selectedProduct["sizes"] (not skus!)
     - Each size: label, available
""")

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
