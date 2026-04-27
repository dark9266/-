# Chrome 확장 프로그램 — 무신사 결제 페이지 100% Catch (Phase 1)

**Date**: 2026-04-27
**Status**: Spec — 사용자 승인 대기 → writing-plans 진입 예정
**Owner**: 크림봇 / UI 봇 트랙 통합
**관련 메모**: `project_size_trade_margin_progress.md`, `project_ui_bot_track.md`

---

## 1. 목적

무신사 **결제 페이지에서만 노출되는** 카드 즉시할인·시한 쿠폰·자동 매칭 쿠폰·실측 결제가를 Chrome 확장이 100% catch → 봇 백엔드(FastAPI) → SQLite 저장 → `profit_calculator`가 매칭되는 catch row를 **추정이 아니라 그대로 정확 계산에 사용**.

**PDP는 범위 외 — 봇 어댑터(`musinsa_httpx.py`)가 이미 catch 중**:
- `__NEXT_DATA__.pageProps.meta.data` 의 `goodsNm/normalPrice/salePrice/couponPrice/isLimitedCoupon` 은 모든 사용자 동일 → SSR fetch로 봇이 이미 수집 중. PDP catch는 redundant.
- 색상/사이즈 옵션은 별도 API `/api2/goods/{id}/options` 호출 → 봇 어댑터 이미 호출 중.
- 따라서 Chrome 확장의 PDP scan은 폐기. 결제 페이지 전용.

**현재 한계 (확장이 메우는 정보)**:
- 카드 즉시할인 7개 = 결제 페이지에서 사용자가 카드 toggle 시에만 노출
- 사용자별 등급 쿠폰 = 로그인 + 결제 진입 시점에만 적용
- 시한 쿠폰 / 자동 매칭 쿠폰 적용 후 결제가 = 결제 페이지에서만
- 결과: 봇은 위 정보 catch 불가능 → 보수 추정. 검증 6건 중 4건이 +10k↑ 갭.

**개선 효과**:
- 사용자 결제 페이지 진입 시 확장이 위 정보 100% catch → `final_price` 그대로 사용 → 정확 계산 → 추가 수익 발굴 ↑.
- catch 데이터 없으면 현재 보수 추정 그대로 (거짓 알림 0 유지).

**불변 원칙 (이 spec이 약화시키지 않음)**:
- **소싱처 제품의 사이즈·컬러 매칭 정확성이 여전히 1순위.** catch 데이터 도입은 매칭 후단(가격 계산) 보강일 뿐, 매칭 단계 (모델번호·색상·사이즈 동일성 검증) 자체는 그대로. 매칭이 틀리면 catch 데이터도 무의미.

---

## 2. 비목적 (Out of Scope)

- ❌ 자동 결제 / 자동 카트 add / 자동 쿠폰 발급 — 매크로 = BAN 위험. 영구 X.
- ❌ 카드 자동 toggle 시뮬레이션 — 7개 카드 catch는 사용자 본인 클릭으로만.
- ❌ **PDP scan** — 봇 어댑터가 이미 동일 정보 수집 중. 확장은 결제 페이지 전용.
- ❌ 무신사 외 소싱처 — Phase 1은 무신사 단독. 골격은 22 소싱처 확장 가능하게 짜되 구현은 Phase 2 (나이키), Phase 3 (29cm), Phase 4 (그 외) 점진.
- ❌ 모바일 — 메모 결정사항 (데스크톱 Chrome만).
- ❌ Firefox/Safari — Chrome 확장 단독 (Manifest V3).

---

## 3. 우선순위 (확정)

| Phase | 대상 | 일정 | 핵심 |
|---|---|---|---|
| 1 | **무신사** | 즉시 (~1주) | 골격 + manual 검증 |
| 2 | **나이키 공홈** | 내일 할인 D-day → Phase 1 골격 위 plug-in 압축본 우선 가동 | 압축 룰 우선, 검증 후 보강 |
| 3 | **29cm** | Phase 2 검증 후 | plug-in 추가 |
| 4 | **그 외 22 소싱처** | 점진 (각 1~2일, 4~6주) | plug-in 추가 |

---

## 4. 아키텍처

