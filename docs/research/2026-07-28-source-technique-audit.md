# 22 소싱처 수집 기법 전수 감사 (2026-07-28)

사장 지시: "등록된 발굴처가 어떤 기법으로 도는지 + 더 좋은 기법이 있는지 + 잘 발굴하는지
코덱스와 함께 전수 조사". 조사 = explorer(코드 인벤토리) + 오케스트레이터(직접 검증) +
코덱스 consult(A/sol-high, `reports/codex/codex_06b89e9117b1d5fded24.md`).

**실호출 0건** — 전부 코드·DB 읽기로만 판정했다. 사이트 실응답은 재가동 시 측정 대상이다.

## 1. 현행 기법 인벤토리 (사실)

| 소싱처 | 전송 | 획득 방식 | 덤프 방식 | DB 카탈로그 |
|---|---|---|---|---|
| 무신사 | httpx | API 검색(`caller=SEARCH`)+세션쿠키 등급가 | 카테고리 페이지네이션 | 9,440 |
| 그랜드스테이지·온더스팟 | httpx | a-rt.com JSON | **브랜드 키워드 12개** | 3,669 |
| 아디다스 | httpx | `/api/search/taxonomy` JSON(사이즈·가격 포함) | offset 페이지네이션 | 3,469 |
| 노스페이스 | httpx | SSR HTML regex (JSON API 차단됨) | 카테고리 페이지네이션 | 2,679 |
| Stussy | httpx | Shopify `/products.json` | 전 페이지 | 2,139 |
| 29CM | httpx | search-api REST + RSC | **브랜드 키워드 9개 × 100** | 975 |
| On Running | httpx | sitemap 전수 + JSON-LD + `__NUXT_DATA__` | sitemap 전량 | 902 |
| 반스 | httpx | Topick JSON + HTML `data-sku-data` | **키워드 5개 × 100** | 824 |
| 튠 | httpx | Shopify Storefront GraphQL(GET) | **커서 페이지네이션(구현됨)** | 816 |
| 살로몬 | httpx | Shopify products.json | **페이지네이션(구현됨)** | 815 |
| 카시나 | httpx | NHN shopby | **NB만 브랜드 전량, Nike/adidas는 EXACT 1회** | 813 |
| EQL | httpx | HTML regex + getSearchGodPaging | page_no 페이지네이션 | 670 |
| 파타고니아 | httpx | getGoodslist JSON | 1회 호출로 전체 345건 | 639 |
| W컨셉 | httpx | POST 검색(gw-front) + GET 상세 | **페이지네이션 파라미터 미확인** | 552 |
| 아크테릭스 | httpx | Laravel REST 검색+옵션 | 카테고리 페이지네이션 | 535 |
| 아식스 | **curl_cffi safari17_0** | NetFunnel 큐 우회 + SSR | 카테고리 SSR | 450 |
| 리복 | httpx | GODOMALL HTML | 리스트 파싱 | 427 |
| 푸마 | httpx | SFCC HTML 내 GA4 JSON | 카테고리 페이지네이션 | 381 |
| 컨버스 | httpx | Cafe24 sitemap | sitemap 전량 | 339 (폐기 판정됨) |
| 나이키 | httpx | `__NEXT_DATA__` | 검색 페이지 기반(sitemap 아님) | 328 |
| 뉴발란스 | httpx | 카테고리 SSR 매핑 + 재고 GET | 카테고리 매핑 | 197 |
| 비이커 | httpx | SSF Ajax SSR | currentPage 페이지네이션 | 키 없음 |
| 더한섬 | httpx | Nuxt2 SSR + Spring REST | categoryGoodsList | 키 없음 |
| 호카 | **curl_cffi safari17_0** | SFCC Coveo GET | 검색 HTML | 키 없음 |

**요약**: 24곳 중 22곳이 순수 httpx + UA/Referer. TLS 지문 위장은 아식스·호카뿐.
**24개 어댑터 전부 `dump_catalog` 보유** (초기 "덤프 코드 없음" 판정은 크롤러 파일만 본 오류 —
정정함). DB 카탈로그 활동은 **2026-05-05 이후 정지**(봇 정지 상태이므로 당연).

## 2. 코덱스 판정 (수용 5 / 거부 0 / 정정 1)

| 초안 | 코덱스 판정 | 내 결론 |
|---|---|---|
| P1 지문 선제 전환(높음) | **강등** — 해외 실측을 한국몰에 일반화 불가 | **수용**. 내 과잉 일반화 인정 |
| P2 커버리지(높음) | 동의 + 대상 확대(wconcept·kasina·arcteryx) | **수용**, 전제는 내가 정정 |
| P3 TNF JSON 재시험 | 조건부 | 수용 |
| P4 regex→bs4 | 실익 낮음 | 수용 |
| P5 재가동 검증(낮음) | **최우선 선행조건으로 승격** | **수용** — 가장 중요한 정정 |

