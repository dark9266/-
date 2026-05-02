# UI 봇 Phase A Hybrid Merge Plan

날짜: 2026-05-01  
대상 브랜치: `master` (HEAD `a90ce51`) ← `feature/ui-bot-phase-a` (HEAD `5a92879`)  
전략: **A 안** — dual-scenario(2순위 예상매수) 폐기 + UI 봇 기능 전부 보존  
예상 시간: 30~45분  

---

## 배경 및 핵심 교훈

오늘 머지 시도에서 확인된 두 가지 함정:

1. **`git checkout --ours` (master 우선) 실수**: master 의 `runtime.py` / `profit_calculator.py` 는 dual-scenario 없는 것이 맞지만, UI 봇 기능(`_is_paused_guard` / `get_adapter` / `_is_source_dispatch_allowed`)도 없다. "ours=master" 는 "dual-scenario X + UI 봇 X" 가 된다.
2. **올바른 hybrid**: worktree 파일을 가져온 후 dual-scenario 코드만 수동으로 제거한다.

---

## 파일별 머지 전략 요약

| 파일 | 전략 | 근거 |
|------|------|------|
| `src/core/v3_alert_logger.py` | worktree 우선 (`--theirs`) | estimated/shadow/followup 코드 — 단순 삭제가 목표, 근거 아래 설명 |
| `src/core/v3_discord_publisher.py` | worktree 우선 (`--theirs`) | estimated 분기 코드 포함 — 단순 삭제 |
| `src/models/database.py` | worktree 우선 (`--theirs`) | `bot_state` / `bot_logs` 테이블 + `_run_sql_migrations` 신규. master 에 없음. |
| `src/core/scheduler.py` | worktree 우선 (`--theirs`) | `_is_paused` + `price_refresher_enabled` 둘 다 반영. 수동 확인 필요. |
| `src/core/runtime.py` | **hybrid 수술** | UI 봇 기능 보존 + dual-scenario 함수 제거 |
| `src/profit_calculator.py` | **hybrid 수술** | `apply_catch_to_buy_price` 보존 + `estimate_*` / `classify_*` / `compute_*` 제거 |
| `src/core/event_bus.py` | **hybrid 수술** | `estimated` / `recent_sales` / `conservative_price` / `recovery_days` 필드 제거 |
| `src/crawlers/kream_delta_client.py` | **hybrid 수술** | sentinel 전부-0 → None 복구 (2순위 fallback 경로 제거) |
| `src/core/orchestrator.py` | worktree 우선 (`--theirs`) | dual-aware prefilter 포함되어 있지만, prefilter 테스트 fix 후 처리 |

> **v3_alert_logger / v3_discord_publisher worktree 우선 이유**: estimated 코드를 포함한 채로 가져오지만, 이후 **step 6** 에서 estimated 관련 블록을 수동 삭제한다. conflict 자체가 estimated 분기 외의 변경(shadow mode, followup INSERT)이 주이고, master 에 없는 코드라 `--theirs` 가 오히려 안전하다.

---

## Pre-merge 체크리스트 (Step 0)

```bash
# 0-1. master 클린 확인
git -C /mnt/c/Users/USER/Desktop/크림봇 status
# → "nothing to commit, working tree clean" 이어야 함

# 0-2. HEAD 확인
git -C /mnt/c/Users/USER/Desktop/크림봇 log --oneline -3
# a90ce51 이 최신이어야 함

# 0-3. 봇 PID 확인 (kill 금지 — 사용자 컨펌 전)
ps -ef | grep "python.*main" | grep -v grep

# 0-4. 마스터 테스트 baseline 확인 (1060 pass 확인)
cd /mnt/c/Users/USER/Desktop/크림봇 && python -m pytest tests/ -q --tb=no 2>&1 | tail -5

# 0-5. worktree HEAD 확인
git -C /mnt/c/Users/USER/Desktop/크림봇 worktree list
# ui-bot-phase-a → 5a92879 이어야 함
```

**모두 OK 이어야 Step 1 진입. 하나라도 이상하면 멈추고 확인.**

---

## Step 1 — 머지 시작 (no-ff, conflict 진입)

