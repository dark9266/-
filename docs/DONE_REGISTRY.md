# DONE 원장 — 재실행 금지

> **이 문서의 목적**: "했던 일을 또 하지 않기". 세션이 바뀌면 직전 상태가 통째로 없어져
> 끝난 일을 다시 하거나 이미 판정된 결정을 다시 연다. 그걸 막는 단일 대조표다.
>
> **자동 배선**: `session_start_inject` 훅이 매 세션 시작에 아래 **Active Lock** 을 주입하고,
> `task_intake_guard` 훅이 프롬프트 토큰으로 이 파일을 grep 해 관련 항목을 눈앞에 놓는다.
> 즉 이 파일에 쓰면 다음 세션이 **읽기로 선택하지 않아도** 본다.
>
> **쓰는 법**: 작업이 끝나 재실행할 이유가 없어지면 한 줄 추가한다.
> `- [YYYY-MM-DD] <무엇> — <판정/결과>. 재개 조건: <있으면>`
> 되돌릴 수 있는 진행 중 작업은 여기 쓰지 않는다(메모리 `project_*` 가 담당).

---

## Active Lock

- [2026-04-19] **Worksout / Carhartt 플랫폼 폐기** — 백엔드에 SKU 필드 자체가 부재.
  재활성화 금지(`project_worksout_platform_permanent_dropped`).
- [2026-04-20] **On Running 매칭 확정** — 전수 검증으로 즉시 매칭분 + collect 후보 확인.
  어댑터 **유지**. 폐기·롤백 제안 금지(`project_on_running_match_blocked`).
- [2026-04-21] **큐 드레인 "handler 단일 직렬화" 오진단 정정** — recover 구간 샘플 편향이었다.
  steady-state 는 정상. 재조사 불필요(`project_queue_drain_debug`).
- [2026-04-21] **첫 실전 알림 → 매수 시도 완료** — 파이프라인 e2e 검증됨.
  알림 품질 근거로 재활용(`project_first_alert_attempt`).
- [2026-04-23] **Converse 한국 공홈 폐기** — 크림 커버율 극소 + 매칭분 거래량 전부 0.
  재시도 금지(`project_converse_rejected`).
- [2026-04-15] **거래량 게이트 = 1 고정** — 저거래(숨은 보석)가 핵심 차별화.
  낮추자/올리자 재제안 금지(`project_hidden_gem_policy`).
- [2026-04-16] **`tier2_monitor.py` 유지 판정** — 역방향 hot 폴링이지만 축 ② 보조 감시로 존치.
  "역방향이니 끄자" 제안 금지. (나머지 역방향/Tier 스캐너는 폐기 흐름 — `direction_guard` 가 차단.)
- [2026-05-01] **`price_refresher` 비활성화** — 토글 OFF + 봇 재가동 완료
  (`project_price_refresher_kill_pending`).
- [2026-05-01] **22 소싱처 cover 저하 root cause 확정** — 매칭 정확도 문제가 **아니었다**.
  원인 = 신규 cold 상품 `volume_7d` 미초기화. "매칭 정확도 조사" 재실행 금지
  (`project_cover_diagnosis_20260501`).
- [2026-05-01] **Phase B 보관판매 endpoint 확정 + B-0 dry-run 완성** — api-prober 완료.
  endpoint 재탐색 금지(`project_phase_b_storage_sale`).
- [2026-05-02] **검수비 2,500원 확정** — 실 정산서 확인. 수수료 공식 재추정 금지.
- [2026-07-24] **구매대행 자산 이식** — Fable 오케스트레이션 · 가드 훅 3종 ·
  서치인서치/코덱스협업/인스타/유튜브 스킬. 재이식 금지(이 커밋 참조).
- [2026-07-25] **1c 정확성 게이트 완성 (faafc60..4100310)** — SourceStockSnapshot 상태모델 ·
  runtime 중앙 게이트(UNKNOWN/만료 → verification_pending 보류, kream_delta 면제) ·
  무신사/on_running 재고 증거 배선 · 체크포인트 재생 신선도/복원. 코덱스 2라운드 수렴
  (7건 전부 봉합) + 리뷰 4회 Approved + live-tester 2회. 재설계 재론 금지.
- [2026-07-25] **on.com 사이즈 실재고 = PDP `__NUXT_DATA__`** — "spree API 로그인 차단"
  과거 기록은 오판정(api-prober 실측). 추가 요청 0·httpx 충분. 재탐색 금지.

---

## 판정 완료 — 다시 열지 말 것 (Settled Decision Lock)

`feedback_settled_decision_lock` 기준. 재개 예외 = ①구체 증거 ②완성도 축 기여 ③판정 명시,
**3개 전부** 충족할 때만.

- **목표 = 크림 47k 전체 × 소싱처 교집합 / 방법 = 푸시.** "역방향이 원래 의도 아니냐" 금지.
- **타겟 축소 금지** — hot 130건만 / 거래량 ≥5 / 인기 카테고리만 / 신발만, 전부 금지.
- **단계 순서**: 22 소싱처 안정화 → **그 다음** 소싱처 확장. 뒤집기 금지.
- **소싱처는 한국 공홈만** — EU/US/JP 금지.
- **수익 계산은 실시간 `sell_now`** — 로컬 DB 시세 금지.
- **Codex 는 크림봇에서 수동 트리거 전용** — Plus 한도를 구매대행과 공유(2026-07-24 사장 결정).
- **봇 가동/중지는 UI 봇 단독** — 자동 시작 영구 금지(`feedback_bot_control_ui_only`).

---

## Deferred — 지금 하지 않기로 한 것 (재개 조건 있음)

- **사이즈별 최근 체결가 수익 계산** — 안정화 후 별도 작업(`project_future_size_trade_price`).
- **등록 모드 + 세일 캘린더** — Stage 5 완주 전 재개·선제안 금지(`project_ask_mode_deferred`).
- **구매대행 솔루션 신규 저장소** — 크림봇 완주 후. 크림봇과 연결 제안 금지
  (`project_future_proxy_buying`).
- **Camoufox 도입** — `kream-search-in-search` 3단계까지 다 해보고도 막혔을 때만 제안.
