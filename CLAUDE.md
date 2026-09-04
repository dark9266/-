# CLAUDE.md

크림봇 (Kream Monitor Bot) — 크림 차익거래 자동화 Discord 봇. Python 3.12+, async-first. 개인용 초고성능 (배포 A).

<!-- 이 파일은 매 세션 전부 컨텍스트에 로드된다. 공식 권장 상한 200줄.
     새 지식은 아래 "지식 라우팅" 표를 따라 보낸다 — 여기 무작정 추가하지 말 것.
     session_start_inject 훅이 매 세션 줄 수를 재서 보고한다. -->

## 🔴 INVARIANT — 매 세션 맨 먼저 읽을 것 (변경 금지)

**단일 source of truth**: `~/.claude/projects/-mnt-c-Users-USER-Desktop----/memory/project_current_track.md`
과거 로그: `project_history_archive.md` (자동 로드 X, 수동 참조만).

### 목표 ↔ 방법 (세션 표류 방지 핵심)
- **목표**: 크림 47k DB **전체 상품 (전 카테고리, 거래량 0 포함)** 을 각 소싱처에서 찾아 수익 실현. **크림에 없는 건 무시.**
- **방법**: **푸시** — 소싱처 카탈로그 전체 덤프 → 크림 DB 로컬 교집합 → 후보만 크림 sell_now → 수익 → 알림.
- **왜 푸시**: 목표가 "역방향 맛"이어도 역방향 실행은 크림 캡 폭발. 푸시가 유일한 방법.

### 🎯 타겟 범위 (축소 금지)
- **47k 전부**: "hot 130건만", "거래량 ≥5만", "인기 카테고리만", "신발만" 축소 **전부 금지**
- **거래량 0~3 포함**: 숨은 보석 = 거래량 낮은 상품의 가격 급등 마진이 가장 큼
- **축 ② 예외**: `tier2_monitor` hot 폴링은 보조 감시. 메인 타겟 축소 아님.
- **필터 vs 축소 구분**: 알림 하드 플로어(순수익/ROI/거래량)는 "거짓 알림 방지"지 "타겟 축소" 아님.

### 🚨 다음 세션 금지 질문/제안
- "역방향이 원래 의도 아니냐?" → **X.** 목표는 교집합, 방법은 푸시.
- "hot/인기 카테고리 먼저 타겟팅할까요?" → **X.** 47k 전체 고정.
- "거래량 낮은 건 빼도 되지 않나요?" → **X.** 숨은 보석 핵심 차별화.

### 완성도 축 (우선순위 고정)
1. **정확성** — 매칭 + 사이즈별 실재고. 거짓 알림 0 지향.
2. **속도** — 소싱처 서칭 속도.
3. **신경 X**: 알림 수량 · 무인 운영 · 상용화.

### 단계 (순서 뒤집지 말 것)
1. **현재 = 22 소싱처 안정화** (테스트 사이클 2h → 6h → 24h 3단, 2026-04-22 축소)
2. **안정화 확정 후 = 소싱처 대거 확장**

### 🚫 금지 리스트
- 역방향 스캐너 재설계·`continuous_scanner`·`tier1_scanner`·cold/warm 순환 (폐기 흐름)
  - **예외**: `tier2_monitor.py` (축 ② 보조 감시) 는 역방향 hot 폴링이지만 **유지**. "역방향이니 끄자" 제안 금지 — 이미 판정 완료된 예외다.
- 소싱처 신규 추가 (안정화 전까지)
- 해외 스토어 (EU/US/JP) — 한국 공홈만
- 매칭량 숫자 쫓기 — 헬스체크 4종 통과 전에는 매칭 작업 금지
- 선택지 a/b/c 나열 — 필수 작업 1개만 제시 (사용자 배달일 중 원격 지시)
- 라이브 관측 없이 수치 주장
- 크림 시세/거래량 로컬 DB로 수익 계산 (반드시 실시간 sell_now)

### ✅ 매 세션 시작 헬스체크 4종 (생략 금지)
1. 봇 프로세스 살아있나 (`ps -ef | grep python.*main`)
2. 마지막 알림 24h 내 (`alert_sent.fired_at > strftime('%s','now')-86400`)
3. 크림 일일 호출 KREAM_DAILY_CAP 이하 (`kream_api_calls` 24h, ts > datetime('now','-1 day'))
4. 파이프라인 활동 — `decision_log` 최근 2h 내 `dedup_recent|prefilter_unprofitable|profit_emitted|alert_sent` 합 > 0 (`ts > strftime('%s','now')-7200`)

**🔴 SQL 주의 (2026-04-26 진단 실수 — 컬럼별 타입 다름, 혼용 금지)**:
- **epoch float** (예: `1777186341.19`): `decision_log.ts`, `alert_sent.fired_at`
  - ✅ `WHERE ts > strftime('%s','now')-N` (N=초 단위)
  - ❌ `WHERE ts > datetime('now','-N hours')` 금지 — text "2026-04-26..." 와 epoch '1777...' string 비교 시 항상 False (`'1' < '2'`)