```
┌─────────────────── Chrome 확장 (Manifest V3) ───────────────────┐
│                                                                  │
│  ┌──────────────────┐    ┌──────────────────────┐                │
│  │ Content Script   │───▶│ Background Service   │───▶ FastAPI    │
│  │ (도메인별 plug-in) │    │ Worker               │    POST       │
│  │ - musinsa.js     │    │ - 메시지 라우팅       │   /api/coupon │
│  │ - nike.js (P2)   │    │ - 백엔드 push        │   /catch      │
│  │ - 29cm.js (P3)   │    │ - 상태 캐시          │                │
│  └──────────────────┘    └──────────────────────┘                │
│         │                          ▲                             │
│         ▼                          │                             │
│  ┌──────────────────┐    ┌──────────────────────┐                │
│  │ DOM/네트워크      │    │ Popup UI             │                │
│  │ 100% scan        │    │ - 카드 toggle 가이드  │                │
│  │ - 카드 즉시할인   │    │ - catch 카운터       │                │
│  │ - 시한 쿠폰      │    │ - on/off 토글        │                │
│  │ - 자동 매칭 쿠폰 │    └──────────────────────┘                │
│  │ - 페이별 결제가  │                                            │
│  │ - 적립금 차감    │                                            │
│  │ - 등급할인       │                                            │
│  └──────────────────┘                                            │
└──────────────────────────────────────────────────────────────────┘

                    ▼ HTTP POST (localhost:8000)

┌──────────── 봇 백엔드 (FastAPI, UI 봇 Phase A worktree) ────────┐
│                                                                  │
│  /api/coupon/catch (POST)                                        │
│       │                                                          │
│       ▼                                                          │
│  Validation + 매칭 키 생성 (sourcing+native_id+color)            │
│       │                                                          │
│       ▼                                                          │
│  SQLite `coupon_catches` insert (또는 update on conflict)        │
│       │                                                          │
│       ▼                                                          │
│  `profit_calculator` lookup hook                                 │
│       - 매칭 row 있음 → catch payload 반영                       │
│       - 매칭 row 없음 → 현재 보수 추정 그대로                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## 5. Catch 항목 — 데이터로 정확 계산 (추정 X)

**원칙**: 확장이 잡은 항목은 모두 결제 페이지에 노출된 실제 값. 봇은 이를 추정·평균·보수 처리 없이 그대로 수익 계산식에 대입.

### 5.0 매칭 검증 (catch 적용 전 필수 게이트)
- catch row 적용 전, 봇이 매칭한 상품의 (모델번호·색상·사이즈)가 catch 시점 페이지의 (native_id·color_code·size_code)와 일치하는지 검증.
- 불일치 시 catch row 미적용 → 보수 추정 그대로 (거짓 알림 방어).
- 사이즈는 결제 페이지에 노출되면 함께 저장 (§6.2 size_code 필드).

### 5.1 결제 페이지 전용 (PDP는 범위 외)

PDP scan 폐기 — 봇 어댑터(`musinsa_httpx.py`)가 `__NEXT_DATA__` SSR JSON + `/api2/goods/{id}/options` API 로 이미 동일 정보 수집 중.

Chrome 확장은 **결제 페이지에서만 노출되는 정보**만 catch:
- 카드 즉시할인 7개 (사용자 카드 toggle 시 결제가 변동 catch)
- 시한 쿠폰 적용 후 결제가
- 자동 매칭 쿠폰 적용 후 결제가
- 페이 옵션별 결제가 (무신사페이/토스페이/카카오페이)
- 적립금 차감
- 최종 결제가 (`final_price`)

### 5.2 결제 페이지 (`https://www.musinsa.com/order/...` — manifest V3 host_permissions)
- 카드 즉시할인 7개 (사용자가 카드 toggle 시 변동되는 결제가 catch)
- 시한 쿠폰 적용 후 결제가
- 자동 매칭 쿠폰 적용 후 결제가
- 페이 옵션별 결제가 (무신사페이 / 토스페이 / 카카오페이)
- 적립금 사용 여부 (`isRestictedUsePoint` 등)
- 최종 결제가

### 5.3 Popup UI (확장 아이콘 클릭 시)
- 현재 페이지에서 catch한 항목 카운터
- 카드 toggle 가이드: "남은 카드 N개 toggle 해주세요" (BC/농협/삼성/롯데/우리/카카오페이머니/계좌 중 미catch 카드 list)
- on/off 토글 (확장 일시정지)

---

## 6. 데이터 흐름 + 저장 키

### 6.1 매칭 키 (확정)
- **(sourcing, native_id, color_code, size_code)** — 색상·사이즈까지 정확 매칭된 catch만 저장.
- 색상 또는 사이즈 매칭 실패 = catch 폐기 (평균/추정 fallback X — "실제 매칭이 중요" 사용자 결정사항).
- 사이즈가 페이지에 노출되지 않는 경우 (PDP 단계 일부) `size_code='NONE'` 저장 → 봇 적용 시 동일 native_id+color_code의 NONE row를 size 무관 폴백으로 사용 가능. 단 결제 페이지 catch는 사이즈 노출 시 반드시 저장.
- 무신사 예: `sourcing="musinsa", native_id="5874434", color_code="BLACK", size_code="270"`

