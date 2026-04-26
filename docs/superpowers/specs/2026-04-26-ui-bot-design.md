# 크림봇 → UI 봇 전환 설계 (2026-04-26)

## Status
**브레인스토밍 완료, 사용자 spec 검토 대기 중.** 구현 진입 전 사용자 승인 필수.

## 1. 비전

현 크림봇 (Python 백그라운드 + Discord 봇) → **Electron 데스크톱 UI 봇** 전환. 터미널/디스코드에서 하던 모든 운영을 UI 안으로 통합.

### 사용자 핵심 요구
1. 봇 시작/중지 컨트롤
2. 모드 시스템 — 24h 모드 / 선택 소싱처 모드
3. 보관판매 자동등록 (Phase B, 디테일 추후 사용자 설명)
4. 자동경쟁 시스템 (Phase C, 디테일 추후 합의)
5. UI = 최대한 멋있게 (캐챱매니아 상회 기술 수준)
6. 봇 = 백그라운드 (봇 외엔 안 보임)
7. 로그창 = 별도 윈도우 (분리 가능)

### 트랙 위치
- **트랙 1 (메인)**: 22 소싱처 안정화 — 현행 운영 유지
- **트랙 2 (UI 봇 전환)**: 본 스펙 — 트랙 1과 병행 가능 (봇 코어 안 건드림)

---

## 2. 결정 사항 (재논의 금지)

| 결정 | 값 | 근거 |
|---|---|---|
| UI 프레임워크 | **Electron** | 생태계 + Windows 호환 + 메모리 200MB 무시 가능 (본인 PC) |
| 모바일 지원 | **X** | 사용자 명시 "모바일버전 없어도돼" |
| 봇 ↔ UI 구조 | **D — 헤드리스 서버 + Electron 클라이언트** | 미래 임대 서버 이전 시 endpoint URL 1줄 변경만 |
| Phase 순서 | **A → B → C** | A 풀완주 후 B 진입, B 안정화 후 C |
| Phase A 범위 | **A1+A2+A3+A4 풀 완주** | "터미널 운영 전부 UI로" = 일부만 X |
| 레이아웃 | **사이드바 메뉴 + 메인 영역 + 분리 가능 로그창** | IDE 수준 UI (VS Code/JetBrains 패턴) |
| 통신 | **HTTP REST + WebSocket** | 제어 = REST, 실시간 로그/상태 = WebSocket |
| 인증 | **로컬 토큰 (`.env.api_token`)** | 임대 서버 외부 노출 시 필수 |
| 토큰 정책 | **풀파워** | 제작/안정화 단계 = 기술력 우선 |
| Phase B 정의 | **보관판매 자동등록 (슬롯 캐치)** — 디테일 추후 | 사용자 추후 설명, 지금은 메뉴 자리만 |

### 🚫 우회 X (블로커 시 원래 방법 해결 우선)
- 막힘 발생 시 다른 방향 제시 금지 (`feedback_no_workarounds.md`)
- 원래 방법 해결책 N개 시도 후에만 우회 검토

---

## 3. 아키텍처

### 3.1 전체 구조 (옵션 D)

```
┌────────────────────────────────────────┐
│ 본 PC (현재) → 임대 서버 (미래)           │
│                                          │
│  ┌─────────────────┐   ┌─────────────┐ │
│  │ Python 봇       │   │ FastAPI     │ │
│  │ (현재 가동 중)   │←→│ /api/*      │ │
│  │ runtime.py 등   │   │ /ws/*       │ │
│  └─────────────────┘   └─────────────┘ │
│           ↓                  ↓          │
│  ┌──────────────────────────────────┐  │
│  │ SQLite (kream_bot.db)            │  │
│  └──────────────────────────────────┘  │
└────────────────────────────────────────┘
              ↑↓ HTTP + WebSocket
              (localhost:8000 → 미래 https://kream.본인.com)
              ↓
┌────────────────────────────────────────┐
│ 본 PC (Electron 데스크톱, 영구)          │
│                                          │
│  ┌────────────────────────────────┐   │
│  │ Main Window                     │   │
│  │ ┌─Sidebar─┐ ┌─Main Area─────┐  │   │
│  │ │Dashboard│ │Status / Adapt│  │   │
│  │ │Adapters │ │er grid /     │  │   │
│  │ │Logs (⇱) │ │Charts        │  │   │
│  │ │Storage  │ │              │  │   │
│  │ │Auto Comp│ │              │  │   │
│  │ │Diagnose │ │              │  │   │
│  │ │Settings │ │              │  │   │
│  │ └─────────┘ └──────────────┘  │   │
│  └────────────────────────────────┘   │
│                                          │
│  ┌────────────────────────────────┐   │
│  │ Log Window (Detached, optional)│   │
│  │ Live stream + grep + 색상 코딩│   │
│  └────────────────────────────────┘   │
└────────────────────────────────────────┘
```

