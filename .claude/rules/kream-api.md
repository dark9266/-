---
paths:
  - "src/crawlers/kream.py"
  - "src/kream_realtime/**/*.py"
  - "src/adapters/kream_*.py"
  - "src/tier2_monitor.py"
---

# 크림 API 규칙 (실계정 — 호출 하나가 BAN 리스크다)

> 상시 로드 아님. 위 `paths` 파일을 열 때만 들어온다.

## 🔴 실계정이다
크림 계정은 사장 **실계정**이다. 호출량 상한과 딜레이를 엄격히 지킨다.
`kream_live_call_guard` 훅이 배치 라이브 실행(`--dry-run` 없이)·레거시 우회로·`probe_kream*`·
안전망 해제 env 를 **물리 차단**한다. 해제 = `KREAM_LIVE_BATCH_GO=1` (사장 GO 사안).

## 엔드포인트 (GET 전용)
```
/api/p/e/search/products       — 키워드 검색 (sort=date, page, per_page)
/api/p/e/products/{id}         — 상품 상세
/api/p/options/display?product_id={id}  — 사이즈별 시세
/api/p/e/products/{id}/sales   — 거래 내역
```

## 호출 최소화 (하드 규칙)
- **NUXT 우선 → API 1회 fallback.** cold 는 경량 `options/display` API 만 사용
- **47k 전체 시세 갱신 절대 금지** — hot tier 만 `price_refresher`, cold 는 연속 스캔 시 즉석 조회
- 매칭된 후보만 호출한다 (푸시 파이프라인의 존재 이유)
- 일일 캡 `KREAM_DAILY_CAP` 이하 유지. `kream_budget` 하드 캡 10k · `tier2_monitor` 폴링 딜레이 ·
  `kream_delta_watcher` rate limit — **느슨하게 바꾸지 말 것**

## 에러 대응
- `429` → 30초 대기 후 재시도
- `500` → 서버 장애. 재시도 후 포기
- `404` → **에러 아님**. 거래량 0 신규 상품
- `5xx` 서킷 → 30분 냉각 후 자동 재개. `403`/`429` 는 manual 전용

## 파싱
`src/crawlers/kream.py` — curl_cffi Safari 핑거프린트 + Nuxt `__NUXT_DATA__` 파싱
(sizes, prices, trade volume). devalue flat 배열 구조.

## 수익 계산은 반드시 실시간
로컬 DB 시세·거래량으로 수익 계산 **금지**. 반드시 실시간 `sell_now`(즉시판매가) 기준.
`buy_now` 는 허수다.

## 모듈
- `src/kream_realtime/collector.py` — 신규 상품 자동 수집 (6시간 주기)
- `src/kream_realtime/price_refresher.py` — hot 전용 시세 갱신. 3회 연속 실패 → cold 강등 (`refresh_fail_count`). **2026-05-01 토글 OFF 상태**
- `src/kream_realtime/volume_spike_detector.py` — 거래량 급등 감지 (2배 → hot 승격). volume_7d + refresh_tier + scan_priority 동시 갱신
- `src/crawlers/kream_delta_client.py` — 델타 워처용 실크림 클라이언트. `kream_delta_watcher` 가 기대하는 `KreamDeltaClientProtocol`(fetch_light / get_snapshot) 구현
- `src/tier2_monitor.py` — **축 ② 유지(폐기 X)**. watchlist 실시간 폴링, sell_now 기준 수익, 5배 안전망(오매칭 방어)