```bash
cd /mnt/c/Users/USER/Desktop/크림봇

git merge --no-ff feature/ui-bot-phase-a \
  -m "feat(ui-bot): Phase A hybrid merge — dual-scenario 폐기 + UI 봇 전기능 보존

A 안: dual-scenario (2순위 예상매수 fallback) 폐기.
UI 봇 기능 (pause/resume, per-source toggle, mode, FastAPI 서버,
Electron 앱, WebSocket, Dashboard, Logs, Diagnostics, TargetScan) 전부 보존.

worktree: feature/ui-bot-phase-a HEAD 5a92879 (32 tasks 완주)
master base: a90ce51 (Phase 1 Dual-Anchor 머지 후)"
```

**예상 결과**: CONFLICT 발생 (7 파일). 자동 머지 실패 → "Automatic merge failed" 메시지. 정상.

확인:
```bash
git status | grep "both modified\|both added\|CONFLICT"
```

예상 conflict 파일 목록 (오늘 실험 기준):
- `src/core/event_bus.py`
- `src/core/runtime.py`
- `src/core/v3_alert_logger.py`
- `src/core/v3_discord_publisher.py`
- `src/models/database.py`
- `src/profit_calculator.py`
- `src/core/scheduler.py`

---

## Step 2 — 단순 resolve: worktree 우선 5파일

아래 5개 파일은 worktree 버전을 그대로 사용. estimated 블록 제거는 Step 5~7에서 별도로 진행.

```bash
cd /mnt/c/Users/USER/Desktop/크림봇

# v3_alert_logger — worktree 우선
git checkout --theirs src/core/v3_alert_logger.py
git add src/core/v3_alert_logger.py

# v3_discord_publisher — worktree 우선
git checkout --theirs src/core/v3_discord_publisher.py
git add src/core/v3_discord_publisher.py

# database.py — worktree 우선 (bot_state/bot_logs 신규)
git checkout --theirs src/models/database.py
git add src/models/database.py

# scheduler.py — worktree 우선 (수동 확인 후)
git checkout --theirs src/core/scheduler.py
git add src/core/scheduler.py

# orchestrator.py — worktree 우선
git checkout --theirs src/core/orchestrator.py
git add src/core/orchestrator.py
```

scheduler.py 수동 확인 (30초):
```bash
grep -n "price_refresher_enabled\|_is_paused\|pause" \
  src/core/scheduler.py | head -20
```
`price_refresher_enabled` 토글 AND `_is_paused` 체크 둘 다 존재해야 정상.

---

## Step 3 — Hybrid 수술: runtime.py

```bash
# 3-1. worktree 버전으로 시작
cd /mnt/c/Users/USER/Desktop/크림봇
git checkout --theirs src/core/runtime.py
```

**3-2. import 블록에서 dual-scenario 3개 제거**

찾아서 지울 라인:
```python
# 제거 대상 (runtime.py import 블록 안)
from src.profit_calculator import (
    apply_catch_to_buy_price,
    calculate_size_profit,
    classify_estimated_signal,   # <-- 이 줄 삭제
    compute_price_trend,          # <-- 이 줄 삭제
    determine_signal,
    estimate_conservative_price,  # <-- 이 줄 삭제
)
```

결과 (남겨야 할 import):
```python
from src.profit_calculator import (
    apply_catch_to_buy_price,
    calculate_size_profit,
    determine_signal,
)
```

**3-3. `_try_estimated_signal` 함수 전체 제거**

`_candidate_handler` 안에 정의된 inner async function:
```python
async def _try_estimated_signal(
    event: CandidateMatched, *, volume_7d: int, snapshot: dict | None,
) -> ProfitFound | None:
    ...  # 전체 블록 (약 50줄) 삭제
```

함수 body 끝은 `return ProfitFound(...)` 의 닫는 괄호 `)` 다음 빈줄.

**3-4. `kream_sell_price <= 0` 분기 교체**

제거 대상 (kream_sell_price 0 → 2순위 fallback):
```python
if kream_sell_price <= 0:
    # 1순위 (즉시판매가) 0 → 2순위 (예상매수, dual-scenario) fallback.
    # snapshot 의 recent_sales 활용 → 추가 NUXT 호출 X.
    return await _try_estimated_signal(
        event,
        volume_7d=volume_7d,
        snapshot=snapshot,
    )
```

교체 후 (원래 master 논리 복구):
```python
if kream_sell_price <= 0 or event.retail_price <= 0:
    return None
```

