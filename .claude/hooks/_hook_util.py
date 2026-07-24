#!/usr/bin/env python3
"""크림봇 훅 공유 유틸 — stdlib only, 시스템 python3 에서 돈다.

## 왜 훅인가 (앤트로픽 공식 진단)
> "'절대 하지 마라' 는 **지시가 아니라 훅+권한**으로 하라 — 긴 세션에서, 압박받을 때,
>  모호한 상황에서 모델은 프롬프트된 규칙을 못 지킨다."

CLAUDE.md 의 🚫 금지 리스트·INVARIANT 는 **읽어야** 작동한다. 안 읽으면 없는 것과 같다.
훅은 읽기로 **선택하지 않아도** 작동한다.

여기 모인 것: shell 파일쓰기 판정(증거게이트가 '변경 있었나'를 판단할 때 공유) ·
서브에이전트 transcript 수집 · 프로젝트 루트/메모리 경로 해석.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|"})
# tee/install 은 head 로 등장하면 파일을 쓴다
_BASH_WRITE_HEADS = frozenset({"tee", "install"})


class ShellParseError(ValueError):
    """shlex 가 명령을 못 삼켰다 — 세그먼트 분리 실패.

    **분리 실패는 "무해"가 아니라 "위험"으로 간주**한다(fail-closed). 호출부가 조용히
    "쓰기 아님"으로 넘기면 그게 새 우회다.
    """


def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def memory_dir(root: Path | None = None) -> Path:
    """이 프로젝트의 자동 메모리 디렉터리.

    Claude Code 는 프로젝트 경로의 비영숫자를 `-` 로 치환해 디렉터리명을 만든다
    (`/mnt/c/Users/USER/Desktop/크림봇` → `-mnt-c-Users-USER-Desktop----`).
    하드코딩하지 않고 같은 규칙으로 계산한다 — 경로가 바뀌어도 따라간다.
    """
    r = root or project_root()
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(r))
    return Path.home() / ".claude" / "projects" / slug / "memory"


def _tokenize_segments(cmd: str) -> list[list[str]]:
    """shlex(POSIX)로 cmd 를 `&&`·`||`·`;`·`|` 경계에서 나눈 segment 토큰 리스트.

    `punctuation_chars=True` 가 다중문자 연산자와 공백 없이 붙은 redirect(`x>file`)를
    별도 토큰으로 갈라준다. 수제 whitespace-split 이 놓치던 지점.
    """
    lexer = shlex.shlex(cmd or "", posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    toks: list[str] = []
    try:
        while True:
            t = lexer.get_token()
            if t is None:
                break
            toks.append(t)
    except ValueError as exc:
        raise ShellParseError(f"shlex 파싱 실패: {exc}") from exc

    segs: list[list[str]] = []
    buf: list[str] = []
    for t in toks:
        if t in _SHELL_SEPARATORS:
            if buf:
                segs.append(buf)
            buf = []
            continue
        buf.append(t)
    if buf:
        segs.append(buf)
    return segs


def shell_segments(cmd: str) -> list[str]:
    return [" ".join(toks) for toks in _tokenize_segments(cmd)]


def segment_head(seg: str) -> str:
    """segment 의 첫 명령 토큰(선행 `ENV=val` prefix 제거)."""
    toks = seg.split()
    i = 0
    while i < len(toks) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", toks[i]):
        i += 1
    return toks[i] if i < len(toks) else ""


def bash_writes_a_file(cmd: str) -> bool:
    """Bash 명령이 **파일을 쓰는가** (`sed -i`·`cp`·`mv`·`tee`·`install`·redirect).

    head 기반이라 `grep -n cp file` 처럼 다른 명령의 **인자**로 등장하는 'cp' 는 안 걸린다.
    redirect 는 명령 뒤에 오므로 segment 내 전 토큰을 본다(`>&` fd 복제는 제외).

    파싱 실패 → True(fail-closed).
    """
    try:
        segs = shell_segments(cmd or "")
    except ShellParseError:
        return True
    for seg in segs:
        toks = seg.split()
        head = segment_head(seg)
        if head == "sed" and any(t.startswith("-i") for t in toks[1:]):
            return True
        if head in ("cp", "mv") and len(toks) >= 2:
            return True
        if head in _BASH_WRITE_HEADS:
            return True
        for tok in toks:
            core = tok.lstrip("0123456789")
            if core.startswith(">") and not core.startswith(">&"):
                return True
    return False


def subagent_transcript_files(transcript_path: str) -> list[str]:
    """메인 `<sid>.jsonl` 옆의 `<sid>/subagents/agent-*.jsonl`.

    서브에이전트가 Read·검증한 기록은 메인 transcript 에 없다 — 합산하지 않으면
    "직원이 검증했는데 증거 없음"으로 오탐한다. 실패는 빈 목록(fail-open — 합산은
    증거를 **추가**하는 방향이라 가드를 약화시키지 않는다).
    """
    try:
        base, ext = os.path.splitext(transcript_path or "")
        if ext != ".jsonl":
            return []
        d = os.path.join(base, "subagents")
        if not os.path.isdir(d):
            return []
        return sorted(
            os.path.join(d, f)
            for f in os.listdir(d)
            if f.startswith("agent-") and f.endswith(".jsonl")
        )[:50]
    except OSError:
        return []
