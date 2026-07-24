#!/usr/bin/env bash
# scripts/fable.sh — 크림봇 Fable 오케스트레이션 스위치 (토글). zsh/bash 공통.
#
# 켜면: Fable 5(머리)가 무거운 구현은 kream-executor(Opus)·잡무는 kream-explorer(Haiku)에
# 위임하도록 CLAUDE.md 지침을 로드하고, 강제 게이트(orchestration_gate.py)가 메인 직접
# 코드수정을 턴당 5개로 제한한다. 서브에이전트 위임은 무제한.
#
# 스위치는 두 가지만 바꾼다(설정파일·가드훅 무접촉):
#   1) .claude/.orchestration-state          (on/off — 게이트 훅이 읽음)
#   2) .claude/orchestration/active.md       (fable-mode.md 또는 off.md 의 **복사본**)
#
# ★ 심링크가 아니라 복사인 이유: /mnt/c(drvfs) 에서 심링크는 신뢰할 수 없다.
#   구매대행 원본은 `ln -sfn` 을 썼는데 active.md 가 실제로 생성되지 않아
#   CLAUDE.md 의 @import 가 **대상 없이 떠 있었다**. 복사는 그 실패 모드가 없다.
#
# ★ 가드훅(direction_guard · task_intake_guard · claim_evidence_guard)은 이 스위치와
#   **완전 무관** — 늘 켜져 있다. 본 스위치는 한도 효율(오케스트레이션)만 껐다 켠다.
# ★ 적용 시점: 다음에 새로 시작하는 claude 세션부터 (떠 있는 세션은 영향 없음).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
ORCH="$ROOT/.claude/orchestration"
STATE="$ROOT/.claude/.orchestration-state"

case "${1:-status}" in
  on)
    echo on > "$STATE"
    cp -f "$ORCH/fable-mode.md" "$ORCH/active.md"
    echo "Fable 오케스트레이션 ON — 다음 claude 세션부터 적용."
    echo "  머리=Fable5 / 구현=kream-executor(Opus) / 잡무=kream-explorer(Haiku)."
    ;;
  off)
    echo off > "$STATE"
    cp -f "$ORCH/off.md" "$ORCH/active.md"
    echo "Fable 오케스트레이션 OFF — 다음 claude 세션부터 일반 모드."
    ;;
  status)
    s=$(cat "$STATE" 2>/dev/null || echo off)
    echo "Fable 오케스트레이션: $(echo "$s" | tr '[:lower:]' '[:upper:]')"
    if head -1 "$ORCH/active.md" 2>/dev/null | grep -q 'ON'; then
      echo "  active.md -> fable-mode (ON 지침 로드됨)"
    else
      echo "  active.md -> off (지침 없음)"
    fi
    grep -q 'orchestration/active.md' "$ROOT/CLAUDE.md" 2>/dev/null && ok='[OK]' || ok='[--]'
    echo "  $ok CLAUDE.md @import"
    grep -q 'orchestration_gate' "$ROOT/.claude/settings.json" 2>/dev/null && ok='[OK]' || ok='[--]'
    echo "  $ok 게이트 훅 등록(settings.json)"
    if [ -f "$ORCH/../agents/kream-executor.md" ] && [ -f "$ORCH/../agents/kream-explorer.md" ]
    then ok='[OK]'; else ok='[--]'; fi
    echo "  $ok 워커 에이전트(executor/explorer)"
    echo "  [가드] direction/task_intake/claim_evidence 훅은 스위치 무관 항상 ON"
    ;;
  *)
    echo "사용법: bash scripts/fable.sh on | off | status" >&2
    exit 1
    ;;
esac
