---
name: kream-search-in-search
description: 막힌 소싱처·봇월·WAF에서 데이터를 우회 추출하는 범용 기법("서치인서치"). TLS 지문 로테이션 fetch + 세션 워밍 + 페이지 내부 JSON(__NUXT_DATA__/__NEXT_DATA__/JSON-LD/SFCC) 추출 + 4단계 escalation + Camoufox 티어. 소싱처 크롤러가 403/202/빈 결과를 낼 때, "차단된 것 같다"고 판단하기 전에, 사이즈별 재고가 정적 HTML에 없을 때 적용. 봇월·WAF·Akamai·DataDome·Cloudflare·크롤러 차단·덤프 실패 상황 전부.
---

# 크림봇 서치인서치 — 봇월/WAF 우회 + 내부 JSON 추출

구매대행 프로젝트에서 실측 축적된 기법을 크림봇으로 이식(2026-07-24). 핵심 원리 =
**직접 API가 막히거나 데이터가 페이지 HTML/JS 안에 숨어 있을 때 우회 추출**. 도메인 무관.

## 🔴 크림봇 적용 범위 (먼저 읽어라)
- 이 스킬은 **기존 22 소싱처 중 막힌 곳을 뚫는 데** 쓴다. 소싱처 **신규 추가는 안정화 확정
  후**다(`direction_guard` 훅이 신규 어댑터 생성을 막는다).
- **한국 공홈만**. 아래 실측표에 미국몰(hoka·backcountry·jdsports 등)이 나오는 건
  **기법 근거**일 뿐 — 소싱처 후보가 아니다(`feedback_sourcing_korea_only`).
- 읽기 전용 원칙 준수: GET + 검색/GraphQL POST 만. 로그인·페이월·PO-token 우회는 **금지**
  (`STILL_BLOCKED` 판정 후 사장에게 보고).
- 파이프라인 접점: `api-prober`(엔드포인트 탐색) → **이 스킬**(데이터 도달) →
  `source-analyzer`(덤프/재고 판별) → `crawler-builder`(구현) → `live-tester`(e2e).

## 핵심 원리 4가지

### 1. TLS 지문은 사이트마다 다르다 (★ 최대 오판 원인)
`curl_cffi` impersonate 값을 **로테이션**한다. 크림 크롤러(`src/crawlers/kream.py`)가 이미
Safari 핑거프린트로 도는 것과 같은 계열이다.

| 지문 | 실측 돌파 |
|---|---|
| `safari260` / `safari2601` / `safari260_ios` | hoka · on · **salomon** · **adidas(DataDome!)** · footlocker |
| `chrome131_android` | **newbalance(SFCC/Akamai)** · joes_nb — 여긴 safari=403, **android 만 통과** |
| `chrome124` / `chrome146` 데스크톱 | 대체로 **403** — "Chrome 필수" 오판의 원인 |

→ 로테이션에 **safari 계열 + chrome131_android 둘 다** 넣고, 사이트별 첫 성공 지문을 기억한다.
현행 CLAUDE.md 는 "adidas = Akamai WAF, Referer 필수"로 적혀 있는데, 위 실측은
**DataDome + safari260 돌파**다 — adidas 가 막히면 이 지문부터 시도하라.

### 2. 세션 워밍 필수
`cf.Session(impersonate="safari260")` 로 **home → 카테고리 → product 순차 GET(쿠키 승계)
+ 1~3s 간격**. 워밍 없는 단발 GET 은 rate 로 막힌다(실측 5/6 = 83% 성공).
크림봇 rate limit 표(CLAUDE.md)의 `min_interval` 을 지키면 자연히 충족된다.

### 3. "차단" 오판 2종 (★ 이게 진짜 함정)
**a) SPA 셸 false-positive** — 200 인데 본문에 `enable JavaScript`/`challenge` 문자열이 있어
crude regex 가 CHALLENGE 로 오판한다. **200 + 내장 데이터가 있으면 통과한 것**이다.
실제 body 를 열어 **데이터 유무로** 판정하라.

**b) "상품 링크 0 = 벽" 오판** — 사이트마다 상품 URL 패턴이 다르다
(`/cat//brand/` · `/plp//pdp/` · `.html` · SFCC `/pd/`). `.html` 만 찾는 regex 는
**진짜 페이지도 "상품 0"** 으로 오판한다. 벽 단정 전 경로 세그먼트 분포를 덤프하라:

```python
from collections import Counter
import re
Counter(x.split('/')[1] for x in re.findall(r'href="(/[^"]+)"', html) if len(x.split('/')) > 1)
```
title 이 진짜 상품명이고 cat/brand/plp/pdp 세그먼트가 많으면 **통과한 것** — regex 만 고치면 된다.
**len < 5KB 의 tiny 셸이라야 진짜 차단**이다.