### 3.2 컴포넌트 분리

| 컴포넌트 | 책임 | 위치 | 의존 |
|---|---|---|---|
| **Python 봇 코어** | 22 어댑터 + matcher + profit_calculator + alert | 봇 머신 | (현행, 변경 X) |
| **FastAPI 서버** | REST API + WebSocket 게이트웨이 | 봇 머신, 봇과 같은 프로세스 또는 별도 프로세스 | uvicorn, pydantic |
| **`bot_state` 테이블** | UI ↔ 봇 시그널 (paused, mode, per-source enabled) | SQLite | 봇 코어가 주기적으로 폴링 |
| **로그 emitter** | 봇 로그 → SQLite + WebSocket 브로드캐스트 | 봇 코어 hook | `logging.Handler` 서브클래스 (structlog 미도입 — 의존성 최소화) |
| **Electron Main Process** | 윈도우 관리, IPC, 인증 토큰 보유 | 사용자 PC | electron, electron-builder |
| **Electron Renderer** | React UI, 메인 윈도우 | 사용자 PC | React, Vite, Tailwind, shadcn/ui, Recharts |
| **Detached Log Window** | 별도 BrowserWindow, WebSocket 직접 연결 | 사용자 PC | electron BrowserWindow API |

### 3.3 봇 ↔ FastAPI 관계

**옵션 1 (권장)**: 봇 entry point에 FastAPI 통합 — 같은 프로세스
- `main.py` 시작 시 `uvicorn.run(app, ...)` 백그라운드 task로 띄움
- 장점: 봇 죽으면 API도 죽음 (UI가 즉시 감지) / DB 접근 단일 / 단순
- 단점: 봇 재시작 시 API도 재시작 (UI 일시 끊김)

**옵션 2**: FastAPI 별도 프로세스 + 봇과 SQLite 공유
- 장점: 독립 재시작 가능
- 단점: DB 접근 경합 / 봇 상태 동기화 복잡

→ **옵션 1 채택**. 봇과 운명 공동체 = UI가 봇 상태 정확히 반영.

---

## 4. Phase A 구현 (4단계)

### Phase A1 — pause/resume + START/STOP UI

**목표**: 사용자가 UI 버튼으로 봇 일시 정지/재개

**구현**:
- DB 테이블 신규: `bot_state(key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)`
- 초기 행: `('paused', 'false')`, `('mode', '24h_full')`
- 봇 측: `scheduler.py` 6개 루프 진입 시 `if state.get('paused') == 'true': continue` 체크
- API: `POST /api/bot/pause` / `POST /api/bot/resume`
- UI: 사이드바 하단에 START/STOP 버튼 (현재 상태 색상 반영)

**파일**:
- 신규: `src/api/server.py` (FastAPI app), `src/api/routers/bot.py`, `src/api/state.py`
- 수정: `main.py` (FastAPI 백그라운드 task 추가), `src/scheduler.py` (pause 체크 6곳)
- DB 마이그레이션: `migrations/0XX_bot_state.sql`

**테스트** (TDD 의무):
- `tests/api/test_bot_pause.py` — pause flag → scheduler 루프 stop
- `tests/api/test_bot_resume.py` — resume flag → scheduler 루프 재개
- `tests/api/test_bot_state_persistence.py` — 봇 재시작 후 상태 유지

### Phase A2 — per-source 토글 (모드 시스템)

**목표**: 사용자가 22 소싱처 중 특정 소싱처만 ON/OFF + 24h 모드 / 선택 모드 토글

