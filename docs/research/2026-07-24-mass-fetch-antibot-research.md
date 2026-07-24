# 크림 대량 조회 — anti-bot · 프록시 · TLS 자료 모음

> 2026-07-24, 4각 병렬 정찰(GitHub·X/커뮤니티·심층기술문서·한국특화) 실재 확인 결과.
> 목적: 크림 공개 시세를 **실계정 안 걸고(로그인 X) 대량·안전하게 조회**하는 아키텍처 설계 근거.
> 전제(실측): 크림봇은 `ensure_login()` 비활성 = 로그인 없이 `__NUXT_DATA__`/pinia 공개 데이터
> 익명 조회. 따라서 리스크는 "실계정 차단"이 아니라 **IP/기기(webDid) 차단 + 크림 약관상
> 크롤링 금지(단독 차단권)**. 해결 축 = IP 분산 + TLS 지문 로테이션 + 자동 감속.

---

## 실행 결론 (요약)

1. **우리는 이미 맞는 도구를 쓴다** — `curl_cffi`가 이 분야 대표(★6.1k, lexiforest 활성 계승판).
   헛발 아님. 개선점 = 지문을 IP마다 Chrome/Firefox/Safari 로테이션 + HTTP/2 지문 일관성.
2. **IP 분산이 핵심** — 두 갈래:
   - (A) **레지덴셜 프록시** (월요금) — 표준·안정, 비용 발생.
   - (B) **AWS API Gateway 무료 IP 로테이션** (`requests-ip-rotator`·`FireProx`) — 거의 무료,
     단 크림이 AWS IP 대역을 통째로 차단하면 무력 → **실측 검증 필요**.
3. **탐지 한계선** — TLS 지문 위장만으론 최신 탐지(JA4 + 행동분석, arXiv AUC 0.998)에 부족.
   그러나 크림은 아직 로그인 없이 `__NUXT_DATA__`가 열리는 수준이라 **엔터프라이즈급 탐지를
   전면 적용하진 않은 것으로 보임** — 지금은 할 만하되, 크림이 강화하면 군비경쟁.
4. **크림 리버싱 공개 자료는 거의 없다** — 한국·해외 모두. 우리가 오히려 앞서 있음.
   참고 가능한 유일한 오픈소스 = `missiletoe/kream`(코드 직접 열람 필요).

---

## 1. TLS 지문 위장 (우리 fetch 계층)

| 도구 | ★ | 용도 | URL |
|---|---|---|---|
| **curl_cffi** (lexiforest) | 6.1k | 크림봇이 **이미 사용**. Chrome/Safari TLS·JA3·HTTP2 위장. 최신 계승판 | https://github.com/lexiforest/curl_cffi |
| tls-client (bogdanfinn) | 1.7k | Go uTLS 기반, 요청마다 지문 프로필. curl_cffi 막힐 때 대체 채널 | https://github.com/bogdanfinn/tls-client |
| primp (deedy5) | 563 | Rust rquest 코어, 더 가볍고 빠름. 지문 갱신 빠름 | https://github.com/deedy5/primp |

## 2. anti-bot 우회 (브라우저/드라이버 — escalation 최종 티어)

