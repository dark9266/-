# Stage B-1 캡처 가이드 — 보관판매 4 항목

**작성일**: 2026-05-01
**대상**: 사용자가 실 계정으로 보관판매 1회 시도 시 트래픽 캡처

---

## 왜 캡처가 필요한가

api-prober JS 번들 분석으로 endpoint 와 응답 구조는 확정. 다만 **실 호출 시 정확한 값**은 직접 시도해야만 알 수 있음:

| 항목 | 왜 코드만으로 알 수 없나 |
|---|---|
| 로그인 endpoint | JS 번들에는 함수 호출만 있고 정확한 path 가 dynamic |
| `product_option.key` 포맷 | 사이즈가 mm 문자열 ("265") 인지 내부 UUID 인지 실측 필요 |
| `delivery_method` 허용값 | enum 값이 dropdown 에서 동적 로드 |
| `return_address_id` | 사용자별 등록 주소 ID, 직접 조회 |

이 4 항목 캡처되면 `RealKreamClient` 구현 가능 → 라이브 봇팅 진입.

---

## 캡처 도구 — Chrome 개발자 도구 (Network 탭)

설치/설정 X. PC 크림 웹사이트 (kream.co.kr) 접속한 상태에서 진행.

### 1. 개발자 도구 열기
- 단축키: **F12** 또는 **Ctrl+Shift+I**
- 상단 탭에서 **Network** 클릭

### 2. 캡처 옵션
- **Preserve log** ✅ 체크 (페이지 이동해도 로그 유지)
- 필터: **Fetch/XHR** 선택 (정적 리소스 제외, API 만)

---

## 캡처 1 — 로그인 endpoint

### 절차
1. 크림 웹 로그아웃 (이미 로그인됐으면)
2. F12 → Network 탭 → Fetch/XHR 필터
3. 우측 위 🚫 (clear) 클릭 → 로그 비우기
4. **로그인 진행** (이메일 + 비밀번호 입력 → 로그인 버튼)
5. Network 탭에서 **POST 요청** 찾기 (보통 path 에 `session` / `login` / `auth` 포함)

### 캡처해서 알려줄 것
- **Request URL**: 전체 URL (예: `https://api.kream.co.kr/api/m/sessions`)
- **Request Headers**: 특히 Cookie / X-KREAM-* 시리즈
- **Request Payload**: body JSON (이메일/비밀번호 형식 — **비밀번호는 가려주세요**)
- **Response**: status code + body (토큰/세션ID 어디에 있는지)

### 가려도 되는 것 / 보존해야 하는 것
- ❌ 가릴 것: 비밀번호, 휴대폰 번호 마지막 4자리, 카드정보
- ✅ 보존: endpoint URL, payload key 이름, response 구조

---

## 캡처 2 — `product_option.key` 사이즈 포맷

### 절차
1. 크림 로그인 상태 유지
2. **보관판매 상품 1개 페이지** 이동 (예: 노스페이스 1996 노벨티 250mm)
3. F12 → Network → clear
4. 보관판매 → 사이즈 선택 → "재고 신청" 또는 "보관판매 신청" 클릭
5. **POST 요청** 발생: URL 에 `stock_request/review` 포함된 거 찾기

### 캡처해서 알려줄 것
- **Request Payload**: 정확히 어떻게 생겼는지
  ```json
  {
    "items": [
      { "product_option": { "key": "??????" }, "quantity": 1 }
    ]
  }
  ```
- "??????" 자리에 실제 어떤 값이 들어가는지:
  - "250" (mm 문자열)
  - "uuid-xxxx-xxxx" (내부 ID)
  - 다른 형식

### 응답도 캡처
- 보통 "창고 가득" 메시지 받을 것 (`{"code": 9999, ...}`)
- 메시지 텍스트도 알려주세요

---

## 캡처 3 — `delivery_method` 허용값

### 절차
1. 마이페이지 → 보관판매 (또는 등록 진행 중인 상품)
2. **배송 방법 선택 화면** 진입
3. F12 → Network → clear
4. 배송 방법 선택 (택배 / 편의점 / 직접 등)
5. 다음 단계 진행 시 **POST 요청** 발생: `stock_request/confirm` 포함된 거

### 캡처해서 알려줄 것
- **Request Payload** 의 `delivery_method` 값
- 가능하면 **드롭다운 / 라디오버튼 옵션 모두** 알려주세요 (각각 어떤 값으로 전송되는지)

### 미리 알면 좋은 것
드롭다운 화면 찍어서 옵션 이름 (한글) 모두 적어주세요:
- 예: 택배 / 편의점 픽업 / 직접 방문
- 각각이 영어 enum 값 ("courier", "convenience", "visit") 으로 매핑될 것

---

## 캡처 4 — `return_address_id`

### 절차
1. 크림 마이페이지 → **주소록**
2. 등록된 주소 1개 이상 있어야 함 (없으면 등록 먼저)
3. F12 → Network → clear
4. 주소록 페이지 새로고침 또는 주소 1개 클릭
5. **GET 요청** 찾기: `address` 또는 `seller/address` 포함된 거

### 캡처해서 알려줄 것
- **Response body** 의 주소 리스트 — 각 주소의 `id` 필드 값 (정수)
- 사용자가 보관판매에 사용할 주소의 `id`

또는 더 간단:
- 보관판매 등록 진행 중 (캡처 3 단계와 동일) **`stock_request/confirm`** 요청 payload 의 `return_address_id` 값

---

## 안전 주의

- ❌ 절대 가리지 말 것: endpoint URL, response 구조, header 이름
- ✅ 가려도 됨: 비밀번호, 카드번호, 사용자 이메일 (도메인은 OK)
- 📸 스크린샷 보내도 OK (Network 탭 + Headers + Payload 탭)
- 🔒 Cookie 안에 있는 세션 토큰은 유출 시 봇이 사용자 계정 접속 가능 → **민감 정보**. 봇 만들 때만 사용자가 직접 입력 (코드에 하드코딩 X).

---

## 캡처 후 단계

4 항목 캡처되면 → 다음 작업:
1. `src/storage_sale/client.py` — RealKreamClient 구현
2. CLI runner (`scripts/storage_sale_run.py`) — 사용자 가동/중지
3. Stage B-1 라이브 1회 (실 1 호출 → 응답 검증)
4. Stage B-2 ~ B-3 본격 봇팅

---

## 한 줄 요약

**F12 → Network 탭 → Fetch/XHR 필터 → 보관판매 1회 시도 → POST 요청 4개 (로그인/review/confirm/주소) 의 URL+Headers+Payload+Response 캡처**.

스크린샷 4장이면 충분.
