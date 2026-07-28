#!/usr/bin/env python3
"""의도 인터뷰 게이트 — 애매한 신규 지시에 "착수 전 인터뷰"를 기계로 강제한다.

## 왜 (사장 2026-07-28: "나는 비개발자라 나에게 인터뷰가 필요해 확실히")

사장 지시가 열려 있을 때("~~하려면?", "자동화 해줘") 모델은 **자기 해석으로 그냥 달린다**.
의도가 어긋난 채 완주하면 배달 중 원격으로는 되돌리기 비싸다 — 역방향/푸시 혼선으로
시간을 크게 잃은 게 그 사례. md 규칙·스킬은 실측상 발동 0회(다른 프로젝트에서 장기간
설치돼 있었지만 한 번도 안 불림) → **훅만이 작동한다**.

## 무엇을 하나 (2조각)
1. **prompt 모드** (UserPromptSubmit): 신규·열린 작업 냄새가 나는 프롬프트를 판정해
   ①"의도 요약 + 객관식 질문 최대 3개 먼저" 지침을 그 자리에서 주입하고
   ②세션 상태 파일에 "의도 미확정"을 마크한다.
   다음 사장 프롬프트(= 인터뷰 답변)가 오면 상태를 해제한다 — "그냥 해"도 답변이다.
2. **pretool 모드** (PreToolUse): 의도 미확정 상태에서 메인 에이전트의 **코드 파일
   수정**(Edit/Write/NotebookEdit + Bash 우회)을 물리 차단한다. 탐색(Read/Grep)과
   문서 수정은 허용 — 좋은 질문을 만들기 위한 조사는 오히려 필요하다.

## 판정 원칙 (오탐 = 배달 중 왕복 1회 낭비 → 보수적으로)
- 발동: 행동 동사(구현/만들/자동화/도입…) 포함 + 면제 신호 없음 + 슬래시 명령 아님.
- 면제: 이어서/조사/확인/의논/왜/상태 등 **연속 작업·질문·조회** + 구체 파일 경로 지목
  (경로까지 짚은 지시는 대체로 스코프가 잡혀 있다) + 장문 스펙(이미 상세).
- fail-open: 어떤 예외든 통과. 상태 파일은 24h 지나면 무시(죽은 세션 잔재).

판정 로직은 tests/test_hooks_guards.py 로 고정한다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestration_gate import bash_writes_code, is_code_path  # noqa: E402

STATE_TTL = 86400  # 죽은 세션의 잔재 상태는 하루 지나면 무시
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# 행동 동사 — 새로 만들거나 바꾸라는 지시 신호
_ACTION = re.compile(
    r"(구현|만들|추가|붙여|넣어|바꿔|바꾸|개선|리팩터|자동화|제작|짜\s?줘"
    r"|설치|도입|적용|전환|교체|재설계|설계|확장|해결해|수정해)"
)
# 면제 — 연속 작업·조회·질문·의논은 인터뷰 대상이 아니다
_EXEMPT = re.compile(
    r"(이어서|계속|마저|커밋|푸시|테스트|검증|확인|조사|분석|알려|뭐야|뭐가|어때"
    r"|어떻게\s?생각|왜\s|왜야|읽어|보여|요약|정리해|설명|상태|헬스|의논|얘기|생각은"
    r"|질문|리뷰|추적|비교)"
)
# 구체 경로/식별자를 짚은 지시는 스코프가 잡힌 것으로 본다
_SPECIFIC = re.compile(r"\.(py|md|json|sh|js|ts|yaml|toml)\b|src/|scripts/|tests/|\.claude/")

# 버그/오작동 신호 → 프로세스 스킬 의무 호출 (다른 프로젝트 실측: 스킬이 깔려 있어도
# "알아서 판다"로 건너뛰다 사고 — 크림봇에선 훅이 매번 지시한다)
_DEBUG = re.compile(
    r"(버그|에러|오류|실패|깨졌|죽었|안\s?잡|안\s?나오|안\s?나와|안\s?오|안\s?와"
    r"|안\s?되|안\s?돼|안\s?됨|왜\s?안)"
)

INTERVIEW_DIRECTIVE = """═══ 🎤 [의도 인터뷰 게이트] 신규·열린 작업 판정 — 코드 착수 금지 ═══
이 프롬프트는 의도 해석의 여지가 있다. **코드 수정 전에 반드시**:
  1. 이해한 의도를 **요약 3줄**(목표 / 범위 / 안 하는 것)로 제시하라.
  2. 결과가 갈리는 지점만 **객관식 질문 최대 3개**(AskUserQuestion, 폰에서 탭 가능)로 물어라.
  3. 사장 답변이 올 때까지 코드 Edit/Write 는 훅이 물리 차단한다(탐색·조사는 허용).
  4. 답변 수신 후에는 **무질문 완주** — 중간에 다시 묻지 않는다.
