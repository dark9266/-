#!/usr/bin/env python3
"""ABC마트 (a-rt.com) API 탐색 스크립트.

GET 요청만 사용. 검색/상세 페이지에서 JSON API 엔드포인트를 찾는다.
"""

import json
import re
import time

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

API_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

DELAY = 2.0


def separator(title: str):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")


def print_response(resp: httpx.Response, label: str, max_body: int = 300):
    print(f"\n--- {label} ---")
    print(f"  URL: {resp.url}")
    print(f"  Status: {resp.status_code}")
    ct = resp.headers.get("content-type", "unknown")
    print(f"  Content-Type: {ct}")
    body = resp.text[:max_body]
    print(f"  Body (first {max_body} chars): {body}")
    if resp.status_code in (301, 302, 303, 307, 308):
        print(f"  Redirect -> {resp.headers.get('location', 'N/A')}")


def find_json_in_scripts(html: str, label: str):
    """HTML의 <script> 태그에서 JSON 데이터를 찾는다."""
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    print(f"\n  [Script Tags] 총 {len(scripts)}개 발견")

    # schema.org JSON-LD
    ld_scripts = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if ld_scripts:
        print(f"  [JSON-LD] {len(ld_scripts)}개 발견:")
        for i, s in enumerate(ld_scripts):
            try:
                data = json.loads(s)
                print(f"    #{i+1}: @type={data.get('@type', 'N/A')}")
                print(f"    내용: {json.dumps(data, ensure_ascii=False)[:500]}")
            except json.JSONDecodeError:
                print(f"    #{i+1}: JSON 파싱 실패: {s[:200]}")
    else:
        print("  [JSON-LD] 없음")

    # __NEXT_DATA__
    next_data = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if next_data:
        print(f"  [__NEXT_DATA__] 발견! 길이: {len(next_data.group(1))}")
        try:
            data = json.loads(next_data.group(1))
            print(f"    keys: {list(data.keys())[:10]}")
        except json.JSONDecodeError:
            print(f"    파싱 실패: {next_data.group(1)[:200]}")
    else:
        print("  [__NEXT_DATA__] 없음")

    # __NUXT_DATA__ / __NUXT__
    nuxt = re.search(r'window\.__NUXT__\s*=\s*(\{.*?\})\s*;?\s*</script>', html, re.DOTALL)
    if nuxt:
        print(f"  [__NUXT__] 발견! 길이: {len(nuxt.group(1))}")
    else:
        print("  [__NUXT__] 없음")

    # window.__STATE__ / window.__INITIAL_STATE__ / window.__DATA__
    state_patterns = [
        (r'window\.__STATE__\s*=\s*(\{.*?\});', '__STATE__'),
        (r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', '__INITIAL_STATE__'),
        (r'window\.__DATA__\s*=\s*(\{.*?\});', '__DATA__'),
        (r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});', '__PRELOADED_STATE__'),
    ]
    for pat, name in state_patterns:
        m = re.search(pat, html, re.DOTALL)
        if m:
            print(f"  [{name}] 발견! 길이: {len(m.group(1))}")
            print(f"    미리보기: {m.group(1)[:300]}")
        else:
            print(f"  [{name}] 없음")

    # 큰 JSON 객체가 포함된 스크립트 찾기
    print(f"\n  [Large Script Tags] JSON 포함 가능성:")
    for i, s in enumerate(scripts):
        s_stripped = s.strip()
        if len(s_stripped) > 500:
            # JSON 시작 패턴 찾기
            json_starts = [m.start() for m in re.finditer(r'[=:]\s*\{', s_stripped)]
            if json_starts:
                print(f"    Script #{i+1}: 길이 {len(s_stripped)}, JSON 패턴 {len(json_starts)}개")
                print(f"      미리보기: {s_stripped[:200]}")

    # API URL 패턴 찾기
    api_urls = re.findall(r'["\'](/api/[^"\']+)["\']', html)
    api_urls += re.findall(r'["\'](https?://[^"\']*api[^"\']*)["\']', html)
    api_urls += re.findall(r'["\'](/v\d+/[^"\']+)["\']', html)
    if api_urls:
        print(f"\n  [API URLs in HTML] {len(api_urls)}개:")
        for url in sorted(set(api_urls))[:30]:
            print(f"    {url}")
    else:
        print("\n  [API URLs in HTML] 없음")

    # fetch/axios/ajax 호출 패턴
    fetch_calls = re.findall(r'fetch\(["\']([^"\']+)["\']', html)
    axios_calls = re.findall(r'axios\.\w+\(["\']([^"\']+)["\']', html)
    ajax_urls = fetch_calls + axios_calls
    if ajax_urls:
        print(f"\n  [Fetch/Axios URLs] {len(ajax_urls)}개:")
        for url in sorted(set(ajax_urls))[:20]:
            print(f"    {url}")
    else:
        print("\n  [Fetch/Axios URLs] 없음")


