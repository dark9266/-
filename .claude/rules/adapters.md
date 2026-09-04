---
paths:
  - "src/adapters/**/*.py"
  - "src/crawlers/**/*.py"
  - "scripts/probe_*.py"
---

# 소싱처 어댑터·크롤러 규칙

> 이 파일은 위 `paths` 에 걸리는 파일을 열 때만 로드된다. 상시 로드 아님.
> 방향·금지 리스트는 `CLAUDE.md`, 크림 API 호출 규칙은 `.claude/rules/kream-api.md`.

## 🚫 신규 소싱처 추가 금지 (안정화 확정 전까지)
`direction_guard` 훅이 `src/adapters/` · `src/crawlers/` 신규 파일 생성을 **물리 차단**한다.
해제 = `KREAM_NEW_SOURCE_GO=1`. 차단 메시지를 받으면 우회하지 말고 사장에게 보고한다.

## 크롤러 목록 (2026-04-19 기준 — Worksout/Carhartt 폐기, On Running 추가)
- `src/crawlers/musinsa_httpx.py` — 무신사. API 검색 (`caller=SEARCH`), 세션 쿠키 등급할인가
- `src/crawlers/twentynine_cm.py` — 29CM. 검색 API v4/products + HTML 파싱
- `src/crawlers/nike.py` — 나이키 공식몰. `__NEXT_DATA__` JSON 파싱 (selectedProduct 구조). LAUNCH 상품 자동 스킵
- `src/crawlers/adidas.py` — 아디다스 공식몰. taxonomy API (Akamai WAF, Referer 필수)
- `src/crawlers/kasina.py` — 카시나. NHN shopby API. Nike/adidas EXACT(`productManagementCd`), NB는 브랜드 덤프+regex. 사이즈별 재고(`saleType`+`forcedSoldOut`) 직접 노출
- `src/crawlers/abcmart.py` — 그랜드스테이지/온더스팟. a-rt.com 멀티채널 API, prefix 검색 + 상세 API 모델번호 보강
- `src/crawlers/tune.py` — 튠. Shopify Storefront GraphQL API (GET), variant title에서 모델번호/사이즈 파싱
- `src/crawlers/eql.py` — EQL. 한섬 편집숍. HTML 파싱 (검색 godNm 속성 + 상세 sizeItmNm/onlineUsefulInvQty)
- `src/crawlers/nbkorea.py` — 뉴발란스 공식몰. 카테고리 SSR 매핑 + getOtherColorOptInfo GET API
- `src/crawlers/salomon.py` — 살로몬 공식몰. Shopify products.json REST API. SKU=크림 모델번호(L+8자리), handle 직접 조회
- `src/crawlers/arcteryx.py` — 아크테릭스 코리아. api.arcteryx.co.kr Laravel REST API. 검색+옵션(사이즈/재고) 조합
- `src/crawlers/vans.py` — 반스 공식몰. Topick Commerce 플랫폼. 검색 JSON API + HTML data-sku-data 사이즈별 재고 파싱
- `src/crawlers/wconcept.py` — W컨셉. POST 검색 API (gw-front, DISPLAY-API-KEY) + GET 상세 HTML 파싱 (brazeJson/skuqty)
- `src/crawlers/on_running.py` — On Running 한국 공식몰. sitemap(/ko-kr/products.xml) 덤프 + JSON-LD SSR 파싱. 신형 11자(3MF10071043) + 구형 dot(61.99025) 이중 SKU. **2026-04-20 전수 검증: 즉시 매칭 150건 + collect 후보 742건. 어댑터 활성 유지**
- `src/crawlers/hoka.py` · `src/crawlers/asics.py` · `src/crawlers/puma.py` · `src/crawlers/patagonia.py` · `src/crawlers/thenorthface.py` · `src/crawlers/thehandsome.py` · `src/crawlers/beaker.py` — Phase 3 배치 크롤러
- `src/adapters/stussy_adapter.py` — Stussy 한국 공식몰 (kr.stussy.com, Shopify). `/products.json` 페이지네이션. variant.sku digit prefix → 크림 prefix 인덱스 매칭. 다중 후보 시 영문→한글 색상 사전으로 disambiguation
- `src/adapters/{patagonia,beaker,thehandsome,puma,asics,nike,adidas,thenorthface}_adapter.py` — Phase 3 배치 어댑터 (crawler 레이어 없이 어댑터에 통합)
- `src/crawlers/_netfunnel_helper.py` — NetFunnel 대기열 쿠키 획득 헬퍼 (asics.co.kr 입구 큐 통과용)
- `src/crawlers/registry.py` — 레지스트리 + 서킷브레이커 (3회 실패 → 30분 비활성화, 자동 재활성화)