- **TEXT datetime** (예: `'2026-04-26 08:36:26'`): `kream_api_calls.ts`, `bot_state.updated_at`, `bot_logs.ts`, `kream_products.updated_at`
  - ✅ `WHERE ts > datetime('now','-1 day')`
  - ❌ `WHERE ts > strftime('%s','now')-86400` 금지 — text '2026...' > number 1777... string 비교 시 항상 True → 전 row 반환 (false high count)
- **컬럼별 `SELECT typeof(ts), ts FROM <table> LIMIT 1` 로 먼저 확인 후 쿼리 작성.**

**1개라도 FAIL → 매칭/신규 작업 착수 금지, 복구 먼저.**

---

## 📇 지식 라우팅 — 새로 알게 된 것을 어디에 쓸 것인가

> 갈 곳이 두 개(CLAUDE.md · 메모리)뿐이라 이 파일이 404줄까지 불었다. 2026-09-04 5분면으로 분리.
> **판단 순서**: ① 어기면 사고인가 → 훅 ② 매 세션 필요한가 → 아니면 CLAUDE.md 금지.

| 이런 지식이면 | 여기로 | 로드 시점 | 상시 컨텍스트 비용 |
|---|---|---|:-:|
| 어기면 사고 (돈·BAN·데이터 손실) | `.claude/hooks/` **훅** | 이벤트마다 | **0** |
| 매 세션 판단에 필요 (방향·목표·금지) | **이 파일** (≤200줄) | 매 세션 전부 | 높음 |
| 특정 파일 만질 때만 필요 | `.claude/rules/*.md` + `paths:` | 그 파일 열 때 | **0** |
| 여러 단계 절차 (매번 같은 순서) | `.claude/skills/` **스킬** | 호출될 때 | **0** |
| 사실·이력·끝난 판정 | auto memory · `docs/DONE_REGISTRY.md` | 필요 시 읽음 | 인덱스 한 줄 |

- ⚠️ `@import` 는 절감 수단이 **아니다** — 임포트 파일도 launch 때 같이 로드된다.
- 코드에서 읽을 수 있는 것(파일 목록·디렉터리 구조·의존성)은 **어디에도 안 쓴다**.
- 현재 규칙 파일: `adapters.md` · `kream-api.md` · `readonly.md` · `testing.md` · `core-modules.md`

## 🛡 기계 가드 (마크다운이 못 막는 것을 훅이 막는다)

> 앤트로픽 공식: *"'절대 하지 마라' 는 지시가 아니라 **훅+권한**으로 하라."*
> 위 🚫 금지 리스트와 "라이브 관측 없이 수치 주장" 은 **기계가 강제**한다.

| 훅 | 시점 | 무엇을 |
|---|---|---|
| `session_start_inject.py` | SessionStart | INVARIANT·봇 생존·최근 커밋·DONE 원장·헬스체크·**지시문 예산** 주입 |
| `task_intake_guard.py` | UserPromptSubmit | DONE 원장·메모리·git log·모듈 자동 검색 + 금지 리스트 경고 + **스킬 자동 소환** |
| `intent_gate.py` | UserPromptSubmit·PreToolUse | 애매한 신규 지시 → 요약 3줄 + 객관식 ≤3개 인터뷰. 답변 전 코드 수정 물리 차단 |
| `direction_guard.py` | PreToolUse | 폐기 흐름 수정 차단 + 신규 소싱처 생성 차단. 해제 = `KREAM_LEGACY_EDIT_GO` / `KREAM_NEW_SOURCE_GO` |
| `kream_live_call_guard.py` | PreToolUse | 실계정 크림 호출 차단. 해제 = `KREAM_LIVE_BATCH_GO=1` |
| `orchestration_gate.py` | PreToolUse | Fable 모드 ON 시 메인 직접수정 턴당 5파일 제한 |
| `claim_evidence_guard.py` | Stop | 증거 없는 "완료·안 된다·N건이다" 차단 |
| `tests/conftest.py` | pytest 전역 | 실 네트워크·운영 DB 차단 + tmp DB 격리 |

전부 **fail-open**. 판정 로직은 `tests/test_hooks_guards.py` 로 고정.
차단 메시지를 받으면 **우회하지 말고** 그 지시대로 하거나 사장에게 보고한다.
재실행 금지 원장 = `docs/DONE_REGISTRY.md` — 끝난 일은 한 줄 추가.

@.claude/orchestration/active.md

## 아키텍처 (푸시 단일 트랙)

```
22 소싱처 어댑터 (src/adapters/*) → 카탈로그 덤프
  → matcher (크림 DB 로컬 교집합) → collect_queue
  → kream_delta_watcher + orchestrator (sell_now 조회)
  → profit_calculator → Discord 알림
```
- 축 ② 보조: `tier2_monitor.py` 역방향 hot 폴링 (폐기 X, 재포지셔닝)
- 모듈 지도·폐기 흐름 상세 → `.claude/rules/core-modules.md`