**구현**:
- `bot_state` 테이블 활용 — `mode` 키 (값: `24h_full` / `selective`) + `enabled_sources` (JSON 배열)
- 봇 측: `registry.py` 의 `is_enabled(source)` 체크에 `bot_state.enabled_sources` 추가
- API: `POST /api/sources/{source}/enable`, `POST /api/sources/{source}/disable`, `POST /api/sources/mode`
- UI: 사이드바 "Adapters" 클릭 → 22 소싱처 그리드 카드 (🟢/🟡/🔴 헬스 상태 + on/off 스위치) + 상단 모드 토글 (24h/Selective)

**파일**:
- 신규: `src/api/routers/sources.py`
- 수정: `src/crawlers/registry.py` (UI flag 통합), 사이드바 mockup CSS

**테스트**:
- `tests/api/test_source_toggle.py` — 단일 소싱처 disable → 어댑터 dispatch에서 제외
- `tests/api/test_mode_switch.py` — selective → 24h 전환 시 모든 소싱처 활성

### Phase A3 — 메인 대시보드

**목표**: 실시간 status + 차트 + 22 어댑터 그리드 (한눈에 봇 상태 파악)

**구현**:
- WebSocket 채널 `/ws/status` — 5초 주기로 status 푸시 (PID, uptime, 큐 적체, 24h 호출량, 알림 수, 매칭 수)
- WebSocket 채널 `/ws/alerts` — 새 알림 발생 시 push
- API: `GET /api/dashboard/snapshot` (초기 로드용)
- UI 컴포넌트:
  - **Status Cards** (4개): 크림 호출 24h / 알림 24h / 매칭 6h / 큐 적체
  - **Adapter Grid** (22개): 헬스 상태 색상 + 클릭 시 디테일 모달
  - **Recent Alerts** (10개): 최근 알림 리스트 + 클릭 시 디테일
  - **차트**: 24h 알림 시계열 / 호출량 시계열 (Recharts)

**파일**:
- 신규: `src/api/routers/dashboard.py`, `src/api/websocket.py`
- 신규 (frontend): `electron/src/pages/Dashboard.tsx`, `StatusCard.tsx`, `AdapterGrid.tsx`, `RecentAlerts.tsx`

**테스트**:
- `tests/api/test_dashboard_snapshot.py`
- `tests/api/test_ws_status_broadcast.py`

### Phase A4 — 운영 패널 (수정 관리자 모드)

**목표**: 터미널에서 하던 운영 작업 전부 UI로 (Claude 수정 관리자 모드)

**기능**:
| 기능 | UI | API |
|---|---|---|
| 봇 재시작 | 사이드바 하단 "🔄 Restart" 버튼 (확인 모달) | `POST /api/bot/restart` |
| 진단 스냅샷 export | "📋 진단 스냅샷" 버튼 → JSON/MD 다운로드 (DB 통계 + decision_log 1h + 어댑터 상태 + git HEAD + uptime) | `GET /api/diagnostics/snapshot` |
| 디버그 로그 ON/OFF | 토글 스위치 (일시적 verbose 로그) | `POST /api/bot/debug` |
| Git pull + 재시작 | "🔄 봇 업데이트" 버튼 (`git pull` + commit log 5개 표시 + 확인 → restart) | `POST /api/bot/update` |
| 로그 grep export | Logs 페이지 또는 분리 윈도우 검색바 + Export 버튼 | `GET /api/logs/export?grep=...&hours=...` |

**파일**:
- 신규: `src/api/routers/ops.py`
- 신규: `electron/src/pages/Diagnostics.tsx`, `Settings.tsx`

**테스트**:
- `tests/api/test_diagnostics_snapshot.py` — 결과 JSON 스키마
- `tests/api/test_log_export.py` — grep + 시간범위 필터

---

## 5. Phase B/C — 메뉴 자리만 (구현 추후)

### Phase B (보관판매 자동등록)
- 사이드바 메뉴: `🎯 보관판매 자동등록` (Disabled, "준비 중 — 추후 사용자 설계 합의 후 활성화")
- 클릭 시 모달: "보관판매 자동등록은 사용자 디테일 설명 후 구현 예정. Phase A 완주 후 진행."
- **구현 0%** — 디테일은 사용자 추후 설명 (`project_phase_b_storage_sale.md` 참조)