단, 바로 위 `if event.retail_price <= 0: return None` 라인과 합쳐야 하므로:
- 현재 worktree: `if event.retail_price <= 0: return None` 다음에 `if kream_sell_price <= 0: ...`
- 합친 후: `if kream_sell_price <= 0 or event.retail_price <= 0: return None`

**3-5. 하드 플로어 fail → 2순위 fallback 제거**

제거 대상:
```python
if (
    result.net_profit < min_profit
    or result.roi < min_roi
    or volume_7d < min_volume_7d
):
    return await _try_estimated_signal(
        event,
        volume_7d=volume_7d,
        snapshot=snapshot,
    )
```

교체 후 (원래 master 논리):
```python
if result.net_profit < min_profit:
    return None
if result.roi < min_roi:
    return None
if volume_7d < min_volume_7d:
    return None
```

**3-6. `matched_sizes` / `with_price` 분기 처리**

worktree 는 `matched_sizes` (sell_now 무관) + `with_price` (sell_now > 0) 로 분리했다.
dual-scenario 제거 후에도 `with_price = []` → `sell_now_price = 0` 경로가 남아있으면 runtime 에서 `kream_sell_price <= 0` → `return None` 이 처리하므로 동작상 문제없다.
단, `matched_sizes` 변수명은 그대로 써도 무방 (size filter 자체는 올바른 개선이므로 보존).

**3-7. `_is_paused_guard` / `get_adapter` / Phase 1.5 블록 보존 확인**

grep 으로 최종 확인:
```bash
grep -n "_is_paused_guard\|get_adapter\|Phase 1.5\|apply_catch_to_buy_price\
\|_try_estimated_signal\|classify_estimated_signal\|estimate_conservative_price" \
  src/core/runtime.py
```

기대 결과:
- `_is_paused_guard` — 존재 (함수 정의 + 호출)
- `get_adapter` — 존재 (메서드 정의)
- `Phase 1.5` — 존재 (catch hook 주석)
- `apply_catch_to_buy_price` — 존재 (import + 호출)
- `_try_estimated_signal` — **없어야 함**
- `classify_estimated_signal` — **없어야 함**
- `estimate_conservative_price` — **없어야 함**

```bash
git add src/core/runtime.py
```

---

## Step 4 — Hybrid 수술: profit_calculator.py

```bash
# 4-1. worktree 버전으로 시작
git checkout --theirs src/profit_calculator.py
```

**4-2. 제거 대상 3개 함수**

- `estimate_conservative_price()` — `_CV_THRESHOLD` 상수 포함 전체 함수
- `compute_price_trend()` — `_TREND_THRESHOLD` 상수 포함 전체 함수
- `classify_estimated_signal()` — 전체 함수

각 함수의 경계:
- `estimate_conservative_price`: `_CV_THRESHOLD = 0.15` 상수부터 함수 끝 `return min(cleaned)` 까지
- `compute_price_trend`: `_TREND_THRESHOLD = -0.10` 상수부터 함수 끝 `return (recent_avg - older_avg) / older_avg` 까지
- `classify_estimated_signal`: 함수 정의부터 `return Signal.WATCH_ESTIMATED, net_profit, roi` 까지

**4-3. `apply_catch_to_buy_price` 보존 확인**

이 함수는 Phase 1.5 Chrome 확장 catch hook. worktree 에 존재하고, master 에도 존재. 보존 필수.

```bash
grep -n "def apply_catch_to_buy_price\|def estimate_conservative_price\
\|def classify_estimated_signal\|def compute_price_trend" src/profit_calculator.py
```

기대: `apply_catch_to_buy_price` 만 존재, 나머지 3개 없음.

```bash
git add src/profit_calculator.py
```

---

## Step 5 — Hybrid 수술: event_bus.py

```bash
# 5-1. worktree 버전으로 시작
git checkout --theirs src/core/event_bus.py
```

**5-2. `ProfitFound` dataclass 에서 4개 필드 제거**

