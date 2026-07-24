#!/usr/bin/env python3
"""Stop 훅 — **증거 없는 '완료' · 증거 없는 '안 됨' · 증거 없는 '수치' 를 끝내지 못하게 막는다.**

## 왜 (앤트로픽 공식 진단)
> "'절대 하지 마라' 는 **지시가 아니라 훅+권한**으로 하라 — 긴 세션에서, 압박받을 때,
>  모호한 상황에서 모델은 프롬프트된 규칙을 못 지킨다."
> "**예외를 용납 못 하는 규칙은 마크다운이 아니라 훅에 있어야 한다.**"

크림봇 CLAUDE.md 에 **"라이브 관측 없이 수치 주장" 금지**가 명시돼 있는데도 실사고가 났다:
  · 2026-04-26 — `decision_log.ts` 를 `datetime()` 으로 비교(epoch vs text)해 **항상 False**.
    "파이프라인 정지"로 5시간 오진단. **쿼리 결과를 안 믿고 결론을 먼저 냈다.**
  · 2026-05-01 — cover 저하를 "매칭 정확도 문제"로 단정. 진짜 원인은 신규 cold 상품
    `volume_7d` 미초기화였다. **반증 쿼리를 안 돌렸다.**

**규칙은 프롬프트에 있었고 나는 못 지켰다.** → 기계가 막는다.

## 4가지 게이트
1. **완료 주장** — 이번 턴에 뭔가 바꿨는데 그 변경을 확인한 **성공한** 검증이 없다.
2. **차단 주장** — "안 된다"인데 실제로 돌려서 나온 **실패 출력**이 없다.
3. **돌파 주장** — "뚫었다"인데 실 데이터 동반 **성공 출력**이 없다.
4. **수치 주장**(크림봇 고유) — 매칭/커버/알림/거래량/수익을 숫자로 단언했는데
   이번 턴에 **읽은 것이 아무것도 없다**.

fail-open: 예외는 전부 통과. 턴당 차단 상한 2회(무한루프 방지).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_util import bash_writes_a_file, subagent_transcript_files  # noqa: E402

# ── 주장 탐지 ────────────────────────────────────────────────────────────────
_SUCCESS_CLAIM = re.compile(
    r"(봉합\s*(완료|했습니다|됐습니다)|수정\s*완료|반영\s*완료|적용\s*완료|"
    r"복구\s*(완료|성공)|해결\s*(했습니다|됐습니다|완료)|고쳤습니다|끝냈습니다|"
    r"차단(했습니다|됐습니다)|막았습니다|정상\s*(작동|동작)합니다|성공했습니다|\bDONE\b)",
    re.I,
)
_BLOCKED_CLAIM = re.compile(
    r"(안\s*됩니다|안\s*된다|불가능합니다|불가합니다|막혀\s*있습니다|"
    r"할\s*수\s*없습니다|못\s*합니다|포기합니다|\bBLOCKED\b)",
    re.I,
)
_WORKS_CLAIM = re.compile(
    r"(뚫렸|뚫었|돌파(했|됐|함|됨|에\s*성공)|\bcracked\b|봇월.{0,4}(통과|우회)했|매칭(됐|됨|성공))",
    re.I,
)
# 자기정정·미확인·과거가정은 단언이 아니다
_WORKS_CORRECTION = re.compile(
    r"(틀렸|오판|미확인|아니었|했는데|였는데|지어냈|잘못\s*(봤|읽|판단|짚))", re.I,
)
# ★ 주장이 **아닌** 것 — 초판류 훅이 정상 문장을 막아 사람이 훅을 꺼버리는 게 최악이다.
_NOT_A_CLAIM = re.compile(
    r"(불가피|"                            # "중단은 불가피합니다" = 판단
    r"는지\s*(확인|검증|보|재확인)|"        # "성공했는지 확인하겠습니다" = 미래형
    r"고\s*볼\s*수\s*없|"                  # "아직 고쳤다고 볼 수 없습니다" = 부정
    r"승인\s*없이|GO\s*없이|컨펌\s*없이|"   # 정당한 게이트 안내
    r"확인하겠|재확인하겠|보겠습니다|하겠습니다|해야\s*합니다)",  # 미래형·권고
    re.I,
)
# 운영 대상 명사 — 개념 문답("훅은 판단을 못 합니다")을 운영성 "안 된다"로 오인하지 않기 위함
_SOURCES = (
    "musinsa", "무신사", "29cm", "nike", "나이키", "adidas", "아디다스", "kasina", "카시나",
    "abcmart", "그랜드스테이지", "온더스팟", "tune", "튠", "eql", "nbkorea", "뉴발란스",
    "salomon", "살로몬", "arcteryx", "아크테릭스", "vans", "반스", "wconcept", "온러닝",
    "on_running", "stussy", "스투시", "asics", "puma", "kream", "크림",
)
_OPERATIONAL_NOUN = re.compile(
    r"(https?://|\bHTTP\b|\bAPI\b|\b[45]\d{2}\b|\.py\b|\.sh\b|\.json\b|"
    r"src/|scripts/|tests/|sell_now|__NUXT|curl_cffi|akamai|cloudflare|datadome|봇월|"
    + "|".join(re.escape(s) for s in _SOURCES) + r")",
    re.I,
)

# ★ 크림봇 고유 — 실측 없이 단언하는 **수치**. CLAUDE.md "라이브 관측 없이 수치 주장" 금지.
_METRIC_CLAIM = re.compile(
    r"(매칭|매칭율|매칭률|커버|커버율|커버리지|알림|거래량|순수익|수익|후보|덤프|적중|호출량)"
    r"\D{0,6}[\d,]+(?:\.\d+)?\s*(건|개|%|퍼센트|원)"
    r"|[\d,]+(?:\.\d+)?\s*(건|개|%)\s*(매칭|커버|알림|덤프|적중)",
)
# 출처를 밝힌 인용은 실측 주장이 아니다
_METRIC_CITED = re.compile(
    r"(CLAUDE\.md|메모리|메모|문서|커밋|원장|스펙|계획|기준|이전|과거)", re.I
)

# ── 증거 탐지 ────────────────────────────────────────────────────────────────
# (`git log`·맨 `SELECT` 나열은 제외 — 변경을 **확인**하는 행위가 아니다)
_VERIFY_CMD = re.compile(
    r"(pytest|scripts/verify\.py|ruff\s+check|sqlite3|FROM\s+\w+|"
    r"aiosqlite|execute\(|fetchall|readback|재조회|sell_now)",
    re.I,
)
# ★ 파일 편집 도구 **자체가 변경**이다. Bash 명령만 보면 Write/Edit 로 고친 뒤
#   "변경 전에 돌린 pytest" 가 증거로 인정되는 구멍이 생긴다.
_MUTATION_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_MUTATION = re.compile(
    r"(--apply|_GO=1|git\s+commit|UPDATE\s+\w+|INSERT\s+INTO|DELETE\s+FROM)", re.I
)
_VERIFY_FAILED = re.compile(r"(FAILED|failed,|Traceback|Error:|✗|no tests ran)", re.I)
# pytest **exit 5(no tests ran)** 는 실패 패턴에 안 걸리지만 성공도 아니다 —
# 명시적 `N passed` 가 있어야만 증거로 친다(애매한 결과 = 증거 아님, fail-closed).
_PYTEST_INVOKE = re.compile(r"\bpytest\b", re.I)
_PYTEST_PASSED = re.compile(r"\b\d+\s+passed\b", re.I)
_ATTEMPT_FAILURE = re.compile(
    r"(HTTP\s*[45]\d\d|\b40[0-9]\b|\b50[0-9]\b|Traceback|Error|FAILED|"
    r"timeout|refused|denied|exit\s*(code\s*)?[1-9])",
    re.I,
)
# 돌파 주장의 유일한 근거 = **실 데이터 동반 성공 출력**(200 만으론 챌린지 셸일 수 있다)
_ATTEMPT_SUCCESS = re.compile(
    r"(\"@type\"\s*:\s*\"Product\"|\bProductGroup\b|__NUXT_DATA__|"
    r"\bproducts?\s*[=:]\s*[1-9]|\b[1-9]\d*\s+passed\b|model_number|sell_now)",
    re.I,
)

MAX_BLOCKS_PER_TURN = 2


# ═════════════════════════════════════════════════════════════════════════════
# 순수 판정 (테스트가 직접 부른다)
# ═════════════════════════════════════════════════════════════════════════════
def is_human_turn(entry: dict) -> bool:
    """★ 진짜 **사람 메시지**인가.

    transcript 의 `type=user` 대부분은 `tool_result` 가 위장한 것이다 — 그걸 사람 턴으로
    오인하면 턴 경계가 잘려 검증 실행이 통째로 버려진다.
    """
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        kinds = {c.get("type") for c in content if isinstance(c, dict)}
        return "tool_result" not in kinds
    return False


# 문장 분리 — 마침표로 자르되 **숫자 사이의 점은 소수점**이라 자르지 않는다
# ("커버율 3.1% 입니다" 가 "커버율 3" / "1% 입니다" 로 쪼개져 수치 주장이 증발했다).
_SENTENCE_SPLIT = re.compile(r"(?<!\d)\.(?!\d)|[\n。!?]")


def _claim_lines(text: str):
    return _SENTENCE_SPLIT.split(text or "")


def claims_success(text: str) -> bool:
    """'완료/성공' 을 **단언**했는가 (미래형·부정·불가피는 주장이 아니다)."""
    return any(
        _SUCCESS_CLAIM.search(ln) and not _NOT_A_CLAIM.search(ln) for ln in _claim_lines(text)
    )


def claims_blocked(text: str) -> bool:
    """'안 된다/불가' 를 **단언**했는가 (운영 대상 명사가 같은 문장에 있을 때만).

    개념 문답("훅은 판단을 못 합니다")은 애초에 검증할 "시도"가 없다 — 막으면 오탐이다.
    """
    return any(
        _BLOCKED_CLAIM.search(ln) and not _NOT_A_CLAIM.search(ln) and _OPERATIONAL_NOUN.search(ln)
        for ln in _claim_lines(text)
    )


def claims_works(text: str) -> bool:
    """'뚫었다/돌파했다/매칭됐다' 를 **단언**했는가 (운영 대상 명사 동반 시에만)."""
    return any(
        _WORKS_CLAIM.search(ln)
        and not _NOT_A_CLAIM.search(ln)
        and not _WORKS_CORRECTION.search(ln)
        and _OPERATIONAL_NOUN.search(ln)
        for ln in _claim_lines(text)
    )


def claims_metric(text: str) -> bool:
    """매칭/커버/알림/거래량/수익을 **숫자로 단언**했는가 (출처 인용은 제외)."""
    return any(
        _METRIC_CLAIM.search(ln) and not _METRIC_CITED.search(ln) and not _NOT_A_CLAIM.search(ln)
        for ln in _claim_lines(text)
    )


def is_mutation(event: dict) -> bool:
    """이 도구 호출이 **변경**인가 — 편집 도구 자체 · 변경성 명령 · Bash 파일쓰기."""
    if event.get("kind") != "tool_use":
        return False
    if event.get("name") in _MUTATION_TOOLS:
        return True
    text = event.get("text") or ""
    if _MUTATION.search(text):
        return True
    # `sed -i`/`cp`/`mv`/redirect/`tee` 로 파일을 고치고 "봉합 완료"가 무검증 통과하던 구멍
    try:
        cmd = json.loads(text).get("command", "") if text else ""
    except (ValueError, AttributeError):
        return False
    return bool(cmd) and bash_writes_a_file(cmd)


def has_causal_verification(events: list[dict]) -> bool:
    """★ **마지막 변경 이후에** 실행된 **성공한** 검증이 있는가.

    짝짓기는 `tool_use_id` 로 한다 — "다음 이벤트가 결과"라고 보면 **병렬 도구 호출**에서
    다른 도구의 실패 결과와 잘못 짝지어 통과하거나 정상 검증을 실패로 오인한다.
    """
    results = {e.get("id"): e for e in events if e.get("kind") == "tool_result" and e.get("id")}

    last_mut = -1
    for i, e in enumerate(events):
        if is_mutation(e):
            last_mut = i

    for i, e in enumerate(events):
        if e.get("kind") != "tool_use" or not _VERIFY_CMD.search(e.get("text") or ""):
            continue
        if i < last_mut:
            continue  # 변경 **전**의 검증은 그 변경을 확인 못 한다
        res = results.get(e.get("id"))
        if res is None:  # id 로 못 찾으면 바로 뒤 결과로 폴백(하위호환)
            nxt = events[i + 1] if i + 1 < len(events) else None
            res = nxt if (nxt and nxt.get("kind") == "tool_result") else None
        if res is None:
            continue  # 결과가 아예 없으면 증거가 아니다(누락 ≠ 성공)
        out = res.get("text") or ""
        if _VERIFY_FAILED.search(out):
            continue  # **실패한 검증**은 증거가 아니다
        if _PYTEST_INVOKE.search(e.get("text") or "") and not _PYTEST_PASSED.search(out):
            continue  # pytest 는 명시적 `N passed` 만 인정
        return True
    return False


def has_real_attempt_failure(events: list[dict]) -> bool:
    """실제로 **돌려서 실패한 출력**이 있는가 ('안 된다'의 유일한 근거)."""
    return any(
        e.get("kind") == "tool_result" and _ATTEMPT_FAILURE.search(e.get("text") or "")
        for e in events
    )


def has_real_attempt_success(events: list[dict]) -> bool:
    """돌파 주장의 근거 = **실 데이터 동반 성공 출력**이 있는가."""
    return any(
        e.get("kind") == "tool_result"
        and _ATTEMPT_SUCCESS.search(e.get("text") or "")
        and not _VERIFY_FAILED.search(e.get("text") or "")
        for e in events
    )


def has_any_read(events: list[dict]) -> bool:
    """이번 턴에 **무엇이든 읽었는가** — 수치 주장의 최소 근거."""
    return any(
        e.get("kind") == "tool_result" and len((e.get("text") or "").strip()) > 20
        for e in events
    )


def evaluate(my_text: str, events: list[dict]) -> tuple[bool, str]:
    """(block?, 이유). **순수**."""
    if not (my_text or "").strip():
        return False, ""

    # ★ "완료" 검증은 **이번 턴에 실제로 뭔가를 바꿨을 때만** 요구한다.
    #   안 바꿨으면 그 "완료"는 과거 작업 **보고**지 새 주장이 아니다 — 보고 턴을 막으면
    #   사람이 훅을 꺼버린다(오탐이 나면 장치가 죽는다).
    #   ⚠️ 단 "안 된다"·"수치" 검사는 이 면제를 받지 않는다. 조사 턴은 대개 변경이 없고,
    #     거기가 정확히 "안 된다"와 "N건입니다"를 남발하는 자리다.
    changed = any(is_mutation(e) for e in events)

    if changed and claims_success(my_text) and not has_causal_verification(events):
        return True, (
            "이번 턴에 **'완료/성공/봉합/복구'** 를 단언했는데 "
            "**그 변경을 확인한 검증 실행이 없다**.\n"
            "  · 2026-04-26 실사고: `decision_log.ts`(epoch)를 `datetime()`과 비교해 항상 False —\n"
            "    '파이프라인 정지' 로 **5시간 오진단**. 쿼리를 믿지 않고 결론을 먼저 냈다.\n"
            "**'바꿨다' ≠ '목표 상태가 됐다'.**\n"
            "→ 끝내기 전에 **적용한 뒤 다시 읽어라**: `pytest` · "
            "`PYTHONPATH=. python scripts/verify.py` · DB 재조회.\n"
            "  **변경 이후**에 실행돼야 하고 **성공**해야 한다(pytest 는 `N passed` 필수)."
        )

    if claims_blocked(my_text) and not has_real_attempt_failure(events):
        return True, (
            "이번 턴에 **'안 된다/불가/막혔다'** 를 단언했는데 "
            "**실제로 시도한 실패 출력이 없다**.\n"
            "사장 규범(`feedback_no_workarounds`): **막히면 해결이지 우회가 아니다.** "
            "보수적으로 '안 된다' 금지 — 실제로 확인해서 안 됐을 때만 안 된다고 해라.\n"
            "→ 실제로 시도하고 **그 실패 출력**(403/Traceback/exit≠0)을 보여라.\n"
            "  아직 안 써본 방법이 있으면 **그걸 먼저 써라**(`kream-search-in-search` 4단계 "
            "escalation). 권한이 없으면 '불가' 가 아니라 **사장 1-action 요청**이다."
        )

    if claims_works(my_text) and not has_real_attempt_success(events):
        return True, (
            "이번 턴에 **'뚫었다/돌파했다/매칭됐다'** 를 단언했는데 "
            "**실 데이터를 동반한 성공 출력이 없다**(status 200 만으론 챌린지 셸일 수 있다).\n"
            "**돌파 주장 전 3확인**: ①이미 되던 것 아닌가(베이스라인) ②반증 쿼리를 이번 턴에 "
            "돌렸나 ③실물(product 개수·`model_number`·`N passed`)을 셌나.\n"
            "→ 실제로 가져와 **실 데이터**를 보여라. 아니면 '미확인' 이라고만 해라."
        )

    if claims_metric(my_text) and not has_any_read(events):
        return True, (
            "이번 턴에 **매칭/커버/알림/거래량/수익을 숫자로 단언**했는데 "
            "**이번 턴에 읽은 것이 아무것도 없다**.\n"
            "CLAUDE.md 금지: **라이브 관측 없이 수치 주장.**\n"
            "  · 2026-05-01 실사고: cover 저하를 '매칭 정확도' 로 단정 → 진짜 원인은 신규 cold "
            "상품 `volume_7d` 미초기화. **반증 쿼리를 안 돌렸다**.\n"
            "→ DB 를 실제로 조회하고 그 출력을 근거로 말하라. "
            "(⚠️ `decision_log.ts`·`alert_sent.fired_at` = epoch float → "
            "`strftime('%s','now')-N` / `kream_api_calls.ts` = TEXT → `datetime('now','-1 day')`.\n"
            "  컬럼별 `SELECT typeof(ts) …` 로 먼저 확인.)\n"
            "출처를 인용하는 것이면 문장에 근거(CLAUDE.md·메모리·커밋)를 명시하라."
        )

    return False, ""


# ═════════════════════════════════════════════════════════════════════════════
# transcript 읽기 (부수효과 경계)
# ═════════════════════════════════════════════════════════════════════════════
def read_turn(transcript_path: str) -> tuple[str, list[dict], str]:
    """이번 턴의 (내 텍스트, 이벤트 시간순, 턴 지문). 마지막 **사람** 메시지 이후만."""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", [], ""
    entries = []
    for ln in lines:
        try:
            entries.append(json.loads(ln))
        except ValueError:
            continue
    if not entries:
        return "", [], ""

    last_human = 0
    for i, e in enumerate(entries):
        if is_human_turn(e):
            last_human = i
    turn = entries[last_human:]
    fp = hashlib.sha1(  # noqa: S324 — 보안 해시가 아니라 **턴 지문**(짧은 식별자)이다
        json.dumps(
            entries[last_human].get("message") or {}, ensure_ascii=False, sort_keys=True
        ).encode()
    ).hexdigest()[:12]

    my_text: list[str] = []
    events: list[dict] = []
    for e in turn:
        msg = e.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "text" and msg.get("role") == "assistant":
                my_text.append(str(c.get("text") or ""))
            elif t == "tool_use":
                events.append({
                    "kind": "tool_use",
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "text": json.dumps(c.get("input") or {}, ensure_ascii=False),
                })
            elif t == "tool_result":
                cc = c.get("content")
                events.append({
                    "kind": "tool_result",
                    "id": c.get("tool_use_id"),
                    "text": cc if isinstance(cc, str) else json.dumps(cc, ensure_ascii=False),
                })
    events = _merge_subagent_events(transcript_path, events)
    return "\n".join(my_text), events, fp


def _agent_events_from_file(path: str) -> list[dict]:
    """서브에이전트 transcript(JSONL) → read_turn 과 동일 shape. 실패 = 빈 목록."""
    out: list[dict] = []
    try:
        for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            content = (e.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    out.append({
                        "kind": "tool_use", "id": c.get("id"), "name": c.get("name"),
                        "text": json.dumps(c.get("input") or {}, ensure_ascii=False),
                    })
                elif c.get("type") == "tool_result":
                    cc = c.get("content")
                    out.append({
                        "kind": "tool_result", "id": c.get("tool_use_id"),
                        "text": cc if isinstance(cc, str)
                        else json.dumps(cc, ensure_ascii=False),
                    })
    except OSError:
        return []
    return out


def _merge_subagent_events(transcript_path: str, events: list[dict]) -> list[dict]:
    """위임한 서브에이전트의 이벤트를 **그 Task 결과 위치에** splice 한다.

    위치 보존이 핵심 안전장치 — 결과 텍스트에 agentId 가 등장하는 에이전트만 그 지점에
    삽입한다. 매칭 안 되는 transcript 는 무시(변경 **이전**에 돈 검증이 '이후'로 둔갑 방지).
    """
    try:
        files = subagent_transcript_files(transcript_path)
        if not files:
            return events
        by_id = {os.path.basename(f)[len("agent-"):-len(".jsonl")]: f for f in files}
        out: list[dict] = []
        for e in events:
            out.append(e)
            if e.get("kind") != "tool_result":
                continue
            text = e.get("text") or ""
            hit = next((aid for aid in by_id if aid and aid in text), None)
            if hit:
                out.extend(_agent_events_from_file(by_id.pop(hit)))
        return out
    except Exception:  # noqa: BLE001 — fail-open
        return events


def _runtime_dir(session_id: str) -> Path:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    d = Path(root) / ".claude" / "runtime" / (session_id or "nosession")
    d.mkdir(parents=True, exist_ok=True)
    return d


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0  # fail-open
    if not isinstance(payload, dict):
        return 0

    my_text, events, turn_fp = read_turn(str(payload.get("transcript_path") or ""))
    last_msg = payload.get("last_assistant_message")
    if isinstance(last_msg, str) and last_msg.strip():
        my_text = last_msg  # Stop 입력이 더 정확하면 그걸 쓴다
    if not my_text:
        return 0

    block, reason = evaluate(my_text, events)
    if not block:
        return 0

    # ★ 무한루프 방지 — `stop_hook_active` 만 보고 즉시 통과하면 실 상한이 1회가 된다.
    #   카운터가 상한에 **도달했을 때만** 통과시킨다. 턴 지문 단위라 새 지시 시 자동 리셋.
    counter = _runtime_dir(str(payload.get("session_id") or "")) / f"claim_blocks_{turn_fp}"
    try:
        n = int(counter.read_text().strip() or "0")
    except (OSError, ValueError):
        n = 0
    if n >= MAX_BLOCKS_PER_TURN:
        sys.stderr.write(f"[증거게이트 WARN·상한 도달] {reason}\n")
        return 0
    counter.write_text(str(n + 1))

    print(json.dumps(
        {"decision": "block", "reason": f"[증거 게이트] 끝낼 수 없다.\n\n{reason}"},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — fail-open
        raise SystemExit(0) from None