### Phase C (자동경쟁)
- 사이드바 메뉴: `🤖 자동경쟁` (Disabled, "Phase B 안정화 후")
- 동일 안내 모달

---

## 6. UI 디자인 (D 옵션 디테일)

### 6.1 컬러 스키마 (다크 테마, GitHub Dark 영감)
| 용도 | 컬러 |
|---|---|
| 배경 (메인) | `#0d1117` |
| 배경 (사이드바) | `#010409` |
| 배경 (카드) | `#161b22` |
| 테두리 | `#30363d` |
| 텍스트 (주) | `#c9d1d9` |
| 텍스트 (보조) | `#8b949e` |
| 액센트 (선택) | `#58a6ff` (blue) |
| 성공/매수 | `#3fb950` (green) |
| 경고/throttle | `#d29922` (orange) |
| 위험/error | `#f85149` (red) |
| 봇 active | `#238636` (dark green) |

### 6.2 타이포그래피
- 본문: `'Inter', system-ui, sans-serif`
- 모노스페이스 (로그/숫자): `'JetBrains Mono', 'Consolas', monospace`
- 사이즈: 큰 숫자 (status) 18~24px, 본문 13~14px, 보조 11~12px, 라벨 9~10px (uppercase)

### 6.3 컴포넌트 라이브러리
- **shadcn/ui** — Button, Modal, Switch, Tabs, Card, Toast, Tooltip
- **Tailwind CSS** — utility-first
- **Recharts** — 시계열 차트 (호출량, 알림)
- **lucide-react** — 아이콘 (사이드바)
- **react-hot-toast** — 작업 결과 알림 (START 성공 등)

### 6.4 메인 윈도우 레이아웃
```
┌─[Title Bar: ⚪⚪⚪ Kream Bot — Control Center · PID 32365]─────────┐
├──────┬─────────────────────────────────────────────────────────┤
│Side  │ ┌─[🟢 RUNNING · 5h 23m · Mode: 24h Full]───────────┐  │
│bar   │ │                                                     │ │
│      │ │ ┌──────────┬──────────┬──────────┬──────────┐    │ │
│📊 Dash│ │ │ 크림 호출 │ 알림 24h │ 매칭 6h  │ 큐 적체  │    │ │
│🔌 Adap│ │ │ 2,122    │ 12       │ 87       │ 340      │    │ │
│📝 Logs│ │ └──────────┴──────────┴──────────┴──────────┘    │ │
│  ⇱ Det│ │                                                     │ │
│🎯 Stor│ │ ┌─[Sourcing Sources (22) · 🟢18 🟡3 🔴1]──────┐ │ │
│  (대기)│ │ │ [musinsa][29cm][nike][adidas][kasina][vans]│ │ │
│🤖 Auto│ │ │ [eql][tnf 🚫][salomon][arc][tune][+11]    │ │ │
│  (대기)│ │ └────────────────────────────────────────────┘ │ │
│🔬 Diag│ │                                                     │ │
│⚙️ Sett│ │ ┌─[Recent Alerts (10)]─────────────────────────┐ │ │
│      │ │ │ STRONG_BUY · adidas R71 · 14m ago · ₩42,100  │ │ │
│[⏸ ▶ ]│ │ │ BUY · nike AF1 · 28m ago · ₩18,500           │ │ │
│Update│ │ │ ...                                            │ │ │
└──────┴─└────────────────────────────────────────────────┘─┘
```

### 6.5 분리 로그 윈도우 (Detached)
- 사이드바 "Logs" 옆 ⇱ 아이콘 클릭 → 새 BrowserWindow 생성
- 윈도우 크기/위치 자동 저장 (다음 실행 복원)
- 헤더: ⚪⚪⚪ Live Logs · 🟢 (연결 상태)
- 검색바: grep filter (정규식 지원), 레벨 필터 (All/INFO/WARN/ERROR)
- 본문: 모노스페이스, 색상 코딩 (INFO=blue, WARN=orange, ERROR=red, OK=green, DBG=gray), auto-scroll 옵션
- 우측 상단 ⇲ Attach 버튼 (메인 윈도우로 복귀)