### 6.2 SQLite 스키마
```sql
CREATE TABLE coupon_catches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sourcing TEXT NOT NULL,           -- 'musinsa' | 'nike' | '29cm' | ...
    native_id TEXT NOT NULL,          -- goodsNo / styleColor / ...
    color_code TEXT NOT NULL,         -- 색상 코드 (없으면 'NONE')
    size_code TEXT NOT NULL,          -- 사이즈 코드 (페이지 미노출 시 'NONE')
    page_type TEXT NOT NULL,          -- 'pdp' | 'checkout'
    payload TEXT NOT NULL,            -- JSON (catch 항목 raw, 결제가/할인 실측값)
    captured_at REAL NOT NULL,        -- epoch float (decision_log 일관성)
    UNIQUE(sourcing, native_id, color_code, size_code, page_type)
);
CREATE INDEX idx_coupon_lookup
    ON coupon_catches(sourcing, native_id, color_code, size_code);
```

### 6.3 POST endpoint
```
POST /api/coupon/catch
Authorization: Bearer <local_token>  -- localhost 한정 + 토큰
Content-Type: application/json

{
  "sourcing": "musinsa",
  "native_id": "5874434",
  "color_code": "BLACK",
  "page_type": "checkout",
  "payload": {
    "card_discounts": [
      {"card": "BC", "pay": "musinsa_pay", "discount": 4000, "min_amount": 100000}
    ],
    "timed_coupons": [{"name": "신상 5%", "discount": 5000, "expires_at": "..."}],
    "auto_matched_coupons": [{"name": "여름맞이 8천", "discount": 8000}],
    "pay_prices": {"musinsa_pay": 175000, "toss_pay": 174000, ...},
    "grade_discount": {"level": "bronze", "rate": 0.01},
    "final_price": 174000
  },
  "captured_at": 1777252745.0
}
```

### 6.4 봇 통합 (profit_calculator hook) — 데이터 그대로 적용 (추정 X)
- 매칭 후보 결정 시점에서 `coupon_catches` lookup (sourcing + native_id + color_code + size_code).
- 매칭 row 있음 (page_type='checkout') → payload의 **실측 결제가**(`final_price`)를 그대로 매입가로 사용. 카드 즉시할인·시한 쿠폰·자동 매칭 쿠폰을 따로 합산하지 않음 (이미 final_price에 반영됨).
- 매칭 row 없음 또는 page_type='pdp' → 현재 보수 추정 그대로 (변경 X). PDP row는 본 spec 범위에서 무시 (코드/스키마는 미래 use case 위해 보존).
- catch 만료 정책: `captured_at + 7일` 지난 row는 lookup에서 제외. 카드 즉시할인 룰 변경 대비.

---

## 7. 보안 + 위험

### 7.1 위험 평가 = 0
- 매크로 X / 자동 카트 add X / 자동 결제 X
- 사용자 정상 쇼핑 행동 catch만 → BAN 사유 없음
- 무신사가 확장 존재 자체 detect 불가능 (DOM/네트워크 read-only)

### 7.2 인증
- localhost 한정 (FastAPI host=`127.0.0.1`, port=8000)
- 로컬 토큰 (`.env` `EXTENSION_API_TOKEN`) → 확장 manifest에 storage로 1회 입력 후 모든 POST에 Bearer 첨부
- CORS: localhost만 허용

### 7.3 사용자 데이터
- 카드번호·CVC·비밀번호 등 결제 민감 정보 catch X — 결제가/할인 금액·쿠폰명만
- 적립금 잔액·등급 정보는 catch (메모 사용자 정보 config 등록 대상과 일치)

---

## 8. 22 소싱처 확장 골격

### 8.1 Content Script 도메인별 plug-in 구조
```
extension/
├── manifest.json           # host_permissions 도메인 list
├── background.js           # 메시지 라우팅 + FastAPI POST (공통)
├── popup/                  # 카드 toggle 가이드 + 카운터 (공통 UI)
│   ├── popup.html
│   ├── popup.js
│   └── popup.css
├── content/                # 도메인별 plug-in
│   ├── musinsa.js          # Phase 1
│   ├── nike.js             # Phase 2 (D-day)
│   ├── 29cm.js             # Phase 3
│   └── ...                 # Phase 4 점진
└── lib/
    ├── extractor.js        # DOM/네트워크 scan 공통 헬퍼
    └── api.js              # FastAPI POST 공통
```

### 8.2 Plug-in 인터페이스 (모든 소싱처 공통)
```js
// content/<source>.js
export default {
  matches: [/* host_permissions URL 패턴 */],
  detectPageType(url) {/* 'pdp' | 'checkout' | null */},
  extractNativeId(dom) {/* string */},
  extractColorCode(dom) {/* string */},
  scanPdp(dom) {/* { coupons, grade_discount, ... } */},
  scanCheckout(dom) {/* { card_discounts, timed_coupons, ... } */},
};
```

