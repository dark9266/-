# 적대검증 백엔드 교체 — 코덱스 → OmniRoute + 자동 강제

> **상태: 0단계(격리 시험) 통과 · 다음 = 1단계 `review_backend.py`**
> 2026-09-04 작성. 새 세션은 이 문서 + 메모리 `project_omniroute_review_backend.md` 부터 읽는다.

## 왜

**코덱스가 사라진다.** 사장이 ChatGPT Plus 를 곧 해지한다. git 이력상 코덱스는 **최소 19건의
실제 결함**을 잡았다(커밋 11건: 5건×2·4건·3건·2건 봉합, `6b27074` "A 판정 수용 5 / 거부 0").
대체 없이 없애면 품질이 내려간다.

**한도 제약.** 사장은 맥스 플랜으로 **프로젝트 2개**(크림봇 + 구매대행)를 동시 운영 중이다.
따라서 Claude 서브에이전트로 적대검증을 돌리는 건 **맥스 플랜 한도를 직접 갉아먹어** 부적합하다.

**OmniRoute** (★60,931 · 기여자 589 · npm 주간 97,558 · MIT · v3.8.50): 로컬 OpenAI 호환
게이트웨이. 352 프로바이더 / 1,200+ 모델. **무료 티어 → 맥스 플랜 한도 소모 0.**

## 사장 확정 방침
1. 수용/거부 **판정자는 Claude**. 지적을 전부 수용하지 않는다 (실측 오탐률 29%).
2. 외부 전송은 **diff + 주변 맥락**까지. 저장소 전체 X.
3. **무료 우선, 실패 시에만 유료 폴백.**
4. **"검증은 확실히 받아야 한다"** — 형식만 통과하는 경로를 전부 막는다.

## 설계 정정 (사장 지적으로 발견)
최초 "S·A 등급만 차단" 안은 두 군데가 틀렸다:
1. `codex_gate` 의 S 판정은 **경로 5개 + 용어**로만 난다. 2026-09-04 검수비 수정
   (`scripts/verify.py` + `tests/*`)은 **B 로 떨어졌다** — 그날 가장 돈에 가까운 변경인데 샜다.
2. "호출을 줄여야 한도가 절약된다"는 전제는 **무료 티어를 쓰면 무효**다. 넓혀도 공짜다.

→ **범위는 넓히고(S·A·B 전부 차단), 비용은 등급별 모델 티어로 통제한다.**

| 등급 | 무엇이 | 차단 | 모델 | 맥스 한도 |
|:-:|---|:-:|---|:-:|
| **S** | 수수료·정산·검수비·보관판매·자격증명·`.env`·`.claude/hooks/`·호출캡 | ✅ | 최상급, 유료 폴백 허용 | 0 |
| **A** | `src/models/` 스키마·마이그레이션·아키텍처 | ✅ | 중급 무료 → 유료 폴백 | 0 |
| **B** | 그 외 모든 코드 변경 (기본값) | ✅ | **무료 전용** | **0** |
| **C** | 전수·열거·커버리지 훑기 | 경고만 | 무료 | 0 |
| **N** | 문서 · `probe_*`/`diag_*`/`repro_*` | 없음 | — | — |

**S 승격 규칙(신규)**: 경로가 `tests/`·`scripts/verify*` 여도 **diff 본문이 수수료·정산·검수비·
시그널 임계에 닿으면 S 로 올린다.** 위 1번 누락을 막는 장치.

---

## ✅ 0단계 — 격리 시험 결과 (2026-09-04 실측, 재실행 불필요)

세션 스크래치패드에 격리 설치 후 실측. **크림봇 저장소에는 아무것도 설치하지 않았다.**

| 항목 | 결과 |
|---|---|
| 크림봇 `.env` 무결성 | ✅ 지문 `ba4c23328502f7d8bc78580dec752476` 시험 전후 동일 |
| 크림봇 저장소 변경 | ✅ 0건 |
| 설치 | ✅ `npm i omniroute` 1,165 패키지 / 2분 |
| 기동 | ✅ `omniroute serve --daemon --no-open --no-tray` (7.4초) |
| 왕복 | ✅ HTTP 200 / 2.09초 / 모델 `big-pickle` / API키 0개 |
| **검증 품질** | ✅ **무료 모델이 실제 버그를 정확히 포착** (아래) |

**품질 실증**: 그날 실제로 있었던 `task_intake_guard` 파서 버그(첫 줄만 읽어 YAML 블록 스칼라가
증발)를 던졌더니 무료 키리스 모델이 정확히 지목했다 —
> "YAML multi-line literal block scalar (`|`). The line `description: |` sets `desc` to `"|"`,
> discarding the actual content."

무료 티어로도 실제 결함을 잡는다는 증거. **이 시험은 다시 할 필요 없다.**

### 🔴 발견 1 — 기본 바인딩이 위험하다 (구현 시 반드시 반영)
기동 로그 원문:
> `⚠ SECURITY: listening on 0.0.0.0 with NO API-key requirement — the inference plane (/v1/*)
> is reachable by ANY device that can route to this host, and requests are billed to your
> configured providers.`

WSL2 라 Windows 호스트 네트워크에 노출된다. **`OMNIROUTE_SERVER_HOST=127.0.0.1` 강제.**
잠근 뒤 실측: `LISTEN 127.0.0.1:20128` / 외부 IP(`172.22.168.177`) 접근 `HTTP 000` = 차단.