제거 대상 (한 블록):
```python
# 2순위 dual-scenario (예상매수) — 즉시판매가 0 fallback 시 최근 체결가 기반.
# estimated=True 면 kream_sell_price 슬롯에 conservative_price 저장.
# 사용자 등록 후 ~recovery_days 일 대기 판매 시나리오.
estimated: bool = False
recent_sales: tuple = ()  # ({"date","size","price"}, ...) 최신 5건
conservative_price: int | None = None
recovery_days: float | None = None  # 7 ÷ volume_7d
```

**5-3. `catch_applied` / `original_retail` 필드 및 주석은 보존**

```python
# Phase 1.5 — Chrome 확장 catch 적용 흔적
# catch_applied=True 시 retail_price 는 실측 결제가, original_retail 은
# 봇 어댑터(musinsa_httpx.py)가 이미 SSR fetch + 옵션 API 로 수집 중 →
# ...
catch_applied: bool = False
original_retail: int | None = None
```

이 블록은 그대로 둔다.

```bash
grep -n "estimated\|recent_sales\|conservative_price\|recovery_days\
\|catch_applied\|original_retail" src/core/event_bus.py
```

기대: `catch_applied`, `original_retail` 존재. `estimated`, `recent_sales`, `conservative_price`, `recovery_days` 없음.

```bash
git add src/core/event_bus.py
```

---

## Step 6 — Hybrid 수술: v3_alert_logger.py / v3_discord_publisher.py

### 6-A v3_alert_logger.py

worktree 버전이 이미 step 2에서 staged 됨. estimated 관련 코드 제거.

**제거 블록 1** — payload dict 안 4줄:
```python
# 2순위 (예상매수 dual-scenario) — UI 카드 / Discord embed 분기 데이터
"estimated": bool(getattr(event, "estimated", False)),
"conservative_price": getattr(event, "conservative_price", None),
"recent_sales": list(getattr(event, "recent_sales", ()) or ()),
"recovery_days": getattr(event, "recovery_days", None),
```

**제거 블록 2** — F5 followup INSERT 전체 (약 25줄):
```python
# F5 사후 추적 (2026-04-28) — 예상매수 알림 시 alert_followup row 추가.
if bool(getattr(event, "estimated", False)):
    try:
        import aiosqlite
        async with aiosqlite.connect(self._db_path) as db:
            ...  # 전체 블록
    except Exception:
        ...
```

`return AlertSent(...)` 는 그대로 둔다.

```bash
grep -n "estimated\|conservative_price\|recent_sales\|recovery_days\|followup" \
  src/core/v3_alert_logger.py
```

기대: 모두 없음.

```bash
git add src/core/v3_alert_logger.py
```

### 6-B v3_discord_publisher.py

worktree 버전이 step 2에서 staged 됨.

**제거 블록 1** — `_SIGNAL_COLOR` dict 2줄:
```python
# 2순위 (예상매수 dual-scenario) — 보라 분리, 한눈에 1순위와 구분
"예상매수": 0x9B59B6,
"예상관망": 0x9B59B6,
```

**제거 블록 2** — `_SIGNAL_EMOJI` dict 2줄:
```python
"예상매수": "📊",
"예상관망": "📊",
```

**제거 블록 3** — `_ALERT_SIGNALS` 에서 `"예상매수"` 제거:
```python
# 변경 전 (worktree)
_ALERT_SIGNALS: frozenset[str] = frozenset({"강력매수", "매수", "예상매수"})

# 변경 후 (master 원래 값)
_ALERT_SIGNALS: frozenset[str] = frozenset({"강력매수", "매수"})
```

**제거 블록 4** — `_build_embed` 안 estimated 분기 (`category` prefix 관련):
```python
# 제거 대상
# 카테고리 명시 — 실현 (바로 팔림) vs 기회 (등록 후 팔 가능성)
estimated = bool(getattr(event, "estimated", False))
category = "📊 기회" if estimated else "💵 실현"
title = f"{category} {emoji} [{event.signal}] {name}"[:256]

# 복구 후 (master 원래 값)
title = f"{emoji} [{event.signal}] {name}"[:256]
```

**제거 블록 5** — estimated 가격 표시 분기 (약 8줄):
```python
if estimated:
    # 2순위 (예상매수) — 즉시판매가 0, 최근 체결가 기반 보수 추정.
    price_value = (
        f"소싱가 **{event.retail_price:,}원**\n"
        f"📊 예상 판매 (보수) **{event.kream_sell_price:,}원**\n"
        f"예상 차액 **~{event.kream_sell_price - event.retail_price:,}원**"
    )
elif catch_applied and original_retail:
    ...
```

