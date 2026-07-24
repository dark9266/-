#!/usr/bin/env python3
"""PreToolUse 훅 — **방향 이탈 물리 차단**.

## 왜 (사장 2026-07-24: "방향 새지 않길 바래, 훅으로 미리 막아줘")
CLAUDE.md 🚫 금지 리스트는 **읽어야** 작동한다. 긴 세션에서 압박받으면 안 읽는다.
과거 실사고: 역방향 피벗으로 시간 대량 손실(`feedback_no_workarounds`), 폐기 흐름
스캐너 리팩터 제안 반복. **마크다운이 못 막은 것을 기계가 막는다.**

## 무엇을 막나 (2건 — 근거 있는 것만. 넓히면 오탐으로 훅이 꺼진다)
1. **폐기 흐름 스캐너 수정** — `reverse_scanner`·`scanner`·`tier1_scanner`·`continuous_scanner`.
   v2 시절 역방향/Tier 구조로 현행 푸시 트랙과 무관. 리팩터·버그픽스·재활용 전부 금지.
   ⚠️ `tier2_monitor.py` 는 **축 ② 보조 감시로 유지 판정 완료** — 여기 없다(막지 않는다).
2. **신규 소싱처 어댑터/크롤러 생성** — 22곳 안정화 전까지 금지. 단계를 뒤집는 대표 경로.

## 안 막는 것 (일부러)
알림 하드플로어·거래량 게이트·타겟 축소 같은 **값** 변경은 정규식으로 판별하면 오탐이
크다 — Codex S등급 적대검증(`kream-codex-collab`)과 `profit-analyzer` 가 담당한다.

## 해제 (사장이 의도적으로 할 때)
`KREAM_LEGACY_EDIT_GO=1` / `KREAM_NEW_SOURCE_GO=1` 를 켜고 실행한다.
차단은 **자물쇠가 아니라 속도방지턱**이다 — "지금 방향에서 벗어나고 있다"를 알리는 게 목적.

fail-open: 어떤 예외든 통과(exit 0). 가드 버그가 세션을 막지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_util import bash_writes_a_file, project_root  # noqa: E402

# ── 1. 폐기 흐름 (CLAUDE.md "🗑 폐기 흐름 스캐너 — 참조용, 손대지 말 것") ──────────
_LEGACY_FILES = (
    "src/reverse_scanner.py",
    "src/scanner.py",
    "src/tier1_scanner.py",
    "src/continuous_scanner.py",
)
_LEGACY_GO = "KREAM_LEGACY_EDIT_GO"

# ── 2. 신규 소싱처 (CLAUDE.md "소싱처 신규 추가 (안정화 전까지)") ────────────────
_SOURCE_DIRS = ("src/adapters/", "src/crawlers/")
_NEW_SOURCE_GO = "KREAM_NEW_SOURCE_GO"
# 소싱처 파일이 아닌 인프라 파일 — 신규 생성 허용
_SOURCE_INFRA = frozenset({
    "src/crawlers/registry.py", "src/crawlers/kream.py", "src/crawlers/__init__.py",
    "src/adapters/__init__.py", "src/adapters/base.py",
})

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


class Decision:
    def __init__(self, allow: bool, message: str = ""):
        self.allow = allow
        self.message = message


def _rel(path: str) -> str:
    """절대경로를 프로젝트 상대경로로 정규화(윈도우 역슬래시 포함)."""
    p = (path or "").replace("\\", "/")
    root = str(project_root()).replace("\\", "/").rstrip("/")
    if root and p.startswith(root + "/"):
        p = p[len(root) + 1:]
    return p.lstrip("./")


def is_legacy(path: str) -> bool:
    return _rel(path) in _LEGACY_FILES


def is_new_source_file(path: str, *, exists: bool) -> bool:
    """아직 없는 소싱처 어댑터/크롤러 파일을 새로 만들려는가."""
    rel = _rel(path)
    if exists or rel in _SOURCE_INFRA:
        return False
    if not rel.startswith(_SOURCE_DIRS) or not rel.endswith(".py"):
        return False
    return not rel.rsplit("/", 1)[-1].startswith(("test_", "_"))


def decide(payload: dict, *, exists, env: dict) -> Decision:
    """순수 판정 — IO 없음(`exists` 는 주입). 서브에이전트도 동일 적용(위임 우회 방지)."""
    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input") or {}

    paths: list[str] = []
    if tool in _EDIT_TOOLS:
        paths = [tin.get("file_path") or tin.get("notebook_path") or ""]
    elif tool == "Bash":
        cmd = tin.get("command", "")
        if bash_writes_a_file(cmd):
            # 명령문에 등장하는 레포 상대경로만 후보로 본다
            paths = re.findall(r"[\w./-]*src/[\w./-]+\.py", cmd)
    paths = [p for p in paths if p]

    for p in paths:
        if is_legacy(p) and env.get(_LEGACY_GO) != "1":
            return Decision(False, (
                f"[방향 게이트] `{_rel(p)}` 는 **폐기 흐름**이다(v2 역방향/Tier 구조).\n"
                "현행은 푸시 단일 트랙 — 이 파일의 리팩터·버그픽스·재활용은\n"
                "CLAUDE.md 금지 리스트다. 고치려던 게 현행 트랙 문제라면\n"
                "`src/core/*`·`src/adapters/*`·`src/tier2_monitor.py` 쪽을 보라.\n"
                f"정말 의도한 것이면 `{_LEGACY_GO}=1` 을 켜고 다시 실행."
            ))
        if is_new_source_file(p, exists=exists(p)) and env.get(_NEW_SOURCE_GO) != "1":
            return Decision(False, (
                f"[방향 게이트] `{_rel(p)}` = **신규 소싱처 파일 생성**으로 보인다.\n"
                "현재 단계는 '22 소싱처 안정화'다 — 소싱처 확장은 안정화 확정 **후**다"
                "(CLAUDE.md 단계 1→2, 순서 뒤집기 금지).\n"
                "기존 22곳 중 막힌 곳을 뚫는 거라면 `kream-search-in-search` 스킬을 먼저 불러라.\n"
                f"정말 추가하는 것이면 `{_NEW_SOURCE_GO}=1` 을 켜고 `crawler-builder` 에이전트로."
            ))
    return Decision(True)


def main() -> int:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return 0
    root = project_root()

    def exists(p: str) -> bool:
        rel = _rel(p)
        return (root / rel).exists()

    d = decide(payload, exists=exists, env=dict(os.environ))
    if d.allow:
        return 0
    sys.stderr.write(d.message)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — fail-open, 가드 버그가 세션을 막지 않는다
        raise SystemExit(0) from None