### 4. 데이터 위치별 추출 (봇월 통과 후)
- **JSON-LD** `Product` / `AggregateOffer` — 가장 흔함(name · availability InStock/OOS ·
  lowPrice/highPrice). SFCC 계열(hoka·nb) 기본.
- **`<script id="__NUXT_DATA__">`** = devalue 포맷 **flat 배열**(배열 값이 `__ref` 인덱스
  포인터). 사이즈·SKU·variant 전부 내장. **크림봇에 이미 구현 있음** →
  `src/crawlers/on_running.py` 참고(On Running 이 정확히 이 구조다).
- **`__NEXT_DATA__`**(Next.js) · `window.__INITIAL_STATE__` · `window.__NUXT__`
- **크림 자체**는 `__NUXT_DATA__` 파싱 — `src/crawlers/kream.py` 가 표준 구현이다.
- ⚠️ **HTML 속성 파싱은 정규식보다 BeautifulSoup** 권장(단일따옴표·대문자·속성순서 변형 커버).

## 4단계 escalation (싼 것부터 올라간다)
0. **공개 API/feed 먼저** — sitemap · `products.json`(Shopify) · 검색 API.
   크림봇 소싱처 다수가 여기서 끝난다(salomon=Shopify REST, tune=Storefront GraphQL,
   stussy=`/products.json`, on_running=sitemap).
1. **URL 변형** — mobile / RSS / `.json` 접미 / API 서브도메인.
2. **TLS impersonate 로테이션 + 워밍** (위 §1·§2).
3. **headless browser** — Camoufox(아래) 또는 Playwright.

## Camoufox 티어 (실측 — WSL 헤드리스로 AWS WAF·Akamai 통과)
- `Camoufox(headless=True, humanize=True)` — 데스크탑 Chrome 브리지 **불요**, WSL 단독 기동.
- 실측: AWS WAF(curl_cffi 로는 202 차단) → Camoufox 200 · 983KB · 정상 title · 링크 755개 =
  **통과**. Akamai + Next.js 사이트도 plp→pdp 완전 소싱.
- → **AWS WAF 는 토큰 솔버 없이 Camoufox 로 뚫린다**(untrusted 코드 실행 불요).
- **함정**: FlareSolverr · Byparr · Trawl 은 **Cloudflare 전용**(AWS WAF 무용).
- **언제 안 되나**: Cloudflare Enterprise **IP 평판 하드차단**엔 무용 — 지문 위장은 IP 차단을
  못 뚫는다. VPN/프록시는 사장 방침상 쓰지 않는다 → 보류하고 보고.
- ⚠️ 크림봇에는 `camoufox` 의존성이 **아직 없다**. 3단계까지 다 해보고도 막혔을 때만 도입을
  제안하라(pyproject 추가 = 사장 판단).

## 새 소싱처 봇월 온보딩 체크리스트 (막힌 곳 재시도에도 동일)
1. **지문 로테이션 + 워밍**으로 PDP 1건 시도 → `status 200` + 데이터 마커 있으면 **통과**
   (§3a: 'enable JS' 문자열이 있어도 데이터가 있으면 OK).
2. **안 뚫리면**:
   ① **유효 URL 확보** — 카테고리 200 확인 후 실제 상품 href 추출.
      **죽은 SKU 의 404 를 차단으로 오판 금지**.
   ② arsenal 확장(firefox147 · edge101 등).
   ③ **home 부터** 403/202(Akamai/AWS WAF challenge)면 진짜 차단 → Camoufox 티어 검토.
3. **데이터 위치 파악**(§4) → 기존 파서(`src/crawlers/<소싱처>.py`)에 그 HTML 을 넣어본다.
   되면 끝(지문만 바꾸면 되는 경우가 많다). 안 되면 위치별 추출 함수 추가.
4. **per-size 재고가 async 면**(dynamicyield · 별도 stock API · 사이즈그리드 JS): 정적 HTML 은
   제품레벨(가격·전체 InStock)만 준다 → 그 async 엔드포인트를 직접 공략하거나 브라우저 폴백.
   ⚠️ 크림봇 정확성 1순위는 **사이즈별 실재고**다 — 제품레벨만으로 알림 보내지 마라.

## STILL_BLOCKED 판정 (중요)
로그인 · 페이월 · PO-token 은 **우회 시도하지 않는다**. 전용 도구로 이관하거나 사장에게
3줄 보고(어디서 막힘 / 왜 불가 / 사장이 원클릭으로 뭘). ⚠️ 단 **"안 된다"고 말하려면 실제로
시도한 실패 출력이 있어야 한다** — `claim_evidence_guard` 훅이 증거 없는 "불가"를 막는다.

## 관련
- `src/crawlers/kream.py`(curl_cffi Safari + `__NUXT_DATA__` 표준 구현) ·
  `src/crawlers/on_running.py`(devalue 배열 파싱) · `src/crawlers/registry.py`(서킷브레이커)
- 원본 실측 기록: 구매대행 `.claude/skills/poel-search-in-search/SKILL.md`