`if estimated:` 블록 제거 후 `elif catch_applied` → `if catch_applied` 로 변경.

**제거 블록 6** — `fields.append` 최근 체결 분포 블록 (약 25줄):
```python
# 2순위 (예상매수) — 최근 5건 체결 분포 + 회수 예상일
if estimated:
    recent_sales = getattr(event, "recent_sales", None) or ()
    ...
    # 전체 블록
```

**제거 블록 7** — `publish` 메서드 안 shadow mode 블록 (약 20줄):
```python
# F4 shadow mode (2026-04-28) — 예상매수 알림 첫 24h JSONL only.
if event.signal == "예상매수":
    try:
        ...
    except (ValueError, ImportError):
        pass
```

```bash
grep -n "estimated\|shadow\|예상매수\|예상관망\|conservative_price\|recent_sales\|recovery_days" \
  src/core/v3_discord_publisher.py
```

기대: 모두 없음.

```bash
git add src/core/v3_discord_publisher.py
```

---

## Step 7 — Hybrid 수술: kream_delta_client.py (sentinel 복구)

kream_delta_client.py 는 conflict 파일이 아니었을 수 있지만, worktree 에서 수정됨.

**문제**: worktree 에서 모든 사이즈가 sentinel 이면 `None` 대신 `{"sell_now_price": 0, ...}` 반환하도록 변경됨. 이는 dual-scenario fallback 경로. A 안에서는 `None` 반환이 맞다.

현재 worktree 코드 (`kream_delta_client.py`, `build_snapshot_fn` 안):
```python
if not normalized:
    # sell_now>0 사이즈 없음 → 2순위 fallback 가능하도록 sell_now=0 + sales 통과
    return {
        "sell_now_price": 0,
        "volume_7d": volume_7d,
        "size_prices": [],
        "recent_sales": recent_sales,
    }
```

**복구 후** (master 원래 값):
```python
if not normalized:
    return None
```

이 변경이 필요한 이유: `test_kream_sell_now_sentinel.py::test_all_sentinel_returns_none` 가 현재 워크트리에서도 FAIL. 복구하면 PASS.

또한, 이미 worktree 에 머지된 `recent_sales` 키는 제거:
- `build_snapshot_fn` 반환 dict 에서 `"recent_sales": recent_sales` 키 전부 제거
- 함수 본문의 `recent_sales = snap.get("recent_sales") or []` 라인 제거

```bash
grep -n "recent_sales" src/crawlers/kream_delta_client.py
```

기대: 없거나 다른 컨텍스트만 존재.

```bash
# 변경 완료 후
git add src/crawlers/kream_delta_client.py
```

---

## Step 8 — dual-scenario 전용 테스트 파일 7개 삭제

A 안에서 dual-scenario 코드가 없으므로 이 테스트들은 더 이상 의미 없음 (실행하면 ImportError / AttributeError).

```bash
cd /mnt/c/Users/USER/Desktop/크림봇

git rm tests/test_conservative_price.py      # estimate_conservative_price 단위 테스트
git rm tests/test_cv_guard.py                # F1 CV 신뢰도 게이트
git rm tests/test_estimated_signal.py        # classify_estimated_signal 단위 테스트
git rm tests/test_trend_guard.py             # F2 추세 게이트
git rm tests/test_estimated_embed.py         # Discord embed estimated 분기
git rm tests/test_estimated_fallback.py      # runtime fallback 시나리오
git rm tests/test_shadow_mode.py             # F4 shadow mode
git rm tests/test_prefilter_dual.py          # F3 dual-aware prefilter
git rm tests/test_kream_recent_sales.py      # kream fetch_recent_sales (dual-scenario 전용)
```

> 참고: `test_orchestrator_prefilter.py` 는 삭제 대상 아님. 이 파일은 원래 master 에도 있었고, dual-aware 코드를 제거한 후 3건의 fail 을 fix 한다 (Step 9).

---

## Step 9 — test_orchestrator_prefilter.py fix (3건 fail)

오늘 실험에서 3건 fail 확인:
- `test_prefilter_blocks_unprofitable`
- `test_prefilter_logs_decision`
- `test_prefilter_runs_before_throttle`