### 수용 근거
1. **P5 승격**: 5/5 이후 3개월 검증 공백. 지금 수치는 "최근 사실"이 아니라 **재가동 전 가설**.
   죽은 엔드포인트를 최적화하는 낭비를 막는다.
2. **P1 강등**: 서치인서치 실측표(adidas=DataDome/safari260, nbkorea=chrome131_android)는
   **타 프로젝트의 해외몰 측정**이다. 같은 브랜드라도 국가별 운영사·CDN·WAF가 다르다.
   → "httpx를 curl_cffi로 교체"가 아니라 **"httpx 기본 + 도메인별 검증된 curl_cffi fallback"**.
3. **지문 무작위 회전 금지**: 한 세션 내에서는 지문·쿠키·헤더를 **일관 유지**. 요청마다
   회전하면 그 자체가 이상 징후다. (스킬 문서의 "로테이션"은 *탐색 시* 지문 찾기용이지
   운영 중 매 요청 회전이 아니다 — 스킬 해석 정정.)

### 실패 판정 기준 (상태코드 아님 — 의미로 판정)
- 알려진 canary 상품 부재 / 기대 필드 부재 / 연속 페이지가 같은 집합 반환 / 건수 급감
- **200이어도 상품 데이터 없으면 실패**, challenge 문구 있어도 데이터 있으면 성공

## 3. 커버리지 실익 순위 (코덱스 + 내 검증)

1. **카시나 Nike/adidas 브랜드 덤프 확장** — NB 경로가 이미 있어 재사용. EXACT 검색이
   SKU 표기차·색상 접미사로 놓치는 걸 낮은 작업량으로 회수.
2. **튠 GraphQL 커서 전수** — 멀티브랜드라 교집합 증가 여지 큼. (GET GraphQL이라 읽기 정책 OK)
3. **29CM 브랜드·카테고리 partition 페이지네이션** — 절대 증가량 최대. 단 사이트 전체 덤프가
   아니라 **47k 대상 브랜드 집합으로 partition** 후 각각 끝까지.
   - 실측 근거: 현재 9브랜드 × 100 상한 = 이론 최대 900, DB 975. **구조적 상한에 도달**.
     무신사(카테고리 전수) 9,440과 10배 차이.
- 살로몬 products.json 전수화는 매우 저렴하나 815건이 이미 실제 상품 범위에 가까울 수 있어
  추가 기대치는 위 3개보다 낮음(난이도 중시하면 3위 승격 가능).

## 4. 우리가 안 보던 기법 축 (코덱스 Q4)

가장 큰 누락은 "더 강한 우회"가 아니라 **발견과 최신화의 분리**:
- **Catalog discovery**(넓게 발견) → **Candidate hydration**(47k 매칭분만 상세·재고) →
  **Alert validation**(후보만 크림 GET으로 sell_now·거래량)
- 추가 축: 모바일/앱 공개 read-only API · variant/size 재고 전용 엔드포인트 ·
  sitemap `lastmod`+ETag+If-Modified-Since 증분 · Next/Nuxt/RSC 빌드 manifest에서 endpoint 발견 ·
  사이트가 이미 쓰는 검색 인덱스(Algolia·Elastic·SFCC·Cafe24·shopby) ·
  content hash 변경 감지 + 삭제 상품 tombstone · 주기 전체 reconciliation + 증분 병행
- 주의: `lastmod`는 가격·재고 변경을 반영 안 할 수 있음(전체 재검증 대체 불가) /
  앱 API는 공개·비인증·읽기 전용만 / 브라우저는 조사 도구지 운영 기본값 아님 /
  크림은 GET-only 유지, 로컬 DB 가격을 수익 계산 대체값으로 쓰지 말 것

## 5. 실행 순서 (확정)

**전제**: 22 소싱처 안정화 단계. 신규 소싱처 추가 금지. 아래는 전부 기존 소싱처 개선.

1. **[선행] 재가동 검증 하네스** — 소싱처별 canary 상품 + 페이지네이션 종료 검증 +
   필드 충실도 + 건수 급감 감지. 의미 기반 성공 판정. ← **다음 작업 후보 1순위**
2. 커버리지 누락 제거 — 카시나 → 튠 → 29CM 순
3. 전송 fallback 구조 — httpx 기본 + 도메인별 curl_cffi fallback (adidas·nbkorea paired probe)
4. (조건부) TNF JSON shadow 비교 · wconcept 페이지네이션 확인

Camoufox는 3단계까지 다 해보고 막혔을 때만 도입 제안(현재 의존성 없음).