---

## 7. 데이터 흐름

### 7.1 시작/중지 (Phase A1)
```
[User clicks START]
  ↓
Electron Renderer → Main Process (IPC)
  ↓
Main Process → HTTP POST /api/bot/resume + auth token
  ↓
FastAPI updates bot_state: paused=false
  ↓
Scheduler 6 loops next tick: continue (no skip)
  ↓
WebSocket /ws/status broadcasts new state
  ↓
Electron Renderer updates indicator (🟢 RUNNING)
```

### 7.2 실시간 로그 (Phase A3+A4)
```
[Bot logs via structlog Handler]
  ↓
Custom Handler: write to SQLite + WebSocket broadcast
  ↓
WebSocket /ws/logs → Electron (Detached Log Window)
  ↓
Renderer appends to log buffer (last 1000 lines)
  ↓
Apply current grep filter + level filter
  ↓
Auto-scroll to bottom (if user not scrolled up)
```

### 7.3 진단 스냅샷 (Phase A4)
```
[User clicks "📋 진단 스냅샷"]
  ↓
HTTP GET /api/diagnostics/snapshot
  ↓
FastAPI collects: {db_stats, decision_log_1h, adapter_status, git_HEAD, uptime, env_vars(safe), recent_errors}
  ↓
Returns JSON
  ↓
Electron offers Save Dialog → user saves to ~/Desktop/snapshot_YYYYMMDD.json
  ↓
사용자가 Claude 한테 "이거 봐줘" 복붙 가능
```

---

## 8. 에러 처리

### 8.1 API 에러
- 4xx (잘못된 요청): UI 토스트 (`react-hot-toast`) 빨간색 표시 + 에러 사유
- 5xx (서버 에러): UI 모달 + "재시도" 버튼 + 자동 로그 첨부 옵션
- WebSocket 끊김: UI 상단 노란 배너 ("연결 끊김, 재연결 중...") + 자동 재시도 (지수 백오프 1s, 2s, 4s, 8s, max 30s)

### 8.2 봇 측 에러
- FastAPI 시작 실패 → Python 봇 main.py 실패 fast (현행 watchdog가 respawn)
- WebSocket 브로드캐스트 실패 → 로그만, 봇 코어 영향 0
- DB lock → 재시도 3회 후 fail (현행 패턴 유지)

### 8.3 Electron 측 에러
- Renderer crash → Main Process 자동 reload
- Detached Log Window crash → Main에서 재생성 옵션
- IPC 실패 → UI 토스트 + 재시도 가능

---

## 9. 테스트 전략

### 9.1 단위 테스트 (의무 — TDD)
- `tests/api/*` — FastAPI 라우터별 테스트 (pytest + httpx AsyncClient)
- `tests/state/*` — `bot_state` 테이블 CRUD + 동시성
- `tests/log_emitter/*` — 로그 핸들러 → SQLite + WebSocket 정상 작동

### 9.2 통합 테스트
- `tests/integration/test_pause_resume_e2e.py` — API → DB → 봇 루프 → API status 변화
- `tests/integration/test_source_toggle_e2e.py` — toggle → registry → adapter dispatch 변화

### 9.3 Electron 측 테스트 (Playwright)
- `electron/tests/dashboard.spec.ts` — 메인 화면 렌더 + status 카드 표시
- `electron/tests/log_window.spec.ts` — 분리/재연결 시 윈도우 생성/복귀
- `electron/tests/start_stop.spec.ts` — START 버튼 → API → status 변경 확인

### 9.4 검증 (verification-before-completion 의무)
- Phase A1 완료 → `pytest tests/api/test_bot_pause.py -v` PASS + 실 봇으로 START/STOP 5회 무사고
- Phase A 전체 완료 → 24h 무중단 + 봇 재시작 5회 무사고 + UI crash 0회

---

## 10. 현 PC → 임대 서버 이전 계획 (미래)

### 진입 조건 (Phase A·B·C 완료 후)
- Phase A 안정화 1주
- Phase B 보관판매 자동등록 안정화 1개월
- 22 소싱처 매칭 누수 0건 유지

