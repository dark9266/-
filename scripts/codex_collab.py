#!/usr/bin/env python3
"""크림봇 ↔ Codex 협업 CLI — **수동 트리거 전용**.

## 왜 수동인가
ChatGPT Plus 계정이 하나라 구매대행 프로젝트와 **한도를 공유**한다. 크림봇은 Stop 훅
자동 큐잉을 배선하지 않는다 — 사장이 부를 때만 쓴다. 대신 **부른 뒤의 등급 판정은 자동**
(`src/ops/codex_gate`).

## 쓰는 법
    python scripts/codex_collab.py plan                 # ★ 판정만. Codex 호출 0 · 한도 0
    python scripts/codex_collab.py plan --paths src/profit_calculator.py
    python scripts/codex_collab.py verify               # 워킹트리 자동판정 → 큐잉 → 드레인
    python scripts/codex_collab.py verify --tier S      # 등급 수동 강제
    python scripts/codex_collab.py consult "A vs B — 근거와 함께 추천"
    python scripts/codex_collab.py collab  "이 난제 같이 풀자"
    python scripts/codex_collab.py status               # 큐 현황
    python scripts/codex_collab.py drain                # 밀린 것 1건 처리

## 안전
- Codex 는 **검토관·조언자이지 집행자가 아니다** — `-s read-only` 고정, 파일 변경·외부
  fetch·DB write 를 시키지 않는다.
- `-C <크림봇루트>` + `--ignore-user-config` + `-c mcp_servers={}` → 다른 프로젝트 설정·MCP
  미상속(프로젝트 분리 보장).
- env 는 allowlist 통과분만 물려준다(`codex_child_env`) — Discord/무신사 자격증명 자동 차단.
- 출력을 **절대 버리지 않는다**(`.raw.log`). 한도 초과 에러를 못 보고 헛대기하는 사고 방지.
- 한도 초과 = fail-open. 작업을 멈추지 않고 PENDING 으로 남긴다.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ops.codex_child_env import codex_review_child_env  # noqa: E402
from src.ops.codex_gate import (  # noqa: E402
    CodexMode,
    CodexTier,
    plan_codex_collaboration,
)
from src.ops.codex_queue_io import locked_queue  # noqa: E402

QUEUE = ROOT / "reports" / "codex" / "backlog.json"
OUTDIR = ROOT / "reports" / "codex"
DEFAULT_TIMEOUT = 1800  # xhigh 는 900s 를 넘긴다(실측)


# ── 워킹트리 관측 ─────────────────────────────────────────────────────────────
def _git(*args: str) -> str:
    try:
        return subprocess.run(  # noqa: S603 — 훅이 조립한 고정 git argv
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def working_tree_paths() -> tuple[str, ...]:
    """변경·스테이지·추적안됨 경로. 노이즈(캐시·일회용 산출물)는 뺀다."""
    raw = (
        _git("diff", "--name-only")
        + _git("diff", "--cached", "--name-only")
        + _git("ls-files", "--others", "--exclude-standard")
    )
    skip = ("reports/codex/", ".claude/runtime/", ".claude/.orchestration", "__pycache__")
    return tuple(sorted({p for p in raw.splitlines() if p and not p.startswith(skip)}))


def snapshot_id(task: str, paths: tuple[str, ...]) -> str:
    head = _git("rev-parse", "HEAD").strip()
    diff = _git("diff") + _git("diff", "--cached")
    blob = "\0".join((head, diff, task, *paths))
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


# ── 프롬프트 ──────────────────────────────────────────────────────────────────
_ROLE = {
    CodexMode.VERIFY: (
        "You are an independent adversarial reviewer for the KREAM arbitrage bot (크림봇). "
        "Read-only: make no file changes, no network fetch, no DB writes."
    ),
    CodexMode.CONSULT: (
        "You are an independent advisor for the KREAM arbitrage bot (크림봇). "
        "Give a second opinion with reasons. Read-only."
    ),
    CodexMode.COLLABORATE: (
        "You are a collaborating engineer on the KREAM arbitrage bot (크림봇). "
        "Attempt the problem independently, then critique. Read-only."
    ),
}
# 검증(VERIFY) 계약 — 코드에서 실버그를 잡게 한다
_VERIFY_CONTRACT = (
    "Report REAL defects only (max 5). For each: failure scenario / file:line / minimal fix, "
    "3 lines each. If none, answer PASS plus at most 2 lines of residual risk. "
    "Mark speculative edges as THEORETICAL one-liners. Do not restate the diff."
)
# 의논·협업(CONSULT/COLLABORATE) 계약 — 방향 자문. 버그 리포트로 새지 않게 못박는다.
# (1라운드 실측: VERIFY 계약을 그대로 쓰면 방향 질문에도 코덱스가 버그 목록으로 답한다.)
_CONSULT_CONTRACT = (
    "This is a DESIGN CONSULTATION, not a code review. Answer each numbered question in the "
    "brief directly, with concrete architectural recommendations and the reasoning behind "
    "them. Do NOT hunt for bugs or produce a defect/PASS list. When a recommendation carries "
    "tradeoffs, state them explicitly. Respect the stated invariants — never propose anything "
    "that violates them; if a question's best answer would conflict with an invariant, say so "
    "and give the best invariant-compliant alternative."
)
_DOMAIN = (
    "Domain invariants: profit uses live KREAM sell_now only (never local DB prices); "
    "fee = base 2500 + 6% of price, x1.1 VAT, plus 2500 inspection and 3000 shipping; "
    "alert hard floor = net >= 10000 KRW AND ROI >= 5% AND 7d volume >= 1; "
    "the bot is GET-only against KREAM except the storage-sale POST exception."
)


def build_prompt(mode: CodexMode, task: str, paths: tuple[str, ...], snap: str) -> str:
    parts = [
        _ROLE[mode],
        _DOMAIN,
        f"Snapshot: {snap}.",
    ]
    if paths:
        parts.append("Focus paths: " + ", ".join(paths[:40]))
    if task:
        parts.append("Context / question: " + task)
    parts.append(_VERIFY_CONTRACT if mode is CodexMode.VERIFY else _CONSULT_CONTRACT)
    return " ".join(parts)


# ── 큐 ────────────────────────────────────────────────────────────────────────
def enqueue(plan, task: str, paths: tuple[str, ...], snap: str) -> bool:
    """PENDING 항목 추가. 이미 있으면 False."""
    with locked_queue(QUEUE) as (items, save):
        if any(isinstance(x, dict) and x.get("snapshot") == snap for x in items):
            return False
        items.append({
            "snapshot": snap,
            "mode": plan.mode.value,
            "tier": plan.tier.value,
            "model": plan.model,
            "effort": plan.effort,
            "reason": plan.reason,
            "task": task[:2000],
            "paths": list(paths),
            "target": _git("rev-parse", "--short", "HEAD").strip() or "no-head",
            "status": "PENDING",
        })
        save(items)
    return True


def _mark(snap: str, status: str, result_rel: str = "") -> None:
    """상태 갱신 — **재조회 후** 갱신한다.

    codex 서브프로세스가 도는 동안(최대 1800s) 다른 프로세스가 큐를 고쳤을 수 있다.
    처음 읽은 stale 스냅샷을 되쓰면 그 사이 추가분을 지운다.
    """
    with locked_queue(QUEUE) as (items, save):
        for it in items:
            if isinstance(it, dict) and it.get("snapshot") == snap:
                it["status"] = status
                if result_rel:
                    it["result"] = result_rel
                break
        save(items)


def first_pending() -> dict | None:
    """PENDING 첫 건의 **복사본**. 느린 codex 호출은 lock 밖에서 한다."""
    with locked_queue(QUEUE) as (items, _save):
        job = next(
            (x for x in items if isinstance(x, dict) and x.get("status") == "PENDING"), None
        )
        return dict(job) if job else None


# ── 실행 ──────────────────────────────────────────────────────────────────────
def run_job(job: dict) -> int:
    snap = job["snapshot"]
    mode = CodexMode(job.get("mode", "VERIFY"))
    OUTDIR.mkdir(parents=True, exist_ok=True)
    result = OUTDIR / f"codex_{snap}.md"
    raw_log = OUTDIR / f"codex_{snap}.raw.log"
    prompt = build_prompt(mode, job.get("task", ""), tuple(job.get("paths", [])), snap)

    argv = [
        "codex", "exec", "-C", str(ROOT), "-s", "read-only", "--ephemeral",
        "--ignore-user-config", "-c", "mcp_servers={}",
        "-m", job["model"], "-c", f"model_reasoning_effort={job['effort']}",
        "-o", str(result), prompt,
    ]
    timeout_s = int(os.environ.get("KREAM_CODEX_TIMEOUT", str(DEFAULT_TIMEOUT)))
    print(f"▶ {snap} · {job['tier']} · {job['model']}/{job['effort']} · timeout {timeout_s}s")

    try:
        proc = subprocess.run(  # noqa: S603 — 고정 codex 바이너리 + 큐잉된 정책 필드
            argv, cwd=ROOT, env=codex_review_child_env(dict(os.environ)),
            timeout=timeout_s, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as exc:
        # 타임아웃도 raw 증거를 남긴다. 항목은 PENDING 유지(다음 드레인 재시도).
        partial = exc.stdout or b""
        raw_log.write_text(
            f"TIMEOUT after {timeout_s}s argv={argv!r}\n--- partial stdout ---\n"
            + (partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial),
            encoding="utf-8",
        )
        print(f"✗ 실패: {snap} (timeout {timeout_s}s) — PENDING 유지. 로그: {raw_log}")
        return 1
    except FileNotFoundError:
        print("✗ `codex` CLI 를 찾을 수 없다. `npm i -g @openai/codex@latest`")
        return 127

    # raw.log 는 성공/실패 무관 **항상** 기록한다(한도초과 에러를 못 보고 헛대기하는 사고 방지)
    raw_log.write_text(
        f"returncode={proc.returncode} argv={argv!r}\n"
        "--- stdout ---\n" + (proc.stdout or "")
        + "\n--- stderr ---\n" + (proc.stderr or ""),
        encoding="utf-8",
    )
    if proc.returncode == 0 and result.is_file():
        _mark(snap, "DONE", str(result.relative_to(ROOT)))
        print(f"✓ 처리: {snap} → {result.relative_to(ROOT)}")
        return 0
    # 한도 초과 = fail-open. 멈추지 않고 PENDING 으로 남긴다.
    print(f"✗ 실패: {snap} (returncode={proc.returncode}) — PENDING 유지. 로그: {raw_log}")
    print("   한도 초과라면 작업을 멈추지 말고 자체 검증으로 진행한 뒤 나중에 드레인하라.")
    return proc.returncode or 1


# ── 서브커맨드 ────────────────────────────────────────────────────────────────
def _resolve(args, mode: CodexMode):
    paths = tuple(args.paths) if args.paths else (
        working_tree_paths() if mode is CodexMode.VERIFY else ()
    )
    task = args.task or ""
    tier = CodexTier(args.tier) if getattr(args, "tier", None) else None
    return plan_codex_collaboration(mode, task=task, paths=paths, tier=tier), task, paths


def cmd_plan(args) -> int:
    """판정만 — Codex 를 부르지 않는다(한도 0)."""
    mode = CodexMode(args.mode)
    plan, _task, paths = _resolve(args, mode)
    print(f"모드: {mode.value}")
    print(f"대상: {len(paths)}개 경로" + (f" — {', '.join(paths[:8])}" if paths else ""))
    print(f"판정: {plan.label()}")
    if not plan.should_call:
        print("→ 호출하지 않는다. 한도 소모 0.")
    return 0


def _queue_and_run(args, mode: CodexMode) -> int:
    plan, task, paths = _resolve(args, mode)
    if not plan.should_call:
        print(f"판정: {plan.label()}")
        print("→ 호출하지 않는다(한도 0). 강제하려면 `--tier B` 등을 지정하라.")
        return 0
    snap = snapshot_id(task, paths)
    fresh = enqueue(plan, task, paths, snap)
    print(f"판정: {plan.label()}")
    print(f"{'큐잉' if fresh else '기존 항목 재사용'}: {snap}")
    if args.no_drain:
        print("→ --no-drain: 실행하지 않았다. `drain` 으로 처리하라.")
        return 0
    job = first_pending()
    if job is None:
        print("PENDING 없음(다른 프로세스가 먼저 처리했을 수 있다).")
        return 0
    if job["snapshot"] != snap:
        print(f"※ 큐는 FIFO 다 — 먼저 밀린 {job['snapshot']} 부터 처리한다.")
    return run_job(job)


def cmd_verify(args) -> int:
    return _queue_and_run(args, CodexMode.VERIFY)


def cmd_consult(args) -> int:
    return _queue_and_run(args, CodexMode.CONSULT)


def cmd_collab(args) -> int:
    return _queue_and_run(args, CodexMode.COLLABORATE)


def cmd_status(_args) -> int:
    with locked_queue(QUEUE) as (items, _save):
        rows = [x for x in items if isinstance(x, dict)]
    pending = [x for x in rows if x.get("status") == "PENDING"]
    print(f"큐: 전체 {len(rows)}건 · PENDING {len(pending)}건")
    for x in rows[-10:]:
        mark = {"PENDING": "·", "DONE": "✓"}.get(x.get("status", ""), "✗")
        print(f"  {mark} {x.get('snapshot')} {x.get('tier')} "
              f"{x.get('model')}/{x.get('effort')} {x.get('status')} "
              f"{x.get('result', '')}")
    return 0


def cmd_drain(_args) -> int:
    job = first_pending()
    if job is None:
        # 빈 큐와 성공은 둘 다 조용하다 — 명시적으로 알린다(no-op 오인 방지).
        print("PENDING 없음")
        return 0
    return run_job(job)


def main() -> int:
    p = argparse.ArgumentParser(description="크림봇 ↔ Codex 협업 (수동 트리거)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _common(sp, *, with_tier=True):
        sp.add_argument("--paths", nargs="*", help="대상 경로(생략 시 워킹트리 자동)")
        sp.add_argument("--task", default="", help="맥락·질문 한 줄")
        if with_tier:
            sp.add_argument("--tier", choices=[t.value for t in CodexTier],
                            help="등급 수동 지정(판정을 이긴다)")
        sp.add_argument("--no-drain", action="store_true", help="큐잉만 하고 실행 안 함")

    sp = sub.add_parser("plan", help="판정만 — Codex 호출 0, 한도 0")
    sp.add_argument("--paths", nargs="*")
    sp.add_argument("--task", default="")
    sp.add_argument("--tier", choices=[t.value for t in CodexTier])
    sp.add_argument("--mode", default=CodexMode.VERIFY.value,
                    choices=[m.value for m in CodexMode])
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("verify", help="끝낸 변경을 적대검증")
    _common(sp)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("consult", help="방향 제2의견 (최소 A등급)")
    _common(sp)
    sp.set_defaults(func=cmd_consult)

    sp = sub.add_parser("collab", help="난제 협업 (최소 A등급)")
    _common(sp)
    sp.set_defaults(func=cmd_collab)

    sp = sub.add_parser("status", help="큐 현황")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("drain", help="밀린 것 1건 처리")
    sp.set_defaults(func=cmd_drain)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
