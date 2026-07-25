#!/usr/bin/env python3
"""PreToolUse 훅 — **실계정 크림 호출을 GO 없이 못 내게 막는다**.

## 왜 (2026-07-25 실사고)
레거시 `scripts/bootstrap_cold_volumes.py` 실행 차단 가드의 판별력을 변이 검증으로
확인하려고 **가드만 끄고** 테스트를 돌렸다가, 그 테스트가 실 DB 를 읽어 레거시
배치가 그대로 돌았다 — **실계정 크림 호출 311건**. 그 전에도 같은 계열 사고가 있다
(`scripts/manual_auto_scan_live.py` 주석: "과거 tests/ 하위에 있어 pytest 자동
수집으로 실운영 크림 예산 오염").

`tests/conftest.py` 전역 안전망이 **테스트 안**을 막는다면, 이 훅은 **Bash 실행
자체**를 막는다. 안전망을 끄는 환경변수를 세우는 명령도 여기서 다시 막힌다
(이중 방어).

## 무엇을 막나 (전부 "크림을 실제로 때리는" 것만)
1. 배치 스크립트를 `--dry-run` 없이 실행 — `bootstrap_cold_volumes_light.py`,
   `drain_collect_queue.py`
2. 레거시 우회로 — `bootstrap_cold_volumes.py`, `KREAM_LEGACY_BOOTSTRAP_GO=1`
3. 크림 직격 스크립트 — `probe_kream*`, `manual_auto_scan_live.py`,
   `push_dump_full.py`, `pilot_musinsa_push.py`, `test_auto_scan_categories.py`
4. 테스트 안전망 해제 — `KREAM_TEST_ALLOW_NETWORK=1` / `KREAM_TEST_ALLOW_REAL_DB=1`

## 안 막는 것 (일부러 — 오탐이 나면 사람이 훅을 꺼버린다)
소싱처 전용 probe(`probe_nike*`·`probe_adidas*`·`probe_abcmart*`·`probe_kasina`·
`probe_naver_asics`·`test_sources_live.py`·`test_29cm.py`) — 크림 캡과 무관한
읽기전용이다. `--dry-run` 붙은 배치. 일반 `pytest`. 파일을 읽기만 하는 명령
(`cat`/`grep`/`python3 -c "import ast..."`).

## 해제 (사장 GO)
`KREAM_LIVE_BATCH_GO=1` 를 세우고 실행한다. 2-4 카나리처럼 **의도한 실호출**은
이걸로 통과시킨다 — 차단은 자물쇠가 아니라 "지금 실계정을 때린다"는 속도방지턱.

fail-open: 어떤 예외든 통과(exit 0). 가드 버그가 세션을 막지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_util import ShellParseError, segment_head, shell_segments  # noqa: E402

LIVE_GO = "KREAM_LIVE_BATCH_GO"

# 1. 배치 — dry-run 이면 통과
_BATCH_SCRIPTS = (
    "scripts/bootstrap_cold_volumes_light.py",
    "scripts/drain_collect_queue.py",
)

# 2·3. 항상 차단 (레거시 우회로 + 크림 직격)
_ALWAYS_BLOCKED = (
    "scripts/bootstrap_cold_volumes.py",
    "scripts/manual_auto_scan_live.py",
    "scripts/push_dump_full.py",
    "scripts/pilot_musinsa_push.py",
    "scripts/test_auto_scan_categories.py",
)
_KREAM_PROBE_RE = re.compile(r"scripts/probe_kream[\w.]*\.py")

# 4. 안전망/가드 해제 환경변수 — 이걸 세우는 명령 자체를 GO 없이는 막는다
_BYPASS_ENVS = (
    "KREAM_LEGACY_BOOTSTRAP_GO",
    "KREAM_TEST_ALLOW_NETWORK",
    "KREAM_TEST_ALLOW_REAL_DB",
)


class Decision:
    def __init__(self, allow: bool, message: str = ""):
        self.allow = allow
        self.message = message


def _is_dry_run(seg: str) -> bool:
    return "--dry-run" in seg.split()


_PY_HEADS = re.compile(r"^(python[\d.]*|uv|uvx|poetry|pipenv)$")
# 실행을 감싸기만 하는 래퍼 — 벗겨내고 다시 판정한다.
# (오케스트레이터 자체 우회 실험 2026-07-25: `nohup python scripts/x.py &` 통과했다)
_WRAPPER_HEADS = frozenset(
    {"nohup", "env", "time", "nice", "ionice", "stdbuf", "setsid", "xargs", "timeout"}
)
# 문자열로 받은 명령을 다시 셸에 태우는 것 — 그 문자열을 재귀 판정한다.
# (`bash -c "python scripts/bootstrap_cold_volumes.py"` 가 통과했다)
_SHELL_HEADS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_MAX_UNWRAP = 4


def _strip_wrappers(toks: list[str]) -> list[str]:
    """`nohup`/`env FOO=1`/`timeout 30` 같은 래퍼를 벗겨 실제 실행 토큰만 남긴다."""
    for _ in range(_MAX_UNWRAP):
        if not toks:
            return toks
        head = os.path.basename(toks[0])
        if head not in _WRAPPER_HEADS:
            return toks
        rest = toks[1:]
        # 래퍼 옵션/인자(-n, 30, FOO=1, -I{})는 건너뛴다
        while rest and (
            rest[0].startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*|[\d.]+", rest[0])
        ):
            rest = rest[1:]
        toks = rest
    return toks


def _shell_string_payloads(toks: list[str]) -> list[str]:
    """`bash -c "<명령>"` 의 <명령> (재귀 판정 대상).

    ⚠️ `shell_segments` 는 shlex(posix) 결과를 공백으로 다시 이어붙이므로 이 시점엔
    따옴표가 이미 사라져 있다 — `-c` 뒤 **나머지 전체**를 payload 로 본다.
    (그러지 않으면 `bash -c "python scripts/x.py"` 가 'python' 한 토큰만 보고 통과한다.)
    """
    if not toks or os.path.basename(toks[0]) not in _SHELL_HEADS:
        return []
    for i, tok in enumerate(toks):
        if tok == "-c" and i + 1 < len(toks):
            return [" ".join(toks[i + 1 :])]
    return []


def _executes(seg: str, script: str, _depth: int = 0) -> bool:
    """세그먼트가 그 스크립트를 **실행**하는가 (언급만 하는 건 아니고).

    `grep -n scripts/bootstrap_cold_volumes.py CLAUDE.md` 처럼 인자로 이름만
    등장하는 읽기 명령은 막지 않는다 — 오탐이 나면 사람이 훅을 꺼버린다.
    래퍼(`nohup`·`env`·`timeout`)는 벗기고, `bash -c "..."` 는 안쪽을 재귀 판정한다.
    """
    toks = _strip_wrappers(seg.split())
    if not toks:
        return False

    if _depth < _MAX_UNWRAP:
        for payload in _shell_string_payloads(toks):
            try:
                inner_segments = shell_segments(payload)
            except ShellParseError:
                inner_segments = [payload]
            if any(_executes(inner, script, _depth + 1) for inner in inner_segments):
                return True

    head = segment_head(" ".join(toks))
    if not head:
        return False
    basename = script.split("/")[-1]
    if head.endswith(script) or head.endswith("/" + basename) or head == basename:
        return True  # 직접 실행 (./scripts/x.py, cd scripts && ./x.py)
    if _PY_HEADS.match(os.path.basename(head)):
        return any(t.endswith(script) or t.endswith(basename) for t in toks[1:])
    return False


def _bypass_env_set(seg: str) -> str | None:
    """`ENV=1` 형태로 안전망 해제 변수를 세우는지 (값이 1 일 때만)."""
    for token in seg.split():
        for env in _BYPASS_ENVS:
            if token == f"{env}=1":
                return env
    return None


def evaluate(cmd: str, go_enabled: bool) -> Decision:
    """Bash 명령 판정. GO 가 켜져 있으면 전부 통과."""
    if go_enabled:
        return Decision(True)

    try:
        segments = shell_segments(cmd or "")
    except ShellParseError:
        # 파싱 실패는 "무해"가 아니다 — 그러나 여기서 fail-closed 하면 일반 명령까지
        # 막혀 오탐이 된다. 원문 전체를 한 세그먼트로 보고 판정만 이어간다.
        segments = [cmd or ""]

    for seg in segments:
        env = _bypass_env_set(seg)
        if env:
            return Decision(
                False,
                f"안전망 해제 환경변수({env}=1)를 세우는 실행이다.\n"
                "  이 변수들은 테스트/스크립트가 실 네트워크·운영 DB 로 나가게 연다 —\n"
                "  2026-07-25 실계정 311콜 사고가 정확히 이 경로였다.",
            )

        probe = _KREAM_PROBE_RE.search(seg)
        if probe and _executes(seg, probe.group(0)):
            return Decision(False, "크림 API 를 직접 때리는 probe 스크립트다.")

        for script in _ALWAYS_BLOCKED:
            if _executes(seg, script):
                return Decision(
                    False, f"실계정 크림/소싱처 호출을 내는 스크립트다: {script}"
                )

        for script in _BATCH_SCRIPTS:
            if _executes(seg, script) and not _is_dry_run(seg):
                return Decision(
                    False,
                    f"백그라운드 배치를 라이브로 돌리는 실행이다: {script}\n"
                    "  → 먼저 `--dry-run` 으로 대상/예산/ETA 를 확인하고 사장에게 보고할 것.",
                )

    return Decision(True)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail-open

    if payload.get("tool_name") != "Bash":
        return 0

    cmd = (payload.get("tool_input") or {}).get("command") or ""
    decision = evaluate(cmd, os.getenv(LIVE_GO) == "1")
    if decision.allow:
        return 0

    print(
        "🛑 [실호출 가드] 크림 실계정을 때리는 실행이라 막았다.\n\n"
        f"{decision.message}\n\n"
        "  사장 GO 로 진행하려면 이 실행에만 명시적으로 켜라:\n"
        f"    {LIVE_GO}=1 <명령>\n"
        "  (2-4 카나리처럼 의도한 실호출은 이 방법으로 통과시킨다.)\n"
        "  ⚠️ 우회하지 말고, 사장에게 dry-run 수치와 함께 보고한 뒤 GO 를 받아라.",
        file=sys.stderr,
    )
    return 2  # PreToolUse 차단


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # pragma: no cover — 훅 버그가 세션을 막지 않는다
        sys.exit(0)