def main():
    client = httpx.Client(
        headers=HEADERS,
        timeout=15,
        follow_redirects=True,
        verify=False,
    )

    # =========================================================================
    # 1. Search page HTML
    # =========================================================================
    separator("1. 검색 페이지 HTML 분석")
    search_url = "https://abcmart.a-rt.com/product?keyword=나이키+덩크"
    try:
        resp = client.get(search_url)
        print_response(resp, "검색 페이지")
        if resp.status_code == 200:
            find_json_in_scripts(resp.text, "검색 HTML")
            # 상품 카드 패턴
            product_links = re.findall(r'href="(/product/\d+)"', resp.text)
            print(f"\n  [상품 링크] {len(product_links)}개: {product_links[:5]}")
    except Exception as e:
        print(f"  에러: {e}")

    time.sleep(DELAY)

    # =========================================================================
    # 2. Common API patterns
    # =========================================================================
    separator("2. API 엔드포인트 탐색")

    api_patterns = [
        "https://abcmart.a-rt.com/api/product/search?keyword=나이키+덩크",
        "https://abcmart.a-rt.com/api/v1/product/search?keyword=나이키+덩크",
        "https://abcmart.a-rt.com/api/goods/search?keyword=나이키+덩크",
        "https://abcmart.a-rt.com/api/search?keyword=나이키+덩크",
        "https://abcmart.a-rt.com/api/product?keyword=나이키+덩크",
        # a-rt.com 자체 API
        "https://api.a-rt.com/product/search?keyword=나이키+덩크",
        "https://api.a-rt.com/v1/product/search?keyword=나이키+덩크",
        # display API (common in Korean e-commerce)
        "https://abcmart.a-rt.com/api/display/search?keyword=나이키+덩크",
        "https://abcmart.a-rt.com/display/search?keyword=나이키+덩크",
        # goods API
        "https://abcmart.a-rt.com/api/goods?keyword=나이키+덩크",
        "https://abcmart.a-rt.com/goods/search?keyword=나이키+덩크",
    ]

    for url in api_patterns:
        try:
            resp = client.get(url, headers=API_HEADERS)
            is_json = "json" in resp.headers.get("content-type", "").lower()
            status = "JSON!" if is_json else resp.headers.get("content-type", "")[:40]
            body_preview = resp.text[:200] if resp.status_code != 404 else "(404)"
            print(f"\n  [{resp.status_code}] [{status}] {url}")
            if is_json or resp.status_code == 200:
                print(f"    Body: {body_preview}")
        except Exception as e:
            print(f"  [ERR] {url}: {e}")
        time.sleep(DELAY)

    # =========================================================================
    # 3. Product detail page
    # =========================================================================
    separator("3. 상품 상세 페이지 분석")

    # 먼저 검색에서 찾은 상품 ID 사용, 없으면 기본값
    detail_id = None
    try:
        resp = client.get(search_url)
        if resp.status_code == 200:
            links = re.findall(r'/product/(\d+)', resp.text)
            if links:
                detail_id = links[0]
    except Exception:
        pass

    time.sleep(DELAY)

    if not detail_id:
        detail_id = "0000899399"  # fallback
        print(f"  검색에서 상품 ID 못 찾음, 폴백: {detail_id}")
    else:
        print(f"  검색에서 찾은 상품 ID: {detail_id}")

    detail_url = f"https://abcmart.a-rt.com/product/{detail_id}"
    try:
        resp = client.get(detail_url)
        print_response(resp, "상품 상세 페이지", max_body=500)
        if resp.status_code == 200:
            find_json_in_scripts(resp.text, "상세 HTML")

            # 사이즈 관련 HTML 패턴
            size_selects = re.findall(r'<(?:select|option|button)[^>]*(?:size|옵션|사이즈)[^>]*>', resp.text, re.IGNORECASE)
            print(f"\n  [사이즈 HTML 요소] {len(size_selects)}개:")
            for s in size_selects[:10]:
                print(f"    {s[:150]}")

            # 가격 관련 패턴
            prices = re.findall(r'(?:price|가격|판매가)["\s:]*["\s]*(\d{2,3},?\d{3})', resp.text, re.IGNORECASE)
            print(f"\n  [가격 패턴] {len(prices)}개: {prices[:5]}")

            # data 속성에서 JSON 찾기
            data_attrs = re.findall(r'data-(?:product|goods|item)[^=]*="([^"]+)"', resp.text)
            print(f"\n  [data-* 속성] {len(data_attrs)}개:")
            for d in data_attrs[:10]:
                print(f"    {d[:150]}")

            # goodsCd/productCd 등 상품 코드 관련
            goods_patterns = re.findall(r'(?:goodsCd|productCd|itemCd|goodsNo)["\s:=]+["\s]*(\w+)', resp.text)
            print(f"\n  [상품코드 패턴] {goods_patterns[:10]}")

    except Exception as e:
        print(f"  에러: {e}")

    time.sleep(DELAY)

    # =========================================================================
    # 4. 상품 상세 API 패턴
    # =========================================================================
    separator("4. 상품 상세 API 탐색")

    if detail_id:
        detail_api_patterns = [
            f"https://abcmart.a-rt.com/api/product/{detail_id}",
            f"https://abcmart.a-rt.com/api/goods/{detail_id}",
            f"https://abcmart.a-rt.com/api/v1/product/{detail_id}",
            f"https://abcmart.a-rt.com/api/v1/goods/{detail_id}",
            f"https://abcmart.a-rt.com/api/product/detail/{detail_id}",
            f"https://abcmart.a-rt.com/api/goods/detail/{detail_id}",
            f"https://abcmart.a-rt.com/api/display/goods/{detail_id}",
            # 옵션/사이즈 API
            f"https://abcmart.a-rt.com/api/product/{detail_id}/options",
            f"https://abcmart.a-rt.com/api/goods/{detail_id}/options",
            f"https://abcmart.a-rt.com/api/product/{detail_id}/size",
            f"https://abcmart.a-rt.com/api/goods/{detail_id}/stock",
        ]

        for url in detail_api_patterns:
            try:
                resp = client.get(url, headers=API_HEADERS)
                is_json = "json" in resp.headers.get("content-type", "").lower()
                status = "JSON!" if is_json else resp.headers.get("content-type", "")[:40]
                body_preview = resp.text[:300] if resp.status_code != 404 else "(404)"
                print(f"\n  [{resp.status_code}] [{status}] {url}")
                if is_json or resp.status_code in (200, 201):
                    print(f"    Body: {body_preview}")
            except Exception as e:
                print(f"  [ERR] {url}: {e}")
            time.sleep(DELAY)

    # =========================================================================
    # 5. JS 파일에서 API 베이스 URL 탐색
    # =========================================================================
    separator("5. JS 파일 API URL 탐색")

    try:
        resp = client.get("https://abcmart.a-rt.com/product?keyword=나이키+덩크")
        if resp.status_code == 200:
            # JS 파일 URL 추출
            js_files = re.findall(r'<script[^>]*src="([^"]*\.js[^"]*)"', resp.text)
            print(f"  JS 파일 {len(js_files)}개:")
            for f in js_files[:10]:
                print(f"    {f}")

            # 가장 큰 JS 파일 2개 분석 (앱 번들)
            app_js = [f for f in js_files if 'app' in f.lower() or 'main' in f.lower() or 'chunk' in f.lower()]
            if not app_js:
                app_js = js_files[:2]

            for js_url in app_js[:3]:
                time.sleep(DELAY)
                full_url = js_url if js_url.startswith("http") else f"https://abcmart.a-rt.com{js_url}"
                try:
                    resp_js = client.get(full_url)
                    if resp_js.status_code == 200:
                        # API 패턴 찾기
                        apis_in_js = re.findall(r'["\'](/api/[^"\']+)["\']', resp_js.text)
                        apis_in_js += re.findall(r'["\'](https?://[^"\']*api[^"\']*)["\']', resp_js.text)
                        # baseURL 패턴
                        base_urls = re.findall(r'baseURL["\s:]+["\'](https?://[^"\']+)["\']', resp_js.text)

                        if apis_in_js or base_urls:
                            print(f"\n  [{js_url}]")
                            if base_urls:
                                print(f"    baseURL: {base_urls[:5]}")
                            unique_apis = sorted(set(apis_in_js))
                            for api in unique_apis[:30]:
                                print(f"    API: {api}")
                except Exception as e:
                    print(f"  JS 분석 실패 ({js_url}): {e}")

    except Exception as e:
        print(f"  에러: {e}")

    # =========================================================================
    # 6. a-rt.com 루트 도메인 API 탐색
    # =========================================================================
    separator("6. a-rt.com 루트 도메인 탐색")
    root_urls = [
        "https://a-rt.com/api/product/search?keyword=나이키+덩크",
        "https://www.a-rt.com/api/product/search?keyword=나이키+덩크",
        "https://a-rt.com/product?keyword=나이키+덩크",
    ]
    for url in root_urls:
        try:
            resp = client.get(url, headers=API_HEADERS)
            is_json = "json" in resp.headers.get("content-type", "").lower()
            status = "JSON!" if is_json else resp.headers.get("content-type", "")[:40]
            print(f"\n  [{resp.status_code}] [{status}] {url}")
            if is_json or resp.status_code == 200:
                print(f"    Body: {resp.text[:200]}")
        except Exception as e:
            print(f"  [ERR] {url}: {e}")
        time.sleep(DELAY)

    client.close()

    # =========================================================================
    # Summary
    # =========================================================================
    separator("요약")
    print("""
  이 스크립트의 결과를 바탕으로:
  - 사용 가능한 JSON API 엔드포인트
  - HTML에서 추출 가능한 데이터 소스
  - schema.org JSON-LD 유무 및 내용
  - 사이즈/가격 데이터 접근 방법
  을 판단할 수 있다.
    """)


if __name__ == "__main__":
    main()