| 도구 | ★ | 용도 | URL |
|---|---|---|---|
| **Scrapling** (D4Vinci) | ~65k+ (2026 트렌딩 #1) | 적응형 스크레이핑 + StealthyFetcher(Cloudflare/CDP leak 차단) + **파서 자동 추적** | https://github.com/D4Vinci/Scrapling |
| camoufox (daijro) | 10.4k | Firefox C++ 패치 anti-detect, WebGL/캔버스/WebRTC 지문 스푸핑 | https://github.com/daijro/camoufox |
| botasaurus (omkarcloud) | 5.6k | Cloudflare/DataDome 우회 올인원 + 사람같은 마우스 | https://github.com/omkarcloud/botasaurus |
| nodriver (ultrafunkamsterdam) | 4.6k | undetected-chromedriver 후속, CDP 직접 제어 | https://github.com/ultrafunkamsterdam/nodriver |

> ⚠️ **FlareSolverr(★14.9k)는 2025-12 archived, "Cloudflare 대응 불가로 영구 미작동" — 쓰지 말 것.**
> 크림봇 규칙: camoufox는 `kream-search-in-search` 3단계까지 다 해보고도 막혔을 때만.

## 3. 프록시 · IP 로테이션 (대량 조회의 핵심)

| 도구 | ★ | 용도 | URL |
|---|---|---|---|
| **requests-ip-rotator** (Ge0rg3) | — | **AWS API Gateway로 IP 로테이션** — 거의 무료 대량 IP | https://github.com/Ge0rg3/requests-ip-rotator |
| **FireProx** (ustayready) | — | 같은 원리(AWS API Gateway 프록시), 오펜시브 보안 도구 | https://github.com/ustayready/fireprox |
| proxy_pool (jhao104) | 23.5k | 무료 프록시 풀 수집·검증·API. 안정성 낮음(무료 소스) | https://github.com/jhao104/proxy_pool |
| proxybroker2 (bluet) | 983 | 비동기 프록시 탐색+검증+로테이션. 헬스체크 패턴 재사용 | https://github.com/bluet/proxybroker2 |

## 4. 리셀 스크래퍼 참고 (파싱 패턴)

| 도구 | ★ | 용도 | URL |
|---|---|---|---|
| scrapfly-scrapers | 1.0k | `stockx/`·`goat/` 폴더 = "hidden JSON 추출" 프로덕션 파서. 크림 NUXT 파싱과 유사 | https://github.com/scrapfly/scrapfly-scrapers |
| **missiletoe/kream** (한국) | — | 실제 크림 거래량·검색 크롤러. **코드 직접 열람 권장**(gh/clone) | https://github.com/missiletoe/kream |

> KREAM 전용 유명 오픈소스는 해외·한국 모두 **전무**. 크몽/Threads에 상업 매크로(보관판매·
> 가격유지) 시장은 있으나 엔드포인트 전부 비공개. Apify `jy-labs/kream-scraper`는 Puppeteer+DOM.

## 5. 심층 기술 문서 · 논문 (탐지 원리 = "상대가 어떻게 잡나")

**TLS/HTTP 지문**
- 🏆 JA3/JA4 TLS Fingerprinting 가이드 (ScrapFly) — 왜 헤더만 바꾸면 안 되는지(TLS+HTTP2+헤더
  3중 일관성) https://scrapfly.io/blog/posts/ja3-ja4-tls-fingerprinting-guide-to-detection-and-evasion
- 🏆 JA4+ 공식 스펙 (FoxIO) — 크림/Akamai가 실제 해시하는 필드의 1차 소스
  https://github.com/FoxIO-LLC/ja4/blob/main/technical_details/README.md
- HTTP/2·HTTP/3 지문 (ScrapFly) https://scrapfly.io/blog/posts/http2-http3-fingerprinting-guide
- Post-Quantum TLS로 기존 위장 라이브러리 노출 (ScrapFly, 최신) https://scrapfly.io/blog/posts/post-quantum-tls-bot-detection
- TLS Fingerprint 우회 (ZenRows) https://www.zenrows.com/blog/what-is-tls-fingerprint

**학술 논문 (탐지 한계선 정량)**
- 🏆 When Handshakes Tell the Truth — JA4 기반 봇탐지 AUC 0.998 (arXiv 2602.09606) https://arxiv.org/abs/2602.09606
- FP-Scanner — 지문 위장 "불일치" 역탐지 (USENIX Security 2018) https://www.usenix.org/conference/usenixsecurity18/presentation/vastel
- TLS 지문 feature expansion + 유사도 매핑 (arXiv 2410.03817) https://arxiv.org/abs/2410.03817

**anti-bot 벤더/리버싱**
- DataDome 우회가 왜 어려운가 (DataDome 자사) — 쿠키가 IP에 바인딩·서명되는 구조 https://datadome.co/bot-management-protection/how-to-bypass-datadome/
- Top 7 anti-scraping 기법 (Bright Data) — IP평판·TLS·지문·행동 4계층 체크리스트 https://brightdata.com/blog/web-data/anti-scraping-techniques
- Akamai v3 sensor_data 리버싱 (glizzykingdreko) https://medium.com/@glizzykingdreko/akamai-v3-sensor-data-deep-dive-into-encryption-decryption-and-bypass-tools-da0adad2a784
- Edioff/akamai-analysis — Akamai Bot Manager 파이프라인 문서화 https://github.com/Edioff/akamai-analysis

## 6. 배울 개발자 · 커뮤니티

- **Antoine Vastel** (@xopek59) — DataDome 출신, 봇탐지 연구. https://x.com/xopek59 · https://antoinevastel.com/
- **0xdevalias** (@_devalias) — Cloudflare/Akamai 우회·JS 난독화 해제 노트 https://x.com/_devalias
- **Mike Felch** (@ustayready) — FireProx 제작(AWS IP 로테이션) https://x.com/ustayready
- Evan Sangaline (Intoli) — "Chrome Headless Undetectable" 원저 https://intoli.com/blog/making-chrome-headless-undetectable/
- Nikolai Tschacher (incolumitas) — 봇탐지 서비스 리버싱, BotOrNot https://incolumitas.com/
- ScrapFly 블로그 (scrapecrow) — DataDome/Akamai/JA3 실무 시리즈 https://scrapfly.io/blog
- **John Watson Rooney** — YouTube 웹스크래핑 실전(10만+), curl_cffi/프록시 튜토 https://www.youtube.com/c/JohnWatsonRooney
- r/webscraping (Reddit) https://reddit.com/r/webscraping
- HN: residential proxy 생태계 https://news.ycombinator.com/item?id=48864252 · 스크래퍼 방어 https://news.ycombinator.com/item?id=45935729

## 7. 한국 크림 특화 (참고)

- `missiletoe/kream` (§4) — 유일한 실제 크림 크롤러 오픈소스.
- 크몽/숨고 gig — 보관판매·가격유지 매크로 상업 판매(엔드포인트 비공개): kmong.com/gig/489690 등.
- Threads @konggozilakong — 보관판매 자동 매크로(로그인→items.txt→전자동) — Phase B 참고.
- **못 찾음**: 크림 `__NUXT_DATA__`/webDid/429 리버싱 한국어 심층 글 = 전무. 우리가 앞섬.

## 8. 크림봇 적용 판단 (다음 설계 반영)

1. **fetch 계층**: curl_cffi 유지 + **IP마다 지문 로테이션**(Chrome/Firefox/Safari) — `kream-search-in-search` 확장.
2. **IP 분산**: 먼저 **AWS API Gateway(requests-ip-rotator) 무료 경로를 실측**(크림이 AWS IP 차단하는지).
   통하면 무료 대량, 막히면 레지덴셜 프록시(월요금)로.
3. **자동 감속**: 429/403 감지 시 해당 IP 격리 + 전역 회로차단 (코덱스 KreamAccountActor 설계와 결합).
4. **탐지 대비**: 크림이 지금은 공개 파싱 수준이나, 강화 시 HTTP/2 지문 일관성 → camoufox 티어 준비.
5. **파서 견고성**: Scrapling의 적응형 셀렉터 개념 참고(크림 구조 변경 자동 추적) — 유지보수 절감.
6. **검증 필수**: 위 전부 "실측"으로. 특히 AWS IP가 크림에 통하는지 = 프로젝트 방향 좌우.

---

## 9. 정밀 조사 결과 (ScrapFly · Vastel · John Watson Rooney 3소스 정독)

### 세 소스 공통 = 확정 원칙
- **레이어 간 일관성이 전부**: TLS(JA3/JA4) + HTTP2(SETTINGS·pseudo-header 순서) + UA + 헤더가
  **같은 브라우저 버전 조합**이어야 한다. Vastel 원문: *"한 가지를 거짓말하면 나머지를
  일관되게 거짓말하기 어렵다."* 탐지의 근본 = 이 불일치를 잡는 것.
- **curl_cffi `impersonate` 프로파일을 통째로 써라. 개별 헤더·HTTP2 손대지 마라** — 손대는
  순간 내부 일관성이 깨져 오히려 잡힌다(우리가 이미 맞게 하고 있음, ScrapFly 원문 확인).
- **JS 챌린지는 curl_cffi 범위 밖** — 걸리면 지문 정교화로 삽질 말고 camoufox 에스컬레이션
  (스킬 4단계). 단 크림은 로그인 없이 데이터가 열려 지금은 해당 안 될 가능성.

### 즉시 실행 후보 (전부 실측 후)
1. **`src/crawlers/kream.py:245` `impersonate="safari17_0"` → 최신(`safari260`/`chrome131+`)**
   — safari17_0은 PQ(양자내성) 이전 구형이라 2026년 소수파로 튄다. **그냥 바꾸지 말고
   지문 덤프 + 크림 반응 비교 → `live-tester` 전후 검증 필수.**
2. **UA 하드코딩 여부 점검** — 별도 지정돼 있으면 impersonate 프로파일과 자동 동기화.
3. **IP 로테이션 시 지문도 세트로** — "고정 지문 + 다중 IP"는 그 자체가 탐지 신호.
   IP풀 × 지문풀 페어링 순환(Vastel·ScrapFly 공통 지적).
4. **세션 워밍** — 콜드 API 직격 = "쿠키/Referer 흔적 없음" 모순. 홈→카테고리→상세 진입
   (`kream-search-in-search`에 이미 있음, 크림 조회에도 적용).
5. **요청 지터 + 순서 섞기 + 시간대 조절** — 균일 간격·정렬 ID 순회·24h 무휴 = 로봇 신호.
6. **쿠키 갱신 전략(JWR)** — webDid 쿠키가 막히면 브라우저(Playwright)로 새 쿠키만 따서
   curl_cffi 대량 조회로 넘긴다(무거운 브라우저는 쿠키 딸 때 1회, 대량은 가벼운 HTTP).
7. **백엔드 API + offset/limit(JWR)** — 이미 우리 방식. limit 상한 최대화로 요청 수 절감.

### 근본 한계 (정직 — 지문으로 못 넘는 것)
- **그래프 기반 상관관계**(Vastel/Castle): 지문이 완벽해도 여러 IP/세션이 "카탈로그 전수
  스캔"이라는 **동일 목적으로 수렴하는 패턴**은 그래프 분석으로 잡힐 수 있다. 지문 문제가
  아니라 **행위 패턴** 문제 → 지터·분산·시간대로 **완화만** 가능, 완전 회피 불가.
- **공개 도구 지문은 알려진 IOC일 수 있음** — curl_cffi impersonate가 오픈소스라 고급 방어
  (Castle급)엔 등록됐을 가능성(추정).
- **크림이 실제 이 정도 탐지를 쓰는지 미확인** — 라이브 관측이 답. 현재 크림은 로그인 없이
  열리니 엔터프라이즈급은 아닌 것으로 보이나, 확정은 실측.

### 다음 실측 2개 (진행의 첫 스텝, 방향 좌우)
1. **AWS API Gateway IP가 크림에 통하나** → 무료 대량 vs 프록시 비용.
2. **safari17_0 → 최신 지문 교체 전후 크림 반응** → 지문 최신화 효과.

> ⚠️ 이 두 실측은 **크림 실계정과 무관한 익명 조회 테스트**지만, 크림 실서버를 건드리므로
> 보수적 소량·딜레이로. 실행 전 사장 GO + `live-tester` 경유.

---

## 10. 실측 결과 (2026-07-24, 익명 소량 진단 — 사장 GO)

### 웹 페이지 (`scripts/probe_kream_detection.py`)
- `safari17_0` / `safari18_0`: **status 200**, 824KB, `__NUXT_DATA__` 있음, challenge **없음**, webDid 쿠키 수신.
- **지문 없는 일반 TLS(httpx): status 500 · 빈 응답** → **크림이 TLS 지문을 검사한다**(curl_cffi 필수, 우리 통과 중).
- 0.5초 간격 3연속: **셋 다 200** → rate limit 느슨.
- 지문 버전(17_0 vs 18_0) 차이 없음 → **크림은 지문 버전을 세밀히 안 봄**(safari17_0 교체 시급하지 않음).

### API + rate limit (`scripts/probe_kream_api_ratelimit.py`)
- `options/display` API가 **익명 작동**: `KreamCrawler.get_sell_prices()`로 로그인 없이 사이즈별
  시세(sell_now / last_sale / bid_count) 수집됨. 추가 인증 불필요(webDid + 고정 시크릿 헤더로 충분).
- **rate limit: 20개 상품 연속 조회 → 19 성공 / 1 빈값**(1개는 단일사이즈 상품 특성, rate limit 아님).
  **연속실패 0, 안 막힘.** 간격 2~3초(봇 내부 `_random_delay`).

### 판정
- **크림 = 엔터프라이즈급 anti-bot 아님.** TLS 지문만 검사(우리 이미 통과), JS 챌린지 없음, rate limit 느슨.
- **프록시 없이 한 IP로 상당량 조회 가능.** 특히 거래량 있는 상품(1,666개)은 한 IP로 충분할 가능성.
- **프록시/AWS IP 분산 = 47k 전수 초대량 또는 속도 극대화를 원할 때만** 필요(지금은 선택).

### 미측정 (정직 — 다음에 볼 것)
- 수백~수천 누적 조회의 IP 임계 (20개는 여전히 소량).
- 간격 <0.5초로 더 몰아쳤을 때.
- 장시간 반복 시 그래프 기반 패턴 탐지(Vastel §9 한계).