**원인**: worktree 에서 `orchestrator._prefilter_blocks()` 가 `prefilter_estimated_aware=True` (기본값) 시 1순위 fail 이라도 통과하도록 변경됨. A 안에서는 이 기능 제거.

**fix 위치**: `src/core/orchestrator.py` 의 `_prefilter_blocks` 메서드.

현재 worktree 코드:
```python
if tentative + upside < min_profit:
    # F3 dual-aware (2026-04-28) — 1순위 fail 이라도 2순위 (예상매수)
    # fallback 가능성 보호. handler 가 snapshot 받아 2순위 평가.
    if getattr(settings, "prefilter_estimated_aware", True):
        return False, tentative
    return True, tentative
```

**복구 후**:
```python
if tentative + upside < min_profit:
    return True, tentative
```

또한, `prefilter_estimated_aware` 관련 `settings` 속성이 남아 있으면 제거.

`apply_catch_to_buy_price` 제거 여부 확인: worktree 의 `orchestrator.py` 에서 이미 `apply_catch_to_buy_price` import 가 제거된 상태다 (diff 에서 확인됨 — `from src.profit_calculator import calculate_kream_fees` 로만 남음). 이 상태를 유지.

```bash
grep -n "prefilter_estimated_aware\|_try_estimated_signal\|estimated" \
  src/core/orchestrator.py
```

기대: 모두 없음.

```bash
git add src/core/orchestrator.py
```

---

## Step 10 — Signal enum 정리 (models/product.py)

dual-scenario 를 위해 `Signal.BUY_ESTIMATED` / `Signal.WATCH_ESTIMATED` 가 추가됐을 수 있음.

```bash
grep -n "BUY_ESTIMATED\|WATCH_ESTIMATED\|예상매수\|예상관망" \
  src/models/product.py
```

존재하면 해당 enum 값 제거. `_ALERT_SIGNALS` 에서도 제거됐으므로 orphan 상태.

```bash
git add src/models/product.py  # 변경된 경우만
```

---

## Step 11 — test_kream_recent_sales 관련 kream.py 정리

`fetch_recent_sales` 가 `src/crawlers/kream.py` 에 추가됐다면, A 안 에서는 이 메서드를 호출하는 곳이 없어진다. 단, kream.py 의 메서드 자체는 **보존해도 됨** (무해하며 나중에 재활용 가능).

```bash
grep -n "fetch_recent_sales\|recent_sales" src/crawlers/kream.py | head -10
```

호출처가 완전히 없어졌는지만 확인. 메서드 자체 삭제는 선택 사항.

---

## Step 12 — `estimated_alert_followup` 테이블 migration 파일 삭제

```bash
ls src/migrations/
# 확인 후
grep -l "estimated_alert_followup" src/migrations/*.sql
```

존재하면 `git rm` 으로 삭제:
```bash
git rm src/migrations/<해당파일>.sql
```

> `0001_coupon_catches.sql` 는 Chrome 확장 catch 관련 — 삭제하지 않는다. Phase 1.5 기능이므로 보존.

---

## Step 13 — config.py prefilter_estimated_aware 속성 제거

```bash
grep -n "prefilter_estimated_aware\|estimated" src/config.py
```

존재하면 해당 속성 제거.

```bash
git add src/config.py  # 변경된 경우만
```

---

## Step 14 — 나머지 worktree 신규 파일 전부 stage

Electron 앱, FastAPI 서버, extension, scripts 등 UI 봇 핵심 파일은 conflict 없이 자동 머지됐을 것이다. 확인:

```bash
git status | grep "^A\|unmerged\|new file"
```

남은 unmerged 파일이 있으면 각각 처리:
```bash
# 패턴: electron/, src/api/, extension/ 는 전부 worktree 우선
git checkout --theirs <파일>
git add <파일>
```

---

## Step 15 — 1차 검증: pytest 전체

```bash
cd /mnt/c/Users/USER/Desktop/크림봇
python -m pytest tests/ -q --tb=short 2>&1 | tail -30
```

**기대**: 1060+ PASS, 0 FAIL.

실패 시 분류:

