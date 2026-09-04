#!/bin/bash
# CLAUDE.md 정합성 체크 — Stop 훅에서 실행
# 새 파일/에이전트/명령이 CLAUDE.md에 누락된 경우 경고 출력

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || exit 0

WARNINGS=""

# 지시문은 CLAUDE.md 한 곳이 아니다 — 2026-09-04 라우팅 분리 이후
# 경로별 규칙은 .claude/rules/*.md 에 산다(paths: 프론트매터로 스코프됨).
# 등록 여부는 **두 곳을 합쳐** 판정한다. CLAUDE.md 만 보면 옮긴 내용이 전부 미등록으로 잡힌다.
DOC_FILES=(CLAUDE.md)
for r in .claude/rules/*.md; do [ -f "$r" ] && DOC_FILES+=("$r"); done

# 지시문 전체에서 문자열 찾기 (등록 = 어느 지시문 파일에든 이름이 있다)
doc_has() {
    grep -q -- "$1" "${DOC_FILES[@]}" 2>/dev/null
}

# 1. src/ 핵심 모듈만 체크 (utils/, models/, discord_bot/ 내부 및 레거시 제외)
SKIP_PATTERNS="__init__|utils/|models/|discord_bot/|abcmart|kream_db_builder|price_tracker"
for f in src/*.py src/crawlers/*.py src/kream_realtime/*.py; do
    [ -f "$f" ] || continue
    basename=$(basename "$f")
    [[ "$basename" == __* ]] && continue
    echo "$f" | grep -qE "$SKIP_PATTERNS" && continue
    if ! doc_has "$basename"; then
        WARNINGS+="  - 모듈 '$f' → CLAUDE.md/rules 에 미등록\n"
    fi
done

# 2. .claude/agents/ 에이전트가 CLAUDE.md 에이전트 표에 없는 경우
for f in .claude/agents/*.md; do
    [ -f "$f" ] || continue
    agent_name=$(basename "$f" .md)
    if ! doc_has "$agent_name"; then
        WARNINGS+="  - 에이전트 '$agent_name' → CLAUDE.md/rules 에 미등록\n"
    fi
done

# 3. .claude/commands/ 슬래시 명령이 CLAUDE.md 명령 표에 없는 경우
for f in .claude/commands/*.md; do
    [ -f "$f" ] || continue
    cmd_name=$(basename "$f" .md)
    if ! doc_has "/$cmd_name"; then
        WARNINGS+="  - 슬래시 명령 '/$cmd_name' → CLAUDE.md/rules 에 미등록\n"
    fi
done

# 4. "구현 예정" / "v2 예정" 잔존 체크
pending_count=$(grep -c '구현 예정\|v2 예정\|확장 예정' CLAUDE.md 2>/dev/null)
pending_count=${pending_count:-0}
if [ "$pending_count" -gt 0 ]; then
    WARNINGS+="  - CLAUDE.md에 '예정' 표기 ${pending_count}건 잔존\n"
fi

# 5. false_positives.json known_bug 잔존
if [ -f tests/fixtures/false_positives.json ]; then
    bug_info=$(python3 -c "
import json
cases = json.load(open('tests/fixtures/false_positives.json')).get('cases', [])
known = [c for c in cases if c.get('status') == 'known_bug']
if known:
    print(f'{len(known)}건 미수정 known_bug')
" 2>/dev/null)
    [ -n "$bug_info" ] && WARNINGS+="  - false_positives.json: $bug_info\n"
fi

# 출력
if [ -n "$WARNINGS" ]; then
    echo ""
    echo "CLAUDE.md 정합성 경고:"
    echo -e "$WARNINGS"
fi
