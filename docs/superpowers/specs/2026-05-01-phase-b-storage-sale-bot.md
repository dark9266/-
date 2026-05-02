# Phase B — 보관판매 자동등록 (슬롯 캐치 봇팅)

**작성일**: 2026-05-01
**상태**: api-prober endpoint 확정 (1페이지 + 2페이지 둘 다). 사용자 실 시도 캡처 항목 4개 미확정.

## Context

### 문제

크림 보관판매 = **창고 용량 제한**. 평소 사용자 클릭 → "창고 가득" → 막힘. 가끔 슬롯 풀림 → 그 순간 캐치해야 등록 가능. 한정판 봇팅과 동일 패턴.

기존 방식 = 녹스(안드로이드 에뮬) + 화면녹화 매크로 + 마우스 무지성 반복. 사용자 직접 운영. **수많은 다른 보판봇 사용자 존재** → 시장 검증된 행위 (BAN 위험 낮음).

### 의도

1. 사용자 수동 시작/중지
2. 사전 입력: 상품 + 사이즈 + 수량
3. 1페이지 자동 시도 (무한 폴링)
4. 슬롯 잡힘 → 2페이지 자동 등록 컨펌

## CLAUDE.md POST 예외 적용

본 spec 의 POST 호출은 CLAUDE.md "POST 금지 예외 (벌크 계정 한정 = 본인 거래 자동화)" 항목에 명시된 영역. 안전장치 8종 (이전 메모 폐기됨, 본 spec 5장 재정의) 필수. 외부 사용자/메인 계정 영향 X.

## API Endpoints (api-prober 확정)

### 1페이지 — 슬롯 진입 시도

```
POST https://api.kream.co.kr/api/seller/inventory/stock_request/review
```

**Request body**:
```json
{
  "items": [
    { "product_option": { "key": "265" }, "quantity": 1 }
  ]
}
```

**Response 분기**:
- 성공 (슬롯 확보): `{"id": "<review_id>", ...}` → 2페이지 진행
- 실패 (창고 가득): `{"code": 9999, "message": "..."}` → 재시도 루프

### 2페이지 — 등록 컨펌

```
POST https://api.kream.co.kr/api/seller/inventory/stock_request/confirm
```

**Request body**:
```json
{
  "review_id": "<1페이지 응답 id>",
  "return_address_id": 123,
  "return_shipping_memo": "",
  "delivery_method": "..."
}
```

### 인증

기존 `kream.py` 헤더 세트 재사용:
- `X-KREAM-DEVICE-ID: web;{webDid}`
- `X-KREAM-WEB-REQUEST-SECRET: kream-djscjsghdkd`
- `X-KREAM-API-VERSION`, `X-KREAM-CLIENT-DATETIME`, `X-KREAM-WEB-BUILD-VERSION`

추가 Bearer 토큰 불필요. **단 실 계정 로그인 세션 필수**.

## Pre-Implementation Discovery (사용자 액션 필요)

다음 4 항목은 사용자 실 시도 시 브라우저 개발자 도구 / mitmproxy 로 캡처. 이거 받기 전엔 워커 dry-run 만 가능, 실 호출 X.

| 항목 | 캡처 방법 |
|---|---|
| 로그인 endpoint | 크림 웹 로그인 시 Network 탭 — `/api/m/sessions` 또는 `/api/m/login` 정확 path + payload |
| product_option.key 포맷 | 실 상품 사이즈 선택 시 review POST body — "265" (mm) 인지 다른 형식인지 |
| delivery_method 허용값 | confirm POST body — "courier" / "convenience" 등 enum 값 |
| return_address_id | 사용자 등록 주소 ID (마이페이지에서 조회 가능) |

## Architecture

### 모듈 분리

```
src/storage_sale/
├── auth.py           # 크림 로그인 + 세션 쿠키 관리
├── slot_worker.py    # Phase B1 — 1페이지 무한 시도 루프
├── confirm_worker.py # Phase B3 — 2페이지 자동 등록
├── runner.py         # B1 → B3 파이프라인 + 시작/중지
└── models.py         # SlotRequest, ConfirmRequest 등 dataclass
```

### Phase A 의존성

- 봇팅 코어 = Phase A 와 분리. 지금 진행 가능.
- UI 메뉴 + 입력 폼 = Phase A 머지 후 (다음 세션 hybrid)
- 임시: CLI 스크립트 (`scripts/storage_sale_run.py`) 로 가동/중지 지원

## Phase B1 — 1페이지 슬롯 워커

### 동작 흐름