### 이전 절차
1. 임대 서버 (Linux + Python 3.12+) 셋업
2. 봇 코드 git clone + `.env` 이전 + `kream_bot.db` 백업/복원
3. systemd unit으로 봇 + FastAPI 서비스 등록
4. Cloudflare Tunnel 또는 Tailscale 셋업 (외부 접근)
5. Electron `.env` 또는 Settings 페이지에서 endpoint URL 변경: `http://localhost:8000` → `https://kream.본인.com`
6. 인증 토큰 변경 (서버 측 새 토큰 생성)
7. **봇 머신 = 임대 서버, UI 머신 = 본 PC** 분리 완료

이전 시 코드 변경 = **0줄** (URL과 토큰만 바뀜).

---

## 11. 디렉토리 구조 (예상)

```
크림봇/
├── src/                              # 기존 봇 코어
│   ├── adapters/
│   ├── core/
│   ├── crawlers/
│   ├── api/                          # 신규: FastAPI 서버
│   │   ├── server.py
│   │   ├── state.py
│   │   ├── websocket.py
│   │   ├── auth.py
│   │   └── routers/
│   │       ├── bot.py
│   │       ├── sources.py
│   │       ├── dashboard.py
│   │       ├── ops.py
│   │       └── logs.py
│   └── ...
├── electron/                         # 신규: Electron 앱
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── electron-builder.json
│   ├── src/
│   │   ├── main/                     # Main process
│   │   │   ├── index.ts
│   │   │   ├── windows.ts            # 메인 + Detached Log
│   │   │   └── api-client.ts
│   │   ├── renderer/                 # Renderer (React)
│   │   │   ├── App.tsx
│   │   │   ├── pages/
│   │   │   │   ├── Dashboard.tsx
│   │   │   │   ├── Adapters.tsx
│   │   │   │   ├── Logs.tsx
│   │   │   │   ├── Diagnostics.tsx
│   │   │   │   ├── Settings.tsx
│   │   │   │   └── PlaceholderB_C.tsx  # Phase B/C 자리
│   │   │   ├── components/
│   │   │   │   ├── Sidebar.tsx
│   │   │   │   ├── StatusCard.tsx
│   │   │   │   ├── AdapterGrid.tsx
│   │   │   │   ├── RecentAlerts.tsx
│   │   │   │   └── ...
│   │   │   └── lib/
│   │   │       ├── api.ts            # axios + auth
│   │   │       └── ws.ts             # WebSocket client
│   │   └── log-window/               # Detached Log Window (별도 entry)
│   │       └── index.tsx
│   └── tests/
├── docs/superpowers/specs/
│   └── 2026-04-26-ui-bot-design.md   # 본 파일
├── tests/
│   ├── api/                          # FastAPI 테스트
│   ├── state/
│   ├── integration/
│   └── ...
└── ...
```

---

## 12. 의존성 (신규 추가)

### Python (`pyproject.toml`)
```toml
fastapi = ">=0.115"
uvicorn = {extras = ["standard"], version = ">=0.32"}
websockets = ">=13"
```

### Node (`electron/package.json`)
```json
{
  "dependencies": {
    "react": "^18.3",
    "react-dom": "^18.3",
    "react-router-dom": "^6.28",
    "axios": "^1.7",
    "tailwindcss": "^3.4",
    "@radix-ui/react-*": "...",
    "lucide-react": "^0.460",
    "recharts": "^2.13",
    "react-hot-toast": "^2.4",
    "zustand": "^5.0"
  },
  "devDependencies": {
    "electron": "^33",
    "electron-builder": "^25",
    "vite": "^5.4",
    "typescript": "^5.6",
    "@playwright/test": "^1.48"
  }
}
```

---

## 13. 작업 순서 (Phase A 4단계 + 검증)