| 실패 패턴 | 원인 | fix |
|-----------|------|-----|
| `ImportError: cannot import name 'classify_estimated_signal'` | 어딘가 import 잔재 | grep 으로 찾아 제거 |
| `AttributeError: 'ProfitFound' has no attribute 'estimated'` | event_bus estimated 필드 잔재 참조 | 해당 코드 제거 |
| `test_all_sentinel_returns_none FAILED` | kream_delta_client sentinel 미복구 | Step 7 재실행 |
| `test_prefilter_blocks_unprofitable FAILED` | orchestrator estimated_aware 미복구 | Step 9 재실행 |
| `test_prefilter_logs_decision FAILED` | 동상 | Step 9 재실행 |

---

## Step 16 — 2차 검증: verify.py

```bash
PYTHONPATH=. python scripts/verify.py
```

**기대**: 파이프라인 전 구간 OK.

---

## Step 17 — 머지 커밋 완성

모든 staged 파일 확인:
```bash
git status
# "All conflicts fixed but you are still merging." 상태여야 함
```

```bash
git commit --no-edit
# 또는 Step 1에서 -m 으로 메시지 지정했으므로 그대로 사용
```

커밋 후 확인:
```bash
git log --oneline -5
git show --stat HEAD
```

---

## Step 18 — 봇 재시작 (사용자 컨펌 필수)

**이 단계는 사용자 직접 컨펌 후 진행. Claude 단독 실행 금지.**

```bash
# 현재 봇 PID 확인
ps -ef | grep "python.*main" | grep -v grep
# → PID 73846 (또는 현재 PID)

# 사용자 OK 후
kill 73846
sleep 2
# 새 세션에서 재가동
cd /mnt/c/Users/USER/Desktop/크림봇
nohup python main.py > logs/bot.log 2>&1 &
echo "봇 PID: $!"
```

---

## Step 19 — 헬스체크 4종

재가동 5분 후:

```bash
# 1) 프로세스
ps -ef | grep "python.*main" | grep -v grep

# 2) 마지막 알림 (24h 내)
sqlite3 data/kream_monitor.db \
  "SELECT alert_id, fired_at FROM alert_sent ORDER BY fired_at DESC LIMIT 3"

# 3) 크림 일일 호출 (TEXT datetime 컬럼)
sqlite3 data/kream_monitor.db \
  "SELECT COUNT(*) FROM kream_api_calls WHERE ts > datetime('now','-1 day')"

# 4) 파이프라인 활동 2h (epoch float 컬럼)
sqlite3 data/kream_monitor.db \
  "SELECT stage, COUNT(*) FROM decision_log
   WHERE ts > strftime('%s','now')-7200
     AND stage IN ('dedup_recent','prefilter_unprofitable','profit_emitted','alert_sent')
   GROUP BY stage"
```

4종 모두 OK → 머지 완료.

---

## Step 20 — worktree 정리

```bash
cd /mnt/c/Users/USER/Desktop/크림봇

git worktree remove .worktrees/ui-bot-phase-a --force
git branch -d feature/ui-bot-phase-a
```

> `phase1-dual-anchor` worktree 는 별도 브랜치이므로 건드리지 않는다.

---

## Step 21 — Discord 웹훅 알림

```bash
source .env
curl -s -X POST "$DISCORD_NOTIFY_WEBHOOK" \
  -H "Content-Type: application/json" \
  -d '{"content": "UI 봇 Phase A hybrid merge 완료 — dual-scenario 폐기 + UI 봇 전기능 master 반영. 봇 재가동 완료."}'
```

---

## 체크리스트 요약 (실행 순서)

