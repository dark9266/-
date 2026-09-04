#!/usr/bin/env python3
"""SessionStart 훅 — 세션 시작 digest 주입 (read-only · fail-safe).

## 왜 (사장 2026-07-24: "했던 일 잊지 않기")
세션이 새로 뜨면 **직전까지의 상태가 통째로 없다**. CLAUDE.md 는 규칙을 주지만 *지금 어디까지
왔는지*는 안 준다 — 그래서 끝난 일을 또 하거나 방향을 다시 묻는다. 훅이 매 세션 시작에
"지금 상태"를 강제로 눈앞에 놓는다.

stdout 이 세션 추가 컨텍스트로 들어간다. 어떤 경우에도 세션을 차단하지 않는다(항상 exit 0).
빠르게 끝나는 것만 한다(ps·git·파일 읽기) — DB 조회는 하지 않고 **명령만** 제시한다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_util import project_root  # noqa: E402


def _run(args: list[str], cwd: Path | None = None) -> str:
    try:
        return subprocess.run(  # noqa: S603 — 훅이 조립한 고정 argv
            args, cwd=cwd, capture_output=True, text=True, timeout=8
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _bot_alive() -> str:
    out = _run(["ps", "-eo", "pid,etime,args"])
    for ln in out.splitlines():
        if "python" in ln and "main.py" in ln and "grep" not in ln:
            parts = ln.split(None, 2)
            if len(parts) >= 2:
                return f"살아있음 (PID {parts[0]}, {parts[1]} 경과)"
    return "**없음** — 헬스체크 1 FAIL. 복구 먼저(사장 컨펌 후 기동)"


def _fable_state(root: Path) -> str:
    try:
        state = (root / ".claude" / ".orchestration-state").read_text().strip()
    except OSError:
        state = "off"
    if state != "on":
        return "OFF"
    return "ON (머리=Fable5 / 구현=kream-executor / 탐색=kream-explorer)"


def _codex_pending(root: Path) -> str:
    import json

    q = root / "reports" / "codex" / "backlog.json"
    try:
        items = json.loads(q.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "0건"
    if not isinstance(items, list):
        return "0건"
    n = sum(1 for x in items if isinstance(x, dict) and x.get("status") == "PENDING")
    return f"**{n}건**" if n else "0건"


def _done_registry(root: Path, limit: int = 6) -> list[str]:
    """DONE 원장의 Active Lock 항목 — 재실행 금지 목록."""
    f = root / "docs" / "DONE_REGISTRY.md"
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out, inside = [], False
    for ln in lines:
        if ln.startswith("## "):
            inside = "Active Lock" in ln
            continue
        if inside and ln.strip().startswith(("- ", "* ", "| ")) and len(ln.strip()) > 8:
            out.append(ln.strip()[:150])
        if len(out) >= limit:
            break
    return out


_CLAUDE_MD_LIMIT = 200  # 공식 권장: code.claude.com/docs/en/memory "target under 200 lines"


def _instruction_budget(root: Path) -> list[str]:
    """지시문 예산 계측 — 라우팅 규칙을 **기계가 지키게** 한다(2026-09-04).

    CLAUDE.md 가 404줄까지 불었던 건 갈 곳이 두 곳(CLAUDE.md·메모리)뿐이었기 때문이다.
    잘라놓기만 하면 6개월 뒤 또 분다. 매 세션 눈에 보이면 안 넘긴다.
    """
    md = root / "CLAUDE.md"
    if not md.is_file():
        return []
    try:
        n = len(md.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return []

    scoped = 0
    files = 0
    rules_dir = root / ".claude" / "rules"
    if rules_dir.is_dir():
        for f in sorted(rules_dir.glob("*.md")):
            try:
                scoped += len(f.read_text(encoding="utf-8", errors="replace").splitlines())
                files += 1
            except OSError:
                continue

    out = ["", "## 📏 지시문 예산"]
    if n > _CLAUDE_MD_LIMIT:
        out.append(
            f"  · 🔴 **CLAUDE.md {n}줄 / 상한 {_CLAUDE_MD_LIMIT} — {n - _CLAUDE_MD_LIMIT}줄 초과.** "
            "길수록 규칙이 묻혀 **안 지켜진다**(공식). 새 규칙 추가 전에 먼저 덜어내라."
        )
    else:
        out.append(f"  · CLAUDE.md {n}줄 / 상한 {_CLAUDE_MD_LIMIT} (여유 {_CLAUDE_MD_LIMIT - n})")
    out.append(f"  · `.claude/rules/` {files}파일 {scoped}줄 — **상시 로드 0줄**(paths 스코프, 해당 파일 열 때만)")
    out.append(
        "  · 새 지식 라우팅: 어기면 사고 → **훅** / 매 세션 필요 → CLAUDE.md / "
        "특정 파일만 → **rules+paths** / 절차 → **스킬** / 사실·이력 → 메모리·DONE 원장"
    )
    return out


def build_session_start_context(root: Path) -> str:
    commits = [ln for ln in _run(["git", "log", "--oneline", "-6"], root).splitlines() if ln]
    dirty = [ln for ln in _run(["git", "status", "--porcelain"], root).splitlines() if ln]
    done = _done_registry(root)

    out = [
        "═══ [크림봇 세션 시작] 지금 상태 — 착수 전에 읽어라 ═══",
        "",
        "🔴 **INVARIANT**: 목표 = 크림 47k **전체**(전 카테고리·거래량 0 포함) × 소싱처 교집합 / "
        "방법 = **푸시**. 역방향 재질문 금지 · 타겟 축소 금지 · 거래량 게이트 1 고정.",
        "🔴 **현재 단계**: 22 소싱처 **안정화**. 소싱처 확장은 안정화 확정 **후**"
        "(순서 뒤집기 금지).",
        "",
        "## 상태",
        f"  · 봇 프로세스: {_bot_alive()}",
        f"  · Fable 오케스트레이션: {_fable_state(root)}",
        f"  · Codex 검증 큐(수동 드레인): {_codex_pending(root)}",
        f"  · 미커밋 변경: {len(dirty)}개 파일",
    ]
    if commits:
        out += ["", "## 최근 커밋 (여기까지 했다 — 또 하지 마라)"] + [f"  · {c}" for c in commits]
    if done:
        out += ["", "## DONE 원장 · Active Lock (재실행 금지)"] + [f"  · {d}" for d in done]

    out += _instruction_budget(root)

    out += [
        "",
        "## 🎀 ponytail 상시 ON (사장 지시 2026-09-04 — 매번 부를 수 없다)",
        "  · **모든 코딩 작업에 ponytail `full` 을 적용한다.** 사장이 `/ponytail` 을 치지 않아도 켜져 있다.",
        "    사다리: 애초에 필요한가(YAGNI) → 이 레포에 이미 있나 → 표준 라이브러리 → 플랫폼 기본 기능",
        "    → 이미 깔린 의존성 → 한 줄 → 그제야 최소 코드. 요청 안 한 추상화·보일러플레이트 금지.",
        "  · **끄기**: 사장이 \"stop ponytail\" / \"normal mode\". 강도: `/ponytail lite|ultra`.",
        "  · 🔴 **양보하지 않는 것** — ponytail 은 이것들보다 **아래**다:",
        "    도메인 의무 에이전트(code-reviewer·profit-analyzer·crawler-builder·scan-debugger·live-tester)",
        "    · TDD · verification-before-completion · 가드 훅 4종 · 사이즈별 실재고 정확성.",
        "    \"게으르게\"는 **덜 쓰라**는 뜻이지 **덜 검증하라**는 뜻이 아니다. 수수료·보관판매·크림 호출 인접은 예외 없이 풀검증.",
        "",
        "## 헬스체크 4종 — 생략 금지 (1개라도 FAIL 이면 매칭/신규 작업 착수 금지)",
        "  1. 봇 프로세스 (위 참조)",
        "  2. 마지막 알림 24h 내 — `alert_sent.fired_at > strftime('%s','now')-86400`",
        "  3. 크림 일일 호출 캡 이하 — `kream_api_calls` where `ts > datetime('now','-1 day')`",
        "  4. 파이프라인 활동 — `decision_log` 최근 2h "
        "(`ts > strftime('%s','now')-7200`) 합 > 0",
        "",
        "  ⚠️ **ts 타입 혼용 금지** — `decision_log.ts`·`alert_sent.fired_at` = **epoch float** "
        "(`strftime('%s','now')-N`), `kream_api_calls.ts`·`bot_state.updated_at` = **TEXT** "
        "(`datetime('now','-1 day')`). 반대로 쓰면 항상 False 또는 전 row 반환 → 오진단.",
        "  컬럼별 `SELECT typeof(ts), ts FROM <table> LIMIT 1` 로 먼저 확인.",
        "═══",
    ]
    return "\n".join(out)


def main() -> int:
    try:
        sys.stdout.write(build_session_start_context(project_root()))
        sys.stdout.write("\n")
    except Exception as exc:  # noqa: BLE001 — fail-safe, never block session
        sys.stderr.write(f"[session_start_inject warn] {exc.__class__.__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
