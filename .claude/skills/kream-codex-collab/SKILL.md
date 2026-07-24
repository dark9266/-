---
name: kream-codex-collab
description: Codex(OpenAI CLI)를 독립 적대검증관·조언자로 돌리는 실행 래퍼 — 검증(끝낸 코드 적대검토)·의논(방향 제2의견)·협업(난제 왕복). 사장이 "코덱스 검증해/의논해/협업해"라고 할 때만 호출한다(ChatGPT Plus 한도를 구매대행 프로젝트와 공유). 등급(모델·effort)은 자동 판정. 수수료·보관판매·크림 POST·훅 등 안전 경계 변경, 다중 파일 리팩터, 신규 파서 마무리 시 적용.
---

# 크림봇 ↔ Codex 협업 (수동 트리거 · 자동 등급)

Codex(`codex` CLI, ChatGPT Plus, read-only)를 **독립 적대검증관·조언자**로 배선한다.
Codex = 검토관이지 **집행자가 아니다**(크림 API·DB write·소싱처 fetch 직접 실행 X).

## 🔴 트리거는 수동이다
**ChatGPT Plus 계정이 하나라 구매대행 프로젝트와 한도를 공유**한다. 크림봇은 Stop 훅 자동
큐잉을 **배선하지 않았다**. 사장이 부를 때만 쓴다:
- "코덱스 검증해" → `verify`
- "코덱스랑 의논해봐" / "제2의견" → `consult`
- "코덱스랑 협업해" / "같이 풀어봐" → `collab`

내가 자율로 부르지 않는다. 다만 **부를 만한 시점이면 사장에게 한 줄 제안**은 한다
(예: "보관판매 등록 로직 바꿨습니다 — S등급 코덱스 검증 돌릴까요?").

## 실행
```bash
python scripts/codex_collab.py plan      # ★ 판정만. Codex 호출 0 · 한도 0 — 항상 먼저 이걸
python scripts/codex_collab.py verify    # 워킹트리 자동판정 → 큐잉 → 드레인
python scripts/codex_collab.py verify --paths src/profit_calculator.py --task "수수료 정정 검토"
python scripts/codex_collab.py consult "A vs B — 근거와 함께 추천"
python scripts/codex_collab.py collab  "이 난제 같이 풀자"
python scripts/codex_collab.py status    # 큐 현황
python scripts/codex_collab.py drain     # 밀린 것 1건 처리
```
**항상 `plan` 을 먼저 돌려 등급을 보고**한 뒤 실행한다 — 한도를 쓰기 전에 얼마나 쓸지 안다.

## 등급 자동 판정 (`src/ops/codex_gate.py` · 테스트로 고정)