```
1. 사용자 시작 → SlotRequest 생성 (product_id, size, quantity)
2. 인증 세션 확보 (auth.get_session())
3. 무한 시도 루프 진입:
   - POST /stock_request/review
   - 응답 분기:
     a. 200 + {"id": review_id} → 슬롯 잡힘 → B3 트리거 + 루프 종료
     b. 200 + {"code": 9999} → 창고 가득 → backoff 후 재시도
     c. 401/403 → 세션 만료 → 재로그인 시도 (1회 한정), 실패 시 중단
     d. 429 → BAN 감지 → 즉시 중단 + Discord 알림
     e. 5xx → 서버 장애 → backoff 30초 후 재시도
   - 사용자 중지 신호 시 즉시 종료
```

### 안전장치 (5종)

1. **시도 빈도 제한**: 1초 인터벌 (사람 클릭 속도 모방)
2. **최대 시도 시간**: 기본 30분 (사용자 설정 가능)
3. **BAN 감지**: HTTP 429 → 즉시 중단
4. **세션 검증**: 로그인 만료 감지 → 1회 재시도 후 중단
5. **사용자 중지 우선**: 중지 신호 시 진행 중 호출 완료 후 종료

### 입력 spec

```python
@dataclass
class SlotRequest:
    product_id: str
    product_option_key: str  # 사이즈 (예: "265")
    quantity: int = 1
    max_duration_minutes: int = 30
    interval_seconds: float = 1.0
```

### 호출량 추정

- 시도 빈도 1초 → 60 호출/분
- 30분 가동 → 1,800 호출 (캡 1만의 18%)
- **사용자 가동 = 슬롯 잡히기 전까지** → 평균 누적 변동 큼

## Phase B3 — 2페이지 등록 컨펌 워커

### 동작 흐름

```
1. B1 review_id 수신
2. POST /stock_request/confirm with payload
3. 응답 분기:
   a. 200 + 성공 응답 → 등록 완료 → Discord 알림
   b. 4xx/5xx → 실패 → 재시도 1회 → 실패 시 사용자 알림 (수동 처리 필요)
```

### 안전장치 (3종)

6. **B3 단일 시도**: B1 과 달리 무한 시도 X (review_id 만료 전 1회만 자동 + 1 재시도)
7. **사용자 알림**: 성공/실패 모두 Discord push
8. **실패 fallback**: B3 실패 시 사용자가 마이페이지 "보관판매 진행 중" 메뉴에서 수동 마무리 가능 (review_id 살아있는 동안)

## UI 요구사항 (Phase A 머지 후)

- 사이드바 메뉴: `🎯 보관판매 자동등록`
- 입력 필드:
  - 상품 검색/선택 (모델번호 또는 크림 product_id)
  - 사이즈 (드롭다운)
  - 수량 (1~?)
  - 최대 시도 시간 (기본 30분)
- 컨트롤:
  - 시작 / 중지 버튼
  - 진행 상태 표시 (시도 횟수 / 마지막 응답 / 슬롯 캐치 상태)
- 알림:
  - 슬롯 캐치 시 디스코드 + UI 알림음
  - 등록 완료 시 디스코드 + UI 결과 표시

## 검증 단계

### Stage B-0 — Dry-Run (no actual POST)
- mock 응답으로 워커 로직 검증
- 시도 루프 / backoff / 종료 조건 단위 테스트

### Stage B-1 — Staging (실 계정, 1회 한정)
- 사용자 직접 가동
- 실 1페이지 POST 1회 → 응답 확인
- 사용자 동시 화면녹화/트래픽 캡처 → 미확정 4 항목 캡처

### Stage B-2 — Limited Live (10분 가동)
- 실 시도 10분 → 슬롯 캐치 시도
- 결과 = 캐치 성공 or 30 분 timeout

### Stage B-3 — Full Live
- 사용자 평소 패턴대로 가동/중지

## 명시 결정 (settled, 재논의 X)

1. **사용자 수동 시작/중지** — 자동 X
2. **끝까지 자동** — B1 슬롯 캐치 → B3 즉시 컨펌 (사용자 추가 컨펌 X)
3. **BAN 위험 낮음** — 다수 보판봇 사용 중 검증
4. **본인 계정 한정** — 외부 사용자/메인 계정 영향 X
5. **POST 예외 적용** — CLAUDE.md 룰
6. **시장 검증 알고리즘** — 보수적 시도 빈도 (1초 인터벌)
7. **5+3 = 8 안전장치** — B1 5종 + B3 3종

## 미해결 (사용자 결정 필요)

- product_option.key 정확 포맷 (Stage B-1 캡처)
- delivery_method 허용값 (Stage B-1 캡처)
- return_address_id 어디서 조회 (Stage B-1 캡처)
- 우선순위 알고리즘 (사용자 다중 상품 동시 봇팅 원하는지)

---

## 다음 작업

1. **Stage B-0 구현** — Dry-run 워커 + 단위 테스트 (지금 진행 가능)
2. **Stage B-1 진입 직전** — 사용자 실 시도 캡처 협조 (브라우저 + 봇 동시)
3. **Stage B-1 ~ B-3** — 사용자 실계정 라이브 검증
4. **UI 연결** — Phase A 머지 후