## Rate Limit (지키지 않으면 소싱처 차단)

| 소싱처 | max_concurrent | min_interval | 시간당 안전 처리량 |
|--------|:-:|:-:|:-:|
| 무신사 | 5 | 1.0초 | ~3,600건 |
| 29CM | 2 | 2.5초 | ~1,440건 |
| 나이키 | 2 | 3.0초 | ~1,200건 |
| 아디다스 | 2 | 5.0초 | ~720건 |
| 카시나 | 2 | 1.5초 | ~2,400건 |
| 그랜드스테이지 | 3 | 2.0초 | ~1,800건 |
| 온더스팟 | 3 | 2.0초 | ~1,800건 |
| 튠 | 2 | 1.0초 | ~3,600건 |
| EQL | 2 | 1.5초 | ~2,400건 |
| 뉴발란스 | 2 | 2.0초 | ~1,800건 |
| 살로몬 | 2 | 1.0초 | ~3,600건 |
| 아크테릭스 | 2 | 2.0초 | ~1,800건 |
| 반스 | 2 | 1.5초 | ~2,400건 |
| W컨셉 | 2 | 2.0초 | ~1,800건 |
| 온러닝 | 2 | 1.5초 | ~2,400건 |

API 호출 간 최소 1~2초 딜레이. 대량 카탈로그 덤프는 소싱처 쪽 부하다.

## 막혔을 때 (403/202/빈 결과)
**"차단됐다"고 판단하기 전에 `kream-search-in-search` 스킬을 먼저 부른다.**
TLS 지문 로테이션 + 세션 워밍 + 내장 JSON 추출 + 4단계 escalation.
실측: 무신사·29CM 은 httpx 기본 UA 로 403 이지만 `curl_cffi impersonate="safari260"` 로 200 통과(2026-09-04).

## 데이터 처리 함정
- 사이즈 파싱: `isinstance(data, dict)` 체크 필수
- 모델번호: 무신사 SKU 가 아닌 **상품명에서 실제 품번 추출**
- `sqlite3.Row` 는 `.get()` 불가 — `dict()` 로 변환
- 크림 API 404 는 에러 아님 (거래량 0 신규 상품)

## 필터링
- 브랜드 필터: **블랙리스트 방식** — 크림에 절대 없는 브랜드만 스킵
- 오프라인전용: "오프라인 전용 상품" 정확 문구 + 구매버튼 없을 때만 스킵
- 발매예정: "판매예정" 또는 "출시예정" → 스킵
- 품절 필터 시 리스탁 고려. LAUNCH 만 영구 필터

## 소싱처 상태 (2026-04 기준)
무신사·29CM·나이키·카시나·그랜드스테이지·온더스팟·튠·EQL·뉴발란스·살로몬·아크테릭스·반스·W컨셉·온러닝 = 안정 /
아디다스 = WAF 주의 / 온러닝 = 매칭 150건 확정

## 크롤러 수정 후 의무
`live-tester` 에이전트로 실서버 e2e 검증. 색상 매칭은 **크림 실제 한글 상품명으로 교차검증** — 영문만 테스트 금지.

## 알려진 한계
- MFS(다중재고) 품절 필터 — inventory API 근본 미작동 (MT410CK5 등)
- 크림 거래량 5건 캡 — pinia/screens 모두 최대 5건 반환, ×3 추정치로 보충