- [ ] **Step 0** Pre-merge 체크 (5건)
- [ ] **Step 1** `git merge --no-ff feature/ui-bot-phase-a` (conflict 진입)
- [ ] **Step 2** 5파일 `--theirs` resolve (v3_alert_logger / v3_discord_publisher / database / scheduler / orchestrator)
- [ ] **Step 3** runtime.py hybrid (import 3개 제거 / `_try_estimated_signal` 제거 / `kream_sell_price<=0` 분기 복구 / 하드 플로어 3줄 복구)
- [ ] **Step 4** profit_calculator.py hybrid (3개 함수 제거, `apply_catch_to_buy_price` 보존)
- [ ] **Step 5** event_bus.py hybrid (4개 필드 제거, `catch_applied` / `original_retail` 보존)
- [ ] **Step 6** v3_alert_logger estimated 블록 제거 / v3_discord_publisher estimated 7개 블록 제거
- [ ] **Step 7** kream_delta_client.py sentinel 복구 (`sell_now=0` → `return None`)
- [ ] **Step 8** `git rm` 9개 테스트 파일
- [ ] **Step 9** orchestrator.py `_prefilter_blocks` estimated_aware 제거 (3 fail fix)
- [ ] **Step 10** Signal enum `BUY_ESTIMATED` / `WATCH_ESTIMATED` 제거
- [ ] **Step 11** kream.py `fetch_recent_sales` 호출처 확인 (메서드 자체 보존 OK)
- [ ] **Step 12** migrations estimated_alert_followup .sql 제거
- [ ] **Step 13** config.py `prefilter_estimated_aware` 제거
- [ ] **Step 14** 나머지 unmerged 파일 처리
- [ ] **Step 15** `pytest tests/ -q --tb=short` → 1060+ PASS
- [ ] **Step 16** `PYTHONPATH=. python scripts/verify.py` → OK
- [ ] **Step 17** `git commit --no-edit`
- [ ] **Step 18** 봇 재시작 (사용자 컨펌)
- [ ] **Step 19** 헬스체크 4종
- [ ] **Step 20** worktree 정리
- [ ] **Step 21** Discord 웹훅 알림

---

## 주의사항 및 사전 확인 항목

1. **봇 PID 73846**: Step 18 까지 kill 금지. 사용자 컨펌 없이 Claude 단독 kill 금지.
2. **`--theirs` 위험**: scheduler.py 를 worktree 우선으로 받았을 때 `price_refresher_enabled` 토글 누락 가능. Step 2 수동 grep 확인이 필수.
3. **kream_delta_client.py conflict 여부**: 오늘 실험에서 conflict 파일 목록에 없었을 수 있음. `git status` 로 확인 후 unmerged 이면 `--theirs` 후 Step 7 수술 진행. 이미 auto-merged 됐으면 바로 Step 7 수술만.
4. **`test_kream_recent_sales.py` 삭제**: `kream.py::fetch_recent_sales` 메서드는 남겨도 무방. 다른 테스트가 이를 임포트하지 않는 한 문제없음.
5. **`0001_coupon_catches.sql` 보존**: Chrome 확장 catch 관련 migration. 절대 삭제 금지.
6. **Electron 파일 auto-merge**: worktree 신규 파일(electron/, src/api/, extension/)은 conflict 없이 자동 머지됨. Step 14 에서 누락 없는지 확인만.
7. **phase1-dual-anchor worktree**: `.worktrees/phase1-dual-anchor` 는 이 머지와 무관. 건드리지 않는다.

---

## 참고: 삭제 대상 테스트 파일 vs 보존 테스트 파일

### 삭제 (dual-scenario 전용)
```
tests/test_conservative_price.py
tests/test_cv_guard.py
tests/test_estimated_signal.py
tests/test_trend_guard.py
tests/test_estimated_embed.py
tests/test_estimated_fallback.py
tests/test_shadow_mode.py
tests/test_prefilter_dual.py
tests/test_kream_recent_sales.py
```

### 보존 (UI 봇 기능 테스트 — dual-scenario 무관)
```
tests/api/test_*.py                   # FastAPI 라우터 전체
tests/core/test_runtime_pause.py      # _is_paused_guard (UI Phase A1)
tests/test_runtime_get_adapter.py     # get_adapter() (타겟 스캔)
tests/test_scheduler_pause.py         # scheduler pause 통합
tests/test_url_dispatch.py            # URL→adapter 매핑
tests/test_target_scan_router.py      # 타겟 스캔 API
tests/test_nike_scan_url.py           # 나이키 URL 스캔
tests/test_coupon_catches_migration.py # Chrome 확장 catch DB
tests/test_coupon_router.py           # catch API 라우터
tests/test_coupon_store.py            # coupon CRUD
tests/test_main_api_launch.py         # FastAPI 서버 시작
tests/test_dashboard_health.py        # 대시보드 헬스
tests/test_price_refresher_disabled.py # price_refresher 비활성
tests/test_formatter_dual_anchor.py   # Phase 1 dual-anchor embed
tests/test_profit_calculator_with_catch.py  # catch hook 수익 계산
```
