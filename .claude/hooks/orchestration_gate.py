#!/usr/bin/env python3
"""Fable 오케스트레이션 게이트 (PreToolUse) — 토큰 효율 전용. 안전훅과 **완전 분리**.

목적: Fable 5(오케스트레이터)가 무거운 실행 노동을 직접 하지 않고 서브에이전트에 위임하도록
"권고(CLAUDE.md)"를 넘어 **물리적으로 유도**한다. 메인 에이전트가 한 턴에 직접 수정하는
코드 파일 수를 상한(5)으로 제한하고, 초과분은 위임하라는 지시를 돌려준다.

  - 상한 = 5 (응집된 멀티파일 기능을 매번 위임하면 오히려 문맥 손실이 크다).
  - 서브에이전트(payload agent_id/agent_type)는 전부 통과 = **위임이 실행 경로**다.
  - 스위치 OFF(`.claude/.orchestration-state` != "on")면 즉시 통과(no-op).
  - **fail-open**: 어떤 예외든 통과(exit 0). 게이트 버그가 세션을 막지 않는다.
  - 이 훅은 `direction_guard`(방향)·`claim_evidence_guard`(증거)와 **별개**다 —
    그 둘은 스위치와 무관하게 늘 작동한다. 본 게이트는 한도 효율일 뿐 안전 기능이 아니다.

토글: `bash scripts/fable.sh on|off|status`
"""
import json
import os
import re
import sys
import time

LIMIT = 5  # 메인이 한 턴에 직접 수정 가능한 코드 파일 수

CODE_EXTS = {
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt",
    "swift", "c", "h", "cc", "cpp", "hpp", "cs", "rb", "php", "vue", "svelte",
    "css", "scss", "sass", "less", "html", "sh", "bash", "zsh", "sql", "lua",
    "dart", "scala", "ex", "exs", "zig", "ipynb",
}
_EXT_RE = "|".join(sorted(CODE_EXTS))
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def _project_dir():
    d = os.environ.get("CLAUDE_PROJECT_DIR")
    if d:
        return d
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def is_code_path(path):
    return "." in path and path.rsplit(".", 1)[-1].lower() in CODE_EXTS


def bash_writes_code(cmd):
    """Bash 로 코드 파일을 직접 수정하는 우회(sed/perl -i · 리다이렉트 · tee)인가."""
    writes_inplace = re.search(r"\b(sed|perl)\s+[^|;&]*-\w*i", cmd or "")
    touches_code = re.search(rf"\.(?:{_EXT_RE})\b", cmd or "")
    writes_redirect = re.search(
        rf"(?:>>?|\btee\b(?:\s+-\w+)*)\s*['\"]?[^\s'\"|;&<>]+\.(?:{_EXT_RE})\b", cmd or "")
    return bool(writes_redirect or (writes_inplace and touches_code))


class Decision:
    def __init__(self, allow, message="", gate=None):
        self.allow = allow
        self.message = message
        self.gate = gate  # 갱신된 턴 카운터(있으면 저장)


def decide(payload, state, gate, *, limit=LIMIT):
    """순수 판정 — IO 없음(테스트 가능). state='on' 일 때만 게이트 작동."""
    if state != "on":
        return Decision(True)
    # 서브에이전트 호출은 게이트 대상 아님 — 위임이 실행 경로다
    if payload.get("agent_id") or payload.get("agent_type"):
        return Decision(True)

    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input") or {}

    if tool == "Bash":
        if bash_writes_code(tin.get("command", "")):
            return Decision(False, (
                "[fable 게이트] 메인은 Bash 로 코드 파일을 수정할 수 없습니다. "
                f"Edit/Write(턴당 {limit}개) 또는 kream-executor(Opus)에 위임하세요."
            ))
        return Decision(True)

    if tool not in _EDIT_TOOLS:
        return Decision(True)

    path = tin.get("file_path") or tin.get("notebook_path") or ""
    if not path or not is_code_path(path):
        return Decision(True)  # 설정·문서 등 비코드는 직접 수정 허용

    prompt = payload.get("prompt_id", "")
    files = list(gate.get("files", [])) if gate.get("prompt_id") == prompt else []
    if path in files:
        return Decision(True, gate={"prompt_id": prompt, "files": files})  # 재수정은 미카운트
    if len(files) < limit:
        files.append(path)
        return Decision(True, gate={"prompt_id": prompt, "files": files})
    joined = ", ".join(files)
    msg = (
        f"[fable 게이트] 이번 턴 메인 직접수정 코드파일 {limit}개 도달({joined}). "
        f"추가 수정({path})은 kream-executor(Opus)에 브리프와 함께 위임하세요. "
        "조회·탐색은 kream-explorer(Haiku). 브리프 골격 = "
        ".claude/orchestration/brief-templates.md"
    )
    return Decision(False, msg, gate={"prompt_id": prompt, "files": files})


def _read_state(proj):
    try:
        with open(os.path.join(proj, ".claude", ".orchestration-state")) as f:
            return f.read().strip()
    except OSError:
        return "off"


def _load_gate(gate_dir, session):
    try:
        with open(os.path.join(gate_dir, session + ".json")) as f:
            saved = json.load(f)
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_gate(gate_dir, session, gate):
    try:
        os.makedirs(gate_dir, exist_ok=True)
        with open(os.path.join(gate_dir, session + ".json"), "w") as f:
            json.dump(gate, f)
        now = time.time()  # 오래된 세션 카운터 정리(24h)
        for fn in os.listdir(gate_dir):
            p = os.path.join(gate_dir, fn)
            if now - os.path.getmtime(p) > 86400:
                os.remove(p)
    except OSError:
        pass


def main():
    proj = _project_dir()
    state = _read_state(proj)
    if state != "on":
        sys.exit(0)
    payload = json.load(sys.stdin)
    gate_dir = os.path.join(proj, ".claude", ".orchestration", "state")
    session = re.sub(r"[^a-zA-Z0-9-]", "", payload.get("session_id", "nosession"))
    gate = _load_gate(gate_dir, session)
    d = decide(payload, state, gate)
    if d.gate is not None:
        _save_gate(gate_dir, session, d.gate)
    if d.allow:
        sys.exit(0)
    sys.stderr.write(d.message)
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        sys.exit(0)  # fail-open — 게이트 버그가 세션을 막지 않는다
