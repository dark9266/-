#!/usr/bin/env python3
"""ABC마트 2차 탐색: JS 번들에서 API URL 추출 + abc.global 기반 API 탐색."""

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
    "Referer": "https://abcmart.a-rt.com/product?keyword=나이키+덩크",
}

DELAY = 2.0


def separator(title: str):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")


def main():
    client = httpx.Client(
        headers=HEADERS,
        timeout=20,
        follow_redirects=True,
        verify=False,
    )

    # =========================================================================
    # 1. 검색 HTML에서 abc.global + abc 스크립트 분석
    # =========================================================================
    separator("1. abc.global 객체 + 인라인 스크립트 분석")
    resp = client.get("https://abcmart.a-rt.com/product?keyword=나이키+덩크")
    html = resp.text

    # abc.global 전체 출력
    global_match = re.search(r'abc\.global\s*=\s*\{(.*?)\};', html, re.DOTALL)
    if global_match:
        print(f"  abc.global = {{{global_match.group(1)}}}")

    # abc. 로 시작하는 함수 호출 찾기
    abc_calls = re.findall(r'(abc\.\w+\([^)]*\))', html)
    print(f"\n  abc.*() 호출 {len(abc_calls)}개:")
    for c in sorted(set(abc_calls)):
        print(f"    {c}")

    # $.ajax, $.get, $.post 패턴
    ajax_calls = re.findall(r'\$\.(?:ajax|get|post)\s*\(\s*["\']([^"\']+)["\']', html)
    print(f"\n  jQuery AJAX 호출: {ajax_calls[:10]}")

    # URL 패턴 (상대/절대) with /product or /goods or /search
    url_patterns = re.findall(r'["\'](/(?:product|goods|search|display|api|catalog)[^"\']*)["\']', html)
    print(f"\n  URL 패턴: {len(url_patterns)}개")
    for u in sorted(set(url_patterns))[:30]:
        print(f"    {u}")

    time.sleep(DELAY)

    # =========================================================================
    # 2. 주요 JS 번들에서 API URL 추출
    # =========================================================================
    separator("2. JS 번들 API URL 탐색")

    js_files = re.findall(r'<script[^>]*src="(https://image\.a-rt\.com/script/[^"]*)"', html)
    # common.js, abc 관련 JS 우선
    priority_js = [f for f in js_files if any(k in f.lower() for k in ['common', 'abc', 'product', 'search', 'goods', 'display'])]
    print(f"  우선 JS 파일 {len(priority_js)}개:")
    for f in priority_js:
        print(f"    {f}")

    # 나머지 JS (라이브러리 제외)
    other_js = [f for f in js_files if f not in priority_js and 'plugin' not in f.lower()
                and 'jquery' not in f.lower() and 'swiper' not in f.lower()
                and 'moment' not in f.lower()]
    print(f"\n  기타 JS 파일 {len(other_js)}개:")
    for f in other_js:
        print(f"    {f}")

    all_api_urls = set()
    for js_url in (priority_js + other_js)[:8]:
        time.sleep(DELAY)
        try:
            resp_js = client.get(js_url)
            if resp_js.status_code != 200:
                continue

            text = resp_js.text
            # /로 시작하는 API 경로
            apis = re.findall(r'["\'](/(?:api|display|goods|product|search|catalog|order|cart|member|event|review|option)[^"\']*)["\']', text)
            # $.ajax url
            ajax = re.findall(r'url\s*:\s*["\']([^"\']+)["\']', text)
            apis_filtered = [a for a in ajax if '/' in a and 'http' not in a and '.js' not in a and '.css' not in a]

            if apis or apis_filtered:
                print(f"\n  [{js_url.split('/')[-1]}]")
                for a in sorted(set(apis))[:20]:
                    print(f"    API: {a}")
                    all_api_urls.add(a)
                for a in sorted(set(apis_filtered))[:10]:
                    print(f"    AJAX: {a}")
                    all_api_urls.add(a)

        except Exception as e:
            print(f"  에러 ({js_url}): {e}")

    time.sleep(DELAY)

    # =========================================================================
    # 3. 발견된 API URL 테스트
    # =========================================================================
    separator("3. 발견된 API URL 테스트")
    print(f"  테스트할 URL {len(all_api_urls)}개")

    # 검색 관련 URL 우선 테스트
    search_apis = sorted([u for u in all_api_urls if 'search' in u.lower() or 'product' in u.lower() or 'goods' in u.lower()])
    for api_path in search_apis[:15]:
        time.sleep(DELAY)
        # 파라미터가 없는 경우 키워드 추가
        url = f"https://abcmart.a-rt.com{api_path}"
        if '?' not in api_path and '{' not in api_path:
            url += "?keyword=나이키+덩크"
        try:
            resp = client.get(url, headers=API_HEADERS)
            is_json = "json" in resp.headers.get("content-type", "").lower()
            status_tag = "JSON" if is_json else "HTML"
            body = resp.text[:300] if resp.status_code < 400 else f"({resp.status_code})"
            print(f"\n  [{resp.status_code}] [{status_tag}] {url}")
            if resp.status_code < 400:
                print(f"    {body}")
        except Exception as e:
            print(f"  [ERR] {url}: {e}")

    # =========================================================================
    # 4. Spring MVC 패턴 기반 API 탐색 (a-rt.com은 Spring)
    # =========================================================================
    separator("4. Spring MVC 패턴 탐색")

    spring_patterns = [
        # 채널별 검색 (currentChannel: 10001 = abcmart)
        "/display/goods/search?channelCd=10001&keyword=나이키+덩크",
        "/display/goods/list?channelCd=10001&keyword=나이키+덩크",
        "/goods/list?channelCd=10001&keyword=나이키+덩크",
        "/product/search/list?keyword=나이키+덩크",
        "/product/list?keyword=나이키+덩크",
        "/search/list?keyword=나이키+덩크",
        "/search?keyword=나이키+덩크",
        # 카탈로그
        "/catalog/search?keyword=나이키+덩크",
        "/display/search?keyword=나이키+덩크&channelCd=10001",
        # goods 직접
        "/goods/search?keyword=나이키+덩크&channelCd=10001",
    ]

    for path in spring_patterns:
        time.sleep(DELAY)
        url = f"https://abcmart.a-rt.com{path}"
        try:
            resp = client.get(url, headers=API_HEADERS)
            is_json = "json" in resp.headers.get("content-type", "").lower()
            is_html = "html" in resp.headers.get("content-type", "").lower()
            tag = "JSON" if is_json else ("HTML" if is_html else resp.headers.get("content-type", "")[:30])
            print(f"\n  [{resp.status_code}] [{tag}] {path}")
            if resp.status_code < 400:
                body = resp.text[:300]
                print(f"    {body}")
                # JSON 응답이면 상세 분석
                if is_json:
                    try:
                        data = json.loads(resp.text)
                        print(f"    Keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
                        print(f"    Full: {json.dumps(data, ensure_ascii=False)[:500]}")
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"  [ERR] {path}: {e}")

    # =========================================================================
    # 5. 메인 페이지에서 실제 상품 ID 찾기
    # =========================================================================
    separator("5. 실제 상품 ID 찾기 (메인/카테고리)")

    category_urls = [
        "https://abcmart.a-rt.com/",
        "https://abcmart.a-rt.com/display/main",
    ]

    goods_ids = set()
    for url in category_urls:
        time.sleep(DELAY)
        try:
            resp = client.get(url)
            if resp.status_code == 200:
                # goodsCd 패턴
                ids = re.findall(r'goodsCd["\s:=]+["\s]*(\d{7,12})', resp.text)
                ids += re.findall(r'/product/(\d{7,12})', resp.text)
                ids += re.findall(r'goods_cd["\s:=]+["\s]*(\d{7,12})', resp.text, re.IGNORECASE)
                goods_ids.update(ids)
                print(f"  [{url}] 상품 ID {len(ids)}개: {list(set(ids))[:10]}")
        except Exception as e:
            print(f"  에러: {e}")

    # 찾은 상품 ID로 상세 페이지 테스트
    if goods_ids:
        test_id = list(goods_ids)[0]
        separator(f"6. 상품 상세 테스트: {test_id}")

        time.sleep(DELAY)
        resp = client.get(f"https://abcmart.a-rt.com/product/{test_id}")
        print(f"  Status: {resp.status_code}")
        if resp.status_code == 200:
            detail_html = resp.text

            # JSON-LD
            ld_scripts = re.findall(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                detail_html, re.DOTALL,
            )
            for i, s in enumerate(ld_scripts):
                try:
                    data = json.loads(s)
                    print(f"  JSON-LD #{i+1}: {json.dumps(data, ensure_ascii=False)[:500]}")
                except json.JSONDecodeError:
                    print(f"  JSON-LD #{i+1}: 파싱 실패")

            # abc.global
            gm = re.search(r'abc\.global\s*=\s*\{(.*?)\};', detail_html, re.DOTALL)
            if gm:
                print(f"  abc.global: {gm.group(1)[:300]}")

            # 인라인 스크립트에서 상품 데이터
            scripts = re.findall(r'<script[^>]*>(.*?)</script>', detail_html, re.DOTALL)
            for i, s in enumerate(scripts):
                if len(s.strip()) > 200 and any(k in s for k in ['goodsCd', 'goodsNm', 'salePrice', 'optnList', 'option']):
                    print(f"\n  상품 데이터 스크립트 #{i+1} (길이 {len(s)}):")
                    print(f"    {s[:500]}")

            # 사이즈/옵션 관련 데이터
            optn_data = re.findall(r'optn\w*\s*[=:]\s*(\[.*?\]|\{.*?\})', detail_html, re.DOTALL)
            for d in optn_data[:3]:
                print(f"\n  옵션 데이터: {d[:300]}")

            # goodsInfo 같은 객체
            goods_info = re.findall(r'(?:goodsInfo|goodsData|productInfo|productData)\s*[=:]\s*(\{.*?\})', detail_html, re.DOTALL)
            for d in goods_info[:3]:
                print(f"\n  상품 정보 객체: {d[:300]}")

            # abc. 함수 호출
            abc_calls = re.findall(r'(abc\.\w+\([^)]*\))', detail_html)
            print(f"\n  abc.*() 호출: {sorted(set(abc_calls))[:20]}")

    client.close()


if __name__ == "__main__":
    main()