| 등급 | 모델·effort | 무엇이 걸리나 | 근거 |
|:-:|---|---|---|
| **S** | `sol`/**xhigh** | `src/storage_sale/**` · `src/profit_calculator.py` · `src/config.py` · `.env` · `.claude/hooks/**` / 용어: 보관판매·수수료·정산·검수비·자격증명·웹훅·호출캡 | 버그 1개 = **밴 또는 실금전 손실** |
| **A** | `sol`/high | `src/models/**` 스키마·마이그레이션 / 용어: 아키텍처·설계·선택지·ADR. **의논·협업은 최소 A** | 판단 품질 우선 |
| **B** | `terra`/high | **기본값** — `.py`/`.sh` 변경 또는 코드파일 3개+ (코어·크롤러·어댑터·매처) | sol 대비 1/2 크레딧 |
| **C** | `luna`/high | 요청 문구에 전수·열거·커버리지·훑기 + 코드 변경 없음 | 판단 아닌 커버리지. 1/5 크레딧 |
| **N** | 호출 없음 | 문서 · `scripts/probe_*`·`diag_*`·`repro_*` 일회용 | 한도 0 |

- **luna 에게 최종 판단을 맡기지 않는다**(열거·breadth 전용).
- 매번 luna→terra→sol 다 돌리지 않는다. 기본 terra 1회, **중대 지적만** sol 승격,
  S등급은 중간 없이 sol/xhigh.
- `sol/max` 는 자동 경로에 **없다** — xhigh 로도 결론이 안 날 때 `--tier` 없이 사람이 판단해
  1회만. 크레딧(1M 토큰 입력/출력): sol 125/750 · terra 62.5/375 · luna 25/150.

## 기존 의무 에이전트와 **병존**한다 (대체 아님)
- `profit_calculator.py`·수수료·시그널 변경 → `profit-analyzer`(내부) **+** Codex S등급(외부).
- 코어 모듈(kream.py·orchestrator.py 등) 수정 → `code-reviewer` **+** Codex B등급.
- 크롤러 수정 → `live-tester` 실동작 검증이 **먼저**다. Codex 는 코드 논리를 본다.

## 수렴 — 무한 반복 금지
Codex 는 적대검토관이지 **모든 지적을 반드시 반영해야 하는 권위가 아니다.**
- ✅ **수용** = ① 진짜 내 실수/버그를 잡았다, 또는 ② 내가 봐도 더 낫다(독립 공감).
- ❌ **거부(근거 1~2줄 명시)** = ⓐ 명시 요구·설계와 배치 ⓑ 이론적·legacy 엣지로 실 리스크
  미미 ⓒ 합리적으로 동의 안 됨. **조용한 무시 금지.**
- **멈춘다**: 핵심(안전·정확성 실버그)이 봉합되면 종료. 남은 게 거부건·deferred 뿐이면 수렴 보고.
- **봉합 후 재검증은 1회**(수렴 라운드) → 새 REAL 없으면 종료. 아키텍처급은 별건으로 분리.
- 보고 형식: **수용 N / 거부 N(각 사유) / 수렴 근거 / deferred 목록.**
- ⚠️ 크림봇 **결정된 사안**(`feedback_settled_decision_lock`)을 Codex 가 다시 열면 그건 거부
  사유 ⓐ다 — 거래량 게이트 1, 47k 전체 타겟, 푸시 단일 트랙, tier2_monitor 유지.

## 호출 위생 (사고 방지)
- 출력을 `> /dev/null` 로 **버리지 않는다** — `reports/codex/codex_<snap>.raw.log` 에 항상 남는다
  (원본 사고: 한도초과 에러를 못 보고 51분 헛대기 + 사장에게 틀린 설명).
- **호출 전 한도 프로브 금지** — 확인용 메시지 자체가 한도를 쓴다. 실제 호출 실패로 감지한다.
- **한도 초과 = fail-open**: 멈추지 말고 자체 검증(재현 시나리오 + targeted test)으로 진행하고
  "검증 밀림 N건"을 보고한다. 다음 호출 때 `drain` 으로 **현재 상태로** 처리한다.
- 미검증 상태는 보관판매 POST·실계정 작업의 GO 근거가 될 수 없다.

## 큐 함정 (실측 — 원본 프로젝트에서 하루에 전부 밟았다)
1. **드레인은 FIFO 1건씩** — 내 항목 앞에 밀린 게 있으면 그게 먼저 나간다(정상). CLI 가
   `※ 큐는 FIFO 다` 로 알려준다.
2. **조용한 성공 오인 금지** — 빈 큐도 성공도 stdout 이 비슷하다. CLI 는 `PENDING 없음` /
   `✓ 처리: <snap>` 을 명시 출력한다. `status` 로 재확인하라.
3. **동시 쓰기 증발** — flock(`codex_queue_io`)으로 봉합됨. 그래도 큐잉 직후 `status` 로 확인.

## 관련
- `docs/ops/codex-collaboration-policy.md`(정책·등급 근거) · `src/ops/codex_gate.py`(판정) ·
  `tests/test_codex_gate.py`(고정) · `reports/codex/`(산출·raw 로그)