명백히 단순하면 질문을 1개로 줄여라. 질문을 지어내려고 억지 갈림길을 만들지 마라.
═══"""

CLEARED_NOTE = "✅ [의도 게이트] 인터뷰 답변 수신 — 의도 고정. 이제 무질문 완주하라."

DEBUG_NOTE = (
    "🐞 [프로세스 스킬 의무] 버그/오작동 신호 감지 — 맨손으로 파지 마라. "
    "**superpowers:systematic-debugging 스킬을 먼저 호출**해 절차(재현→가설→검증)를 따르라. "
    "파이프라인 추적('왜 안 잡혀/알림 안 와')이면 scan-debugger 에이전트가 우선이다."
)

BLOCK_MESSAGE = (
    "[의도 게이트] 의도 인터뷰 미완료 — 코드 수정 차단. "
    "먼저 ①이해한 의도 요약 3줄 ②갈림길 객관식 질문(최대 3개)을 사장에게 제시하고 "
    "답변을 기다려라. 사장의 다음 프롬프트가 오면 자동 해제된다. 우회하지 마라."
)


def should_flag(prompt: str) -> bool:
    """순수 판정 — 이 프롬프트가 '착수 전 인터뷰' 대상인가."""
    p = (prompt or "").strip()
    if not p or p.startswith("/") or p.startswith("!"):
        return False
    if len(p) > 700:  # 장문 = 이미 상세 스펙
        return False
    if _EXEMPT.search(p) or _SPECIFIC.search(p):
        return False
    return bool(_ACTION.search(p))


def needs_debug_skill(prompt: str) -> bool:
    """순수 판정 — 버그/오작동 신호라 systematic-debugging 의무 지시를 붙일 것인가."""
    p = (prompt or "").strip()
    if not p or p.startswith("/"):
        return False
    return bool(_DEBUG.search(p))


class Decision:
    def __init__(self, allow: bool, message: str = ""):
        self.allow = allow
        self.message = message


def decide_pretool(payload: dict, pending: bool) -> Decision:
    """순수 판정 — 의도 미확정 상태에서 이 도구 호출을 막을 것인가."""
    if not pending:
        return Decision(True)
    if payload.get("agent_id") or payload.get("agent_type"):
        return Decision(True)  # 서브에이전트는 대상 아님(메인의 착수만 막는다)
    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input") or {}
    if tool == "Bash":
        try:
            writes = bash_writes_code(tin.get("command", ""))
        except Exception:  # noqa: BLE001 — 판정 불가면 통과(fail-open)
            return Decision(True)
        return Decision(False, BLOCK_MESSAGE) if writes else Decision(True)
    if tool in _EDIT_TOOLS:
        path = tin.get("file_path") or tin.get("notebook_path") or ""
        if path and is_code_path(path):
            return Decision(False, BLOCK_MESSAGE)
    return Decision(True)


# ── IO ───────────────────────────────────────────────────────────────────────
def _root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _state_file(session: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9-]", "", session or "nosession")
    return _root() / ".claude" / "runtime" / safe / "intent.json"


def _pending(session: str) -> bool:
    f = _state_file(session)
    try:
        if not f.exists():
            return False
        if time.time() - f.stat().st_mtime > STATE_TTL:
            f.unlink(missing_ok=True)
            return False
        return True
    except OSError:
        return False


def _run_prompt_mode() -> None:
    payload = json.load(sys.stdin)
    session = str(payload.get("session_id") or "")
    prompt = str(payload.get("prompt") or "")
    f = _state_file(session)
    if _pending(session):
        # 사장의 다음 프롬프트 = 인터뷰 답변("그냥 해" 포함) → 해제
        f.unlink(missing_ok=True)
        print(CLEARED_NOTE)
    elif should_flag(prompt):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps({"prompt": prompt[:200], "ts": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(INTERVIEW_DIRECTIVE)
    if needs_debug_skill(prompt):
        print(DEBUG_NOTE)


def _run_pretool_mode() -> None:
    payload = json.load(sys.stdin)
    session = str(payload.get("session_id") or "")
    d = decide_pretool(payload, _pending(session))
    if not d.allow:
        sys.stderr.write(d.message)
        sys.exit(2)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"
    if mode == "pretool":
        _run_pretool_mode()
    else:
        _run_prompt_mode()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        sys.exit(0)  # fail-open — 게이트 버그가 세션을 막지 않는다
