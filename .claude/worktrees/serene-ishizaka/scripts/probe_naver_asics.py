"""Naver Brand Store Asics API probe (curl_cffi edition).

Previous probe was blocked by Akamai/TLS fingerprinting with httpx/curl.
This version uses curl_cffi to impersonate Chrome's JA3/ALPN/HTTP2 settings.

Rules:
  - GET only.
  - 2 second sleep between requests.
  - Start with impersonate='chrome131', fall back to chrome124 / safari17_0.
  - Persist cookies from home page hit into subsequent API calls.
  - Save sample JSON to /tmp/naver_asics_*.json for inspection.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests as cc  # type: ignore

OUT = Path("/tmp")
OUT.mkdir(exist_ok=True)

IMPERSONATE_PROFILES = ["chrome131", "chrome124", "chrome120", "safari17_0"]

BASE_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
}

API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://brand.naver.com/asics",
    "Origin": "https://brand.naver.com",
    "X-Requested-With": "XMLHttpRequest",
}


def sep(title: str) -> None:
    print(f"\n{'=' * 80}\n  {title}\n{'=' * 80}")


def sniff(d: Any, depth: int = 0, max_depth: int = 2, prefix: str = "") -> None:
    if depth > max_depth or not isinstance(d, dict):
        return
    for k, v in list(d.items())[:30]:
        t = type(v).__name__
        extra = ""
        if isinstance(v, list):
            extra = f" [len={len(v)}]"
        elif isinstance(v, dict):
            extra = f" [keys={len(v)}]"
        elif isinstance(v, str):
            extra = f" = {v[:70]!r}"
        elif isinstance(v, (int, float, bool)) or v is None:
            extra = f" = {v!r}"
        print(f"{prefix}{k}: {t}{extra}")
        if isinstance(v, dict):
            sniff(v, depth + 1, max_depth, prefix + "  ")
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            print(f"{prefix}  [0]:")
            sniff(v[0], depth + 1, max_depth, prefix + "    ")


# ---------------------------------------------------------------------------
# Session with impersonation + auto rotation on 403/429
# ---------------------------------------------------------------------------


class ImpersonateSession:
    def __init__(self) -> None:
        self.profile_idx = 0
        self.session = cc.Session(impersonate=self.profile)

    @property
    def profile(self) -> str:
        return IMPERSONATE_PROFILES[self.profile_idx]

    def rotate(self) -> bool:
        if self.profile_idx + 1 >= len(IMPERSONATE_PROFILES):
            return False
        self.profile_idx += 1
        old_cookies = self.session.cookies
        self.session = cc.Session(impersonate=self.profile)
        # preserve cookies
        for c in old_cookies:
            self.session.cookies.set(c.name, c.value, domain=c.domain)
        print(f"  >> rotated impersonate -> {self.profile}")
        return True

    def get(self, url: str, headers: dict | None = None, retries: int = 2):
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.session.get(url, headers=headers or {}, timeout=20)
            except Exception as e:
                print(f"  EXC[{self.profile}] {e}")
                if not self.rotate():
                    raise
                continue
            last = r
            if r.status_code in (403, 429):
                print(f"  blocked status={r.status_code} on {self.profile}")
                if r.status_code == 429:
                    time.sleep(30)
                if not self.rotate():
                    return r
                continue
            return r
        return last


# ---------------------------------------------------------------------------
# Probe steps
# ---------------------------------------------------------------------------


def step1_home(sess: ImpersonateSession) -> tuple[dict | None, str | None]:
    sep("STEP 1: GET https://brand.naver.com/asics (home HTML)")
    r = sess.get("https://brand.naver.com/asics", headers=BASE_HEADERS)
    print(f"profile={sess.profile} status={r.status_code} len={len(r.text)}")
    print(f"content-type={r.headers.get('content-type')}")
    print(f"cookies after home: {[c.name for c in sess.session.cookies]}")

    html = r.text
    (OUT / "naver_asics_home.html").write_text(html, encoding="utf-8")

    payload = None
    patterns = [
        (r'<script id="__PRELOADED_STATE__"[^>]*>([^<]+)</script>', "__PRELOADED_STATE__"),
        (r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});", "window.__PRELOADED_STATE__"),
        (r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', "__NEXT_DATA__"),
        (r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});", "__APOLLO_STATE__"),
    ]
    for pat, name in patterns:
        m = re.search(pat, html, re.DOTALL)
        if m:
            raw = m.group(1)
            print(f"found {name} raw_len={len(raw)}")
            try:
                payload = json.loads(raw)
                print(f"  -> parsed; top keys: {list(payload.keys())[:20]}")
                (OUT / f"naver_asics_{name.strip('_').lower()}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2)[:500_000],
                    encoding="utf-8",
                )
                break
            except Exception as e:
                print(f"  parse fail: {e}")

    # Extract channel number candidates directly from HTML
    channel_no = None
    for pat in (
        r'"channelNo"\s*:\s*"?(\d+)"?',
        r'"channelSiNo"\s*:\s*"?(\d+)"?',
        r'data-channel-no="(\d+)"',
        r'/channels/(\d+)/',
    ):
        m = re.search(pat, html)
        if m:
            channel_no = m.group(1)
            print(f"channel_no from HTML ({pat[:30]}): {channel_no}")
            break

    # API URL hints
    api_hints = set(re.findall(r'/(?:n|i)/v\d/[A-Za-z0-9/_\-{}]+', html))
    print(f"API hints found: {len(api_hints)}")
    for h in sorted(api_hints)[:25]:
        print(f"  {h}")

    return payload, channel_no


def step2_listing(sess: ImpersonateSession, channel_no: str | None) -> list[dict]:
    sep("STEP 2: brand store listing/category API")
    if not channel_no:
        # Try to resolve channel by url path
        for url in (
            "https://brand.naver.com/n/v2/channels/by-url-path/asics",
            "https://brand.naver.com/n/v1/channels/by-url-path/asics",
        ):
            time.sleep(2)
            r = sess.get(url, headers=API_HEADERS)
            print(f"GET {url} -> {r.status_code} len={len(r.text)}")
            if r.status_code == 200:
                try:
                    j = r.json()
                    (OUT / "naver_asics_channel.json").write_text(
                        json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    for k in ("channelNo", "id", "channelId"):
                        if isinstance(j, dict) and k in j:
                            channel_no = str(j[k])
                            print(f"  -> channel_no={channel_no}")
                            break
                    if channel_no:
                        break
                except Exception as e:
                    print(f"  json parse fail: {e}")

    if not channel_no:
        print("!! channel_no unknown — cannot continue listing")
        return []

    list_candidates = [
        # all-products with STDCATG sort
        f"https://brand.naver.com/n/v2/channels/{channel_no}/categories/ALL/products"
        f"?categorySearchType=STDCATG&sortType=POPULAR&page=1&pageSize=20",
        f"https://brand.naver.com/n/v2/channels/{channel_no}/categories/ALL/products"
        f"?categorySearchType=STDCATG&sortType=TOTALSALE&page=1&pageSize=20",
        f"https://brand.naver.com/n/v1/channels/{channel_no}/products?page=1&pageSize=20",
        f"https://brand.naver.com/n/v2/channels/{channel_no}/products?page=1&pageSize=20",
        # search-in-store
        f"https://brand.naver.com/n/v1/channels/{channel_no}/search?keyword=kayano&page=1&pageSize=20",
    ]

    products: list[dict] = []
    for url in list_candidates:
        time.sleep(2)
        r = sess.get(url, headers=API_HEADERS)
        print(f"\nGET {url}\n  -> {r.status_code} len={len(r.text)}")
        if r.status_code != 200:
            print(f"  body head: {r.text[:300]}")
            continue
        try:
            j = r.json()
        except Exception as e:
            print(f"  json parse fail: {e}; head={r.text[:200]}")
            continue
        (OUT / f"naver_asics_list_{len(products)}.json").write_text(
            json.dumps(j, ensure_ascii=False, indent=2)[:500_000], encoding="utf-8"
        )
        print("  top structure:")
        sniff(j, max_depth=2, prefix="    ")

        # extract products list under common keys
        found = None
        if isinstance(j, dict):
            for key in ("simpleProducts", "products", "content", "items", "data"):
                v = j.get(key)
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    found = v
                    print(f"  -> products[] under {key!r} n={len(v)}")
                    break
            if found is None:
                # one-level deeper
                for k, v in j.items():
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            if isinstance(vv, list) and vv and isinstance(vv[0], dict):
                                found = vv
                                print(f"  -> products[] under {k}.{kk} n={len(vv)}")
                                break
                    if found:
                        break
        if found:
            products = found
            print("  first product keys:")
            sniff(products[0], max_depth=2, prefix="    ")
            break

    return products


def step3_detail(
    sess: ImpersonateSession, channel_no: str | None, products: list[dict]
) -> None:
    sep("STEP 3: product detail + options/stock")
    if not products:
        print("skip — no products")
        return

    target = None
    for p in products:
        blob = json.dumps(p, ensure_ascii=False).upper()
        if any(k in blob for k in ("KAYANO", "NIMBUS", "MEXICO")):
            target = p
            print(f"picked target by name hint: {p.get('name') or p.get('productName')}")
            break
    if not target:
        target = products[0]

    product_no = None
    for key in ("productNo", "id", "productId", "channelProductNo"):
        if key in target and target[key]:
            product_no = target[key]
            break
    print(f"target productNo={product_no}")

    if not product_no:
        print("!! no productNo on target")
        return

    # JSON API detail candidates
    detail_urls = []
    if channel_no:
        detail_urls += [
            f"https://brand.naver.com/n/v2/channels/{channel_no}/products/{product_no}",
            f"https://brand.naver.com/n/v1/channels/{channel_no}/products/{product_no}",
            f"https://smartstore.naver.com/i/v1/channels/{channel_no}/products/{product_no}",
        ]
    detail_urls += [
        f"https://brand.naver.com/n/v2/products/{product_no}",
    ]

    api_detail = None
    for url in detail_urls:
        time.sleep(2)
        r = sess.get(url, headers=API_HEADERS)
        print(f"\nGET {url}\n  -> {r.status_code} len={len(r.text)}")
        if r.status_code != 200:
            print(f"  body head: {r.text[:300]}")
            continue
        try:
            api_detail = r.json()
        except Exception as e:
            print(f"  json fail: {e}")
            continue
        (OUT / "naver_asics_detail_api.json").write_text(
            json.dumps(api_detail, ensure_ascii=False, indent=2)[:800_000],
            encoding="utf-8",
        )
        print("  detail top keys:")
        sniff(api_detail, max_depth=3, prefix="    ")
        break

    # HTML page with __PRELOADED_STATE__
    time.sleep(2)
    page_url = f"https://brand.naver.com/asics/products/{product_no}"
    r = sess.get(page_url, headers=BASE_HEADERS)
    print(f"\nGET {page_url} -> {r.status_code} len={len(r.text)}")
    (OUT / "naver_asics_detail.html").write_text(r.text, encoding="utf-8")

    state = None
    m = re.search(
        r'<script id="__PRELOADED_STATE__"[^>]*>([^<]+)</script>', r.text
    )
    if m:
        raw = m.group(1)
        # Naver often HTML-escapes certain chars
        for rep in ("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"):
            raw = raw.replace(*rep)
        try:
            state = json.loads(raw)
            print("  __PRELOADED_STATE__ parsed OK")
            (OUT / "naver_asics_detail_state.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2)[:1_200_000],
                encoding="utf-8",
            )
            print("  state top keys:")
            sniff(state, max_depth=3, prefix="    ")
        except Exception as e:
            print(f"  state parse failed: {e}")

    # Investigate option/stock fields
    sep("STEP 3b: option/stock discovery")
    blobs = []
    if api_detail:
        blobs.append(("api", api_detail))
    if state:
        blobs.append(("state", state))

    KEYS_OF_INTEREST = [
        "optionCombinations",
        "stockQuantity",
        "optionInfo",
        "productOptions",
        "optionUsable",
        "outOfStock",
        "modelName",
        "manufactureName",
        "brandName",
        "catalogId",
        "productInfo",
        "options",
    ]

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}" if path else k
                if k in KEYS_OF_INTEREST:
                    if isinstance(v, (dict, list)):
                        print(f"  FOUND {p} ({type(v).__name__}, "
                              f"{'len=' + str(len(v)) if isinstance(v, list) else 'keys=' + str(len(v))})")
                        if isinstance(v, list) and v:
                            print(f"    sample[0]: {json.dumps(v[0], ensure_ascii=False)[:500]}")
                        elif isinstance(v, dict):
                            print(f"    sample: {json.dumps(v, ensure_ascii=False)[:500]}")
                    else:
                        print(f"  FOUND {p} = {v!r}")
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node[:5]):
                walk(v, f"{path}[{i}]")

    for name, blob in blobs:
        print(f"\n-- scanning {name} --")
        walk(blob)

    # Save the best end-to-end sample
    sample_path = OUT / "naver_asics_sample.json"
    sample = {
        "productNo": product_no,
        "channelNo": channel_no,
        "api_detail": api_detail,
        "preloaded_state": state,
    }
    sample_path.write_text(
        json.dumps(sample, ensure_ascii=False, indent=2)[:2_000_000],
        encoding="utf-8",
    )
    print(f"\nsaved end-to-end sample -> {sample_path}")


def main() -> int:
    sess = ImpersonateSession()
    print(f"starting impersonate profile: {sess.profile}")

    try:
        _payload, channel_no = step1_home(sess)
    except Exception as e:
        print(f"step1 error: {e}")
        return 1

    time.sleep(2)
    try:
        products = step2_listing(sess, channel_no)
    except Exception as e:
        print(f"step2 error: {e}")
        products = []

    time.sleep(2)
    try:
        step3_detail(sess, channel_no, products)
    except Exception as e:
        print(f"step3 error: {e}")

    print("\nDONE. Artifacts in /tmp/naver_asics_*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