새 소싱처 추가 = plug-in 1개 작성 (background/popup/lib 수정 X).

---

## 9. 테스트 전략

### 9.1 Phase 1 (무신사) 검증
1. **단위 테스트** (확장 측): plug-in `extractNativeId` / `scanCheckout` mock DOM input 검증
2. **단위 테스트** (백엔드 측): `/api/coupon/catch` POST validation, SQLite insert/upsert, profit_calculator lookup
3. **manual e2e** (사용자 PC):
   - 확장 설치 → 무신사 PDP 진입 → catch row 1건 확인
   - 결제 페이지 진입 → 카드 7개 toggle → catch row 7건 확인
   - 봇 알림에 catch 데이터 반영 확인 (decision_log + Discord embed)
4. **회귀 테스트**: 기존 6건 검증 케이스에 catch 데이터 적용 시 추정 가격 변동 확인 (메모 검증 결과 표 vs 새 결과)

### 9.2 Phase 2~4 검증
- Phase 1 골격에 plug-in 추가 시 plug-in 단위 테스트 + manual e2e 1건만

---

## 10. 의존성 + 파일

### 10.1 신규 (확장)
- `extension/manifest.json` (Manifest V3, host_permissions: musinsa.com)
- `extension/background.js`
- `extension/popup/{popup.html,js,css}`
- `extension/content/musinsa.js`
- `extension/lib/{extractor.js,api.js}`

### 10.2 신규 (백엔드, Phase A worktree에 추가)
- `src/api/routers/coupon.py` (POST /api/coupon/catch)
- `src/coupon_store.py` (coupon_catches CRUD + 만료 정책)
- `src/profit_calculator_coupon_hook.py` (또는 profit_calculator.py 확장)

### 10.3 수정
- `src/profit_calculator.py` — coupon_catches lookup hook 추가
- `src/models/database.py` — coupon_catches 테이블 마이그레이션
- `.env` — `EXTENSION_API_TOKEN` 추가
- `src/api/server.py` — coupon router 등록

### 10.4 테스트
- `tests/test_coupon_router.py`
- `tests/test_coupon_store.py`
- `tests/test_profit_calculator_with_catch.py`
- `extension/tests/musinsa.test.js` (jest 또는 vitest)

---

## 11. 미해결 질문 (writing-plans 단계로 이월)

- `EXTENSION_API_TOKEN` 생성 + Chrome 확장 설치 시 1회 입력 UX 디테일
- 무신사 결제 페이지 정확한 URL 패턴 (`/order/...` 변형) — 사용자 PC에서 확인
- 카드 toggle 시 결제가 변동을 어느 DOM/네트워크 시점에 catch 할지 — 무신사 결제 페이지 분석 단계에서 확정
- 색상 코드가 native_id에 이미 포함된 경우 (예: 나이키 `DV1748-001`) color_code 별도 필드 처리 방식
- 사이즈 코드 형식 표준화 — 무신사 `270` vs 크림 `270mm` vs 의류 `M/L` 등 정규화 룰 (소싱처별 매칭 정확성 1순위 유지)
- PDP catch (size_code=NONE) 폴백을 결제 페이지 catch가 1건이라도 있을 때 끄는 정책 (정확성 우선) vs 폭넓게 쓰는 정책 (커버리지 우선) 중 선택

---

## 12. 다음 단계

1. **사용자 spec 리뷰** — 본 파일 검토 후 변경 요청 또는 승인
2. **`superpowers:writing-plans` 호출** — 단계별 구현 plan 작성 (Phase 1 무신사 단독)
3. **TDD 구현 진입** — 백엔드 → 확장 → manual e2e 순서

---

## Appendix — 검증 6건 (메모 인용, 이 spec 효과 측정 baseline)

| 상품 | 봇 보수 (현재) | 실제 결제 | 갭 | catch 적용 후 예상 |
|---|---:|---:|---:|---|
| adidas EVO SL | ~207k | 189,900 | +17k | catch 적용 시 ~189k 근접 (정확도 ↑) |
| adidas KD1517 | 142,400 | 142,400 | 0 ✅ | 변동 없음 (이미 정확) |
| Nike Jordan 1 | ~152k | 140,990 | +11k | catch 적용 시 ~141k 근접 |
| Nike AWF | ~124k | 122,900 | 1.1k ✅ | 변동 없음 |
| Nike 샥스 Z W (타투) | ~136k | 126,300 | +10k | catch 적용 시 ~126k 근접 |
| Nike 샥스 Z W (블랙) | ~136k | 135,000 | 1.5k ✅ | 변동 없음 |

**핵심**: catch 데이터로 +10k↑ 갭 4건이 정확화 → 거짓 알림 방어 유지하면서 누락 발굴 회복.