| Step | 작업 | effort | 검증 |
|---|---|---|---|
| 0 | Electron 앱 골격 셋업 (Vite + React + Tailwind + electron-builder) | 0.5d | `npm run dev` → 빈 윈도우 뜸 |
| 0.5 | FastAPI 서버 골격 셋업 + main.py 통합 + auth 토큰 | 0.5d | `curl http://localhost:8000/api/health` 200 |
| A1 | pause/resume + START/STOP UI | 0.5d | 봇 START/STOP 5회 무사고 |
| A2 | per-source 토글 + 모드 시스템 | 0.5d | 1 소싱처 disable → adapter dispatch 0건 확인 |
| A3 | 메인 대시보드 (status + adapter grid + alerts + 차트) | 1d | 5초 주기 status 갱신 + 알림 push 정상 |
| A4 | 운영 패널 (snapshot/디버그/업데이트/재시작/로그 export + Detached Log Window) | 1d | 진단 스냅샷 export → JSON 정상 / Detached 윈도우 분리/복귀 |
| **검증** | 24h 안정화 + UI crash 0 + 봇 재시작 5회 무사고 | — | 사용자 일상 사용 확인 |
| **Phase B 진입 게이트** | 사용자 디테일 설명 + 새 합의 | — | 디테일 받은 후 spec 갱신 |

**총 effort**: 3.5~4일 (Phase A 풀 완주, 검증 제외)

---

## 14. 자동 트리거 (매 세션 자동 적용)

`feedback_ui_bot_triggers.md` 참조. 사용자 미지시 시에도 발동:

| 시점 | 자동 적용 |
|---|---|
| 새 UI 컴포넌트 작성 직전 | `frontend-design:frontend-design` 스킬 invoke |
| 신규 기능 구현 (pause flag 등) | `superpowers:test-driven-development` 적용 |
| 파일 3개+ 수정 예상 | `EnterPlanMode` 진입 |
| 봇 코어 (`runtime.py` 등) 수정 | `code-reviewer` 에이전트 |
| "완료" 주장 직전 | `superpowers:verification-before-completion` invoke |
| Phase B0 진입 (디테일 받은 후) | `api-prober` 에이전트 |

---

## 15. 트랙 1 (소싱처 안정화) 와의 관계

**병행 가능**. Phase A 작업은 봇 코어를 거의 안 건드림:
- A1 = `scheduler.py` pause 체크 1줄 추가 × 6 (회귀 위험 낮음)
- A2 = `registry.py` UI flag 통합 (회귀 위험 낮음, 기존 서킷브레이커 패턴 재활용)
- A3 = 신규 모듈 (`src/api/*`)만 추가, 봇 코어 변경 0
- A4 = 동일

**충돌 시점**: 트랙 1에서 봇 코어 fix가 들어오면 (예: `orchestrator.py` 수정) → A1·A2 작업 일시 멈춤 → 트랙 1 fix 머지 → A 재개 (rebase). 큰 충돌 예상 X.

---

## 16. 미해결 / Phase B 디테일 합의 시 결정

| 항목 | 디테일 합의 시 결정 |
|---|---|
| Phase B 보관판매 자동등록 — 슬롯 캐치 패턴 | 사용자 추후 설명 (`project_phase_b_storage_sale.md`) |
| Phase C 자동경쟁 정의 | 사용자 추후 확인 (등록 후 최저가 추적이 맞는지) |
| 임대 서버 후보 (AWS / Hetzner / Linode 등) | Phase A·B·C 안정화 후 |
| Cloudflare Tunnel vs Tailscale 선택 | 임대 서버 이전 시점 |
| Electron 자동 업데이트 (electron-updater) | Phase A 완료 후 검토 (개인용이라 우선순위 낮음) |
| 다국어 (영어 UI) | X (한국어 단일) |

---

## 17. 사용자 검토 요청

본 spec 검토하시고 변경/추가 사항 알려주세요. 승인 후 `superpowers:writing-plans` 스킬로 단계별 implementation plan 작성 진입합니다.

특히 확인 필요:
1. **Phase A 구조 (D 옵션) OK?** 헤드리스 봇 + Electron 클라이언트 + WebSocket 통신
2. **사이드바 메뉴 7개 OK?** Dashboard / Adapters / Logs / 보관판매 자동등록 / 자동경쟁 / Diagnostics / Settings
3. **컬러/타이포 디자인 방향 OK?** 다크 테마, GitHub Dark 영감
4. **Phase B/C 메뉴 자리만 (구현 추후) OK?**
5. **3.5~4일 effort 예상 — 의도 맞음?**
6. **Phase A 작업 ↔ 트랙 1 (소싱처 안정화) 병행 OK?**