### UI 봇 트랙 (2026-04-26 시작, 22 소싱처 안정화와 병행)
Electron 데스크톱 전환. **Phase A**(UI 골격) → **Phase B**(보관판매 등록, 벌크 계정 한정 POST 예외) → **Phase C**(자동경쟁).
상세: 메모리 `project_ui_bot_track.md` · `project_phase_b_storage_sale.md` · `project_phase_c_auto_compete.md` · `feedback_ui_bot_triggers.md`.
UI는 봇 코어를 안 건드리므로 트랙 1과 병행 가능.

## 수수료 · 시그널 (돈 계산 — 매 세션 필요)

```
정산가 = 판매가 - (기본료 2,500 + 판매가 × 6%) × 1.1(VAT) - 검수비 2,500 - 배송비 3,000
검수비 2,500원 (실 정산서 확인 — 2026-05-02, 재추정 금지)
```

- **STRONG_BUY**: 순수익 ≥ 30,000 AND 7일 거래량 ≥ 1
- **BUY**: 순수익 ≥ 15,000 AND 7일 거래량 ≥ 1
- **WATCH**: 순수익 ≥ 5,000 AND 7일 거래량 ≥ 1
- **NOT_RECOMMENDED**: 그 외 (거래량 0 = 대기 매수자 없음 → 판매 불가)
- **알림 하드 플로어**: 순수익 ≥ 10,000₩ AND ROI ≥ 5% AND 거래량 ≥ 1

**거래량 게이트 1 고정.** 저거래(숨은 보석)가 핵심 차별화 — 낮추자/올리자 재제안 금지.
`profit_calculator.py`·수수료·시그널 변경 시 **`profit-analyzer` 의무 + Codex S 등급**.

## Commands

```bash
pip install -e ".[dev]"                 # 설치
python main.py                          # 봇 실행 (사장 명시 지시 or UI 버튼만)
pytest tests/ -v                        # 전체 테스트
PYTHONPATH=. python scripts/verify.py   # 파이프라인 검증
ruff check src/ tests/                  # 린트
bash scripts/fable.sh on|off|status     # 오케스트레이션 토글
python scripts/codex_collab.py plan     # Codex 등급 판정 (호출 0)
```

슬래시 명령: `/status` `/health` `/verify` `/queue` `/catalog` `/coverage` `/trace` `/commit` `/add-source`
— 정의는 `.claude/commands/*.md`.

## 에이전트 — 의무 투입 트리거

| 트리거 | 에이전트 |
|---|---|
| 새 소싱처 추가 | `crawler-builder` (선행: `api-prober` → `source-analyzer`) |
| 수수료·시그널·하드플로어 변경 | `profit-analyzer` |
| 코어 모듈 수정 후 (kream.py·profit_calculator.py·orchestrator.py) | `code-reviewer` |
| "왜 안 잡혀? 왜 알림 안 와?" | `scan-debugger` |
| 크롤러·어댑터 수정 후 실동작 검증 | `live-tester` |

Fable 모드 ON 시 일반 노동은 `kream-executor`(구현) / `kream-explorer`(탐색).
**도메인 의무 트리거가 오케스트레이션보다 우선.**
읽기전용 조사는 자율 병렬 투입 허용(한 줄 고지). 상태 변경 동반 작업은 사전 확인.

## 스킬 (설명은 각 SKILL.md — 훅이 자동 소환한다)

- `kream-search-in-search` — 소싱처 403/202/빈 결과. **"차단됐다" 판단하기 전에 필수**
- `kream-codex-collab` — 사장이 "코덱스 검증/의논/협업해" 할 때만 (Plus 한도 구매대행과 공유 → 수동 전용)
- `ponytail` (+`-review` `-audit` `-debt`) — 과잉설계 차단. **세션 시작 시 상시 ON**, 단 의무 에이전트·TDD·검증보다 **아래**
- `kream-youtube-transcript` · `kream-youtube-channel` · `kream-instagram-extract` — 외부 자료 추출

## 개발 방식

- **플랜 모드**: 파일 3개+ 또는 아키텍처 변경 시 **의무 진입**. 그 외는 바로 실행.
- **커밋**: 검증 3종(verify.py + pytest + AST) 통과 후에만. 완료 시 Discord 웹훅(`DISCORD_NOTIFY_WEBHOOK`) 알림.
- **봇 가동/중지**: UI 봇 STOP/START 또는 사장 명시 지시만. 자동 시작 영구 금지.
- **막히면 해결, 우회 X** — 원래 방법의 해결책을 N개 시도한 뒤에만 우회를 논한다.
- 환경: WSL2 + Windows. `.env` 키 정의는 `src/config.py`. 무신사 세션 쿠키 `data/musinsa_session.json`.