### 🔴 발견 2 — "빈 리포트" 구멍이 **첫 시도에 재현됐다**
무료 모델이 추론 토큰을 다 쓰고 `content: None` 반환 (`finish_reason: length`).
내용은 `reasoning_content` 에만 있었다. 구현 시 필수:
- `max_tokens` 를 **넉넉히** (300 → 4,000. 300 으로는 추론만 하다 끝난다)
- `content` 가 비면 **`reasoning_content` 폴백** → 그것도 비면 **미검증 취급 + 상위 모델 승격 1회**

### ⚠️ 발견 3 — postinstall 이 `.env` 를 만든다
`node_modules/omniroute/.env` (3,033줄, JWT 키 자동생성). **자기 패키지 안**이라 크림봇 `.env`
와 무관하다. 위험하지 않다.

### ⚠️ 발견 4 — 시험 설치는 세션 스크래치패드라 사라진다
새 세션은 스크래치패드 경로가 바뀐다. **구현 시엔 durable 경로에 설치할 것**
(`npm i -g omniroute` 또는 `~/.local/omniroute`). 시험 설치를 재사용하려 하지 마라.

---

## 다음 작업 (1단계부터)

### 1. `src/ops/review_backend.py` (신규) — 백엔드 추상화
`run_review(mode, prompt, tier) -> ReviewResult`, 구현 2개:
- `CodexBackend` — 현행 `subprocess` 경로 이관 (해지 전까지 유지)
- `OmniRouteBackend` — `127.0.0.1:20128/v1/chat/completions`. **발견 1·2 반영 필수.**
  미기동/타임아웃은 **명시적 실패**(조용히 통과 금지).

선택: env `KREAM_REVIEW_BACKEND=codex|omniroute` (기본 omniroute, 미기동 시 codex 폴백).

### 2. `scripts/codex_collab.py` — 호출부만 교체
`subprocess.run([...codex...])` → `review_backend.run_review(...)`.
**큐(`reports/codex/backlog.json`)·리포트 저장·`snapshot_id`·`build_prompt`·CLI 서브커맨드·
파일명은 그대로.**

### 3. 페이로드 빌더
`build_prompt()` 에 diff 추가 — 변경 hunk + **주변 ±40줄**.
제외: `.env*` · `data/musinsa_session.json` · `*secret*` · `*credential*` · `*token*`.
**기존 `src/ops/codex_child_env.py` 의 env allowlist 재사용** (Discord/무신사 차단 로직 이미 있음).

### 4. `src/ops/codex_gate.py` — 추가만
- `_S_DIFF_TERMS` (diff 본문 검사 → S 승격)
- `tier_to_model(tier) -> ModelSpec`
- **`classify_tier` / `plan_codex_collaboration` 시그니처 불변** (`tests/test_codex_gate.py` 무회귀)

### 5. `.claude/hooks/review_gate.py` (신규) — Stop 훅 자동 강제
S·A·B 미검증 시 차단. 통과 조건 = `reports/codex/` 에 이번 `snapshot_id` 리포트 + 판정 기록.
fail-open 유지 + 해제 `KREAM_REVIEW_GO=1`. `.claude/settings.json` Stop 훅 등록.

### 6. 검증이 조용히 새는 경로 봉쇄
| 구멍 | 봉쇄 |
|---|---|
| 훅이 죽는다 | fail-open 유지하되 **"게이트 고장" 크게 출력** + 그 턴 미검증 기록 |
| OmniRoute 미기동 | **통과 아니라 차단.** codex 폴백도 없으면 보고하고 정지 |
| 빈/무의미 리포트 | 근거(파일:줄) 0개면 미검증 취급 + 상위 모델 승격 1회 |
- 무료 풀 안에서도 **강한 모델 우선** 순서를 명시 고정
- `KREAM_REVIEW_GO=1` 사용 시 커밋 메시지에 흔적 + 다음 세션 시작 경고(`session_start_inject` 재사용)

### 7. 문서
`docs/ops/codex-collaboration-policy.md` 의 **수렴 규칙(재검증 1회 / 수용N·거부N·사유)은 그대로
승계**. 백엔드·등급표만 갱신. CLAUDE.md 는 스킬 줄 1개만 (≤200줄 유지).

## 손대지 않는 것
- `classify_tier` 본체 · `tests/test_codex_gate.py` 기대값
- 큐 포맷 · `reports/codex/` 경로 · CLI 서브커맨드 이름
- 수수료 공식 · 크림 실계정 경로 · `.env`
- 메인 코딩 모델은 그대로 Claude. OmniRoute 는 **적대검증 전용**

## 검증 (1단계 이후)
1. `pytest tests/test_codex_gate.py -q` → 무회귀
2. 신규 테스트: 등급→모델 매핑 · **S 승격 규칙(검수비 경로가 S 로 오르는지 고정)** · diff 제외 패턴
3. `pytest tests/test_hooks_guards.py -q` → 훅 무회귀
4. `pytest tests/ -q` → **1,649 passed** 유지, 신규 실패 0
5. 실왕복 1회 → 리포트가 `reports/codex/` 에 떨어지는지 + Stop 훅이 **실제로 막는지** 눈으로 확인
6. **구멍 3개 각각 재현해서 막히는지** (OmniRoute 끄기 / 훅 깨뜨리기 / 빈 리포트) — 생략 금지
7. pre-commit 카나리 **20/20 PASS**
