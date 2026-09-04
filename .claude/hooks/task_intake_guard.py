#!/usr/bin/env python3
"""UserPromptSubmit 훅 — **중복 작업 방지 · 망각 방지 · 방향 이탈 경고**.

## 왜 (사장 2026-07-24: "했던 일 중복으로 하지 않기, 방향 새지 않기 — 훅으로 막아줘")

**기록은 내가 *읽어야* 작동한다. 그런데 나는 안 읽는다.**
CLAUDE.md INVARIANT·메모리·done 원장은 전부 **일이 끝난 뒤** 쓰고, 다음 작업 **시작할 때
안 읽는다**. 앤트로픽 공식: *"'매번 X 하면 항상 Y' 는 훅으로 하라 — 긴 세션에서 압박받을 때
모델은 프롬프트된 규칙을 못 지킨다."*

→ 내가 **읽기로 선택하지 않아도** 훅이 찾아서 눈앞에 놓는다.

## 무엇을 하나
1. 프롬프트에서 **작업 토큰**을 뽑는다(모델번호·소싱처·파일명·기능어. 한국어 조사 벗김).
2. 그 토큰으로 **done 원장 · 메모리 · git log · 기존 모듈 · 스킬**을 자동 검색한다.
3. CLAUDE.md **🚫 금지 리스트** 저촉 신호를 잡아 경고한다(방향 이탈 사전 차단).
4. 결과를 stdout 으로 출력 → Claude Code 가 `additionalContext` 로 프롬프트에 붙인다.
5. **작업 계약**을 `.claude/runtime/<session>/contract.json` 에 남긴다(증거게이트 카운터 리셋).

## 안전
read-only(파일 읽기·grep 만). 실패해도 프롬프트를 죽이지 않는다(fail-open, exit 0).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_util import memory_dir, project_root  # noqa: E402

MAX_HITS_PER_SOURCE = 6
MAX_TOKEN = 12

# ── 작업 토큰 추출 ────────────────────────────────────────────────────────────
_STOP = frozenset({
    "그리고", "그럼", "해줘", "해라", "하자", "이거", "저거", "지금", "다시", "좀",
    "계속", "일단", "제발", "완벽하게", "확실히", "진행", "시작", "완료", "해봐",
    "어떻게", "무엇", "그거", "이건", "저건", "우리", "너는", "네가",
    "the", "and", "for", "with", "this", "that", "have", "been", "from",
})
# 22 소싱처 (한/영 — CLAUDE.md 크롤러·어댑터 목록)
_SOURCES = (
    "musinsa", "무신사", "29cm", "twentynine", "nike", "나이키", "adidas", "아디다스",
    "kasina", "카시나", "abcmart", "그랜드스테이지", "온더스팟", "onthespot", "tune", "튠",
    "eql", "nbkorea", "뉴발란스", "salomon", "살로몬", "arcteryx", "아크테릭스",
    "vans", "반스", "wconcept", "더블유컨셉", "컨셉", "on_running", "온러닝", "stussy",
    "스투시", "patagonia", "파타고니아", "beaker", "비이커", "thehandsome", "한섬",
    "puma", "푸마", "asics", "아식스", "thenorthface", "노스페이스", "kream", "크림",
)

_PARTICLES = (
    "으로써", "으로서", "이라는", "라는", "에서는", "에서", "으로", "이라", "라고",
    "에게", "한테", "까지", "부터", "처럼", "보다", "마다", "조차", "이나", "이며",
    "은", "는", "이", "가", "을", "를", "의", "에", "도", "만", "과", "와", "로", "랑",
)


def _strip_particle(word: str) -> str:
    """한국어 조사를 벗긴 어간. 순수.

    조사가 붙으면 문서·스킬 설명과 문자열 매칭이 **통째로 실패**한다
    ("봇월**이라**" ≠ "봇월" → 봇월 우회 전문 스킬이 안 잡힌다).
    """
    for suf in _PARTICLES:  # 긴 것부터(위 튜플이 길이순)
        if len(word) > len(suf) + 1 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def extract_tokens(prompt: str) -> list[str]:
    """프롬프트 → 검색할 작업 토큰. **순수**(테스트가 직접 부른다)."""
    p = prompt or ""
    toks: list[str] = []

    toks += re.findall(r"\b[A-Z]{1,3}\d{4,6}-\d{3}\b", p)      # 나이키 DZ5485-100
    toks += re.findall(r"\b[A-Z]{2}\d{4}\b", p)                # 아디다스 IH1325
    toks += re.findall(r"\bL\d{8}\b", p)                       # 살로몬 L41234567
    toks += re.findall(r"\b[A-Z]{2}\d{3}[A-Z]{2}\d\b", p)      # 뉴발란스 MT410CK5
    toks += re.findall(r"\b[\w/]+\.(?:py|sh|json|toml|md)\b", p)
    low = p.lower()
    toks += [s for s in _SOURCES if s in low]

    for w in re.findall(r"[가-힣]{2,}|[A-Za-z_]{4,}", p):
        wl = w.lower()
        if wl in _STOP:
            continue
        toks.append(w)
        stem = _strip_particle(w)
        if stem and stem != w and len(stem) >= 2:
            toks.append(stem)

    seen, out = set(), []
    for t in toks:
        k = t.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out[:MAX_TOKEN]


# ── 🚫 금지 리스트 저촉 신호 (CLAUDE.md INVARIANT) ────────────────────────────
# (정규식, 경고문) — 차단이 아니라 **경고 주입**이다. 사장이 판단한다.
_FORBIDDEN = (
    (re.compile(r"(역방향|reverse_scanner|continuous_scanner|tier1_scanner|cold.?warm)", re.I),
     "폐기 흐름(v2 역방향/Tier)이다. 현행은 **푸시 단일 트랙**. "
     "리팩터·버그픽스·재활용 제안 금지. "
     "⚠️ 단 `tier2_monitor.py` 는 축 ② 보조로 **유지 판정 완료**."),
    (re.compile(r"(소싱처.{0,4}(추가|확장|늘리)|새.{0,2}소싱처|신규.{0,2}소싱처)", re.I),
     "소싱처 신규 추가는 **22곳 안정화 확정 후**다(단계 1→2, 순서 뒤집기 금지)."),
    (re.compile(r"(해외|미국|유럽|일본|\bEU\b|\bUS\b|\bJP\b).{0,6}(스토어|몰|공홈|사이트)", re.I),
     "소싱처는 **한국 공홈만**. EU/US/JP 금지(KRW 정합성)."),
    (re.compile(r"(hot.{0,4}(만|위주|우선)|인기.{0,3}카테고리|신발만|거래량.{0,4}(5|10|이상).{0,3}만|"
                r"타겟.{0,3}축소)", re.I),
     "🎯 타겟은 **47k 전체(전 카테고리, 거래량 0 포함) 고정**. 축소 전부 금지 — "
     "거래량 낮은 상품의 마진이 가장 크다(숨은 보석 = 핵심 차별화)."),
    (re.compile(r"거래량.{0,6}(게이트|기준).{0,6}(낮|내리|완화|올리)", re.I),
     "거래량 게이트는 **1 고정**. 낮추자/올리자 재제안 금지 — 거짓 알림 방어는 "
     "순수익/ROI 하드플로어 담당(`project_hidden_gem_policy`)."),
    (re.compile(r"(로컬|local).{0,6}(DB|디비).{0,10}(수익|시세|가격).{0,4}계산", re.I),
     "크림 시세/거래량을 로컬 DB로 수익 계산 **금지**. 반드시 실시간 `sell_now`."),
)


def forbidden_hits(prompt: str) -> list[str]:
    """프롬프트가 CLAUDE.md 금지 리스트를 건드리는가. **순수**."""
    return [msg for rx, msg in _FORBIDDEN if rx.search(prompt or "")]


# ── 이미 한 일 검색 ───────────────────────────────────────────────────────────
def _grep_file(path: Path, tokens: list[str], limit: int) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    hits: list[str] = []
    for ln in lines:
        low = ln.lower()
        for t in tokens:
            if t.lower() in low and len(ln.strip()) > 8:
                hits.append(ln.strip()[:160])
                break
        if len(hits) >= limit:
            break
    return hits


def _git_log(tokens: list[str], limit: int) -> list[str]:
    """커밋 제목 매칭 — **이미 봉합한 걸 또 봉합하는 것**을 막는다."""
    try:
        out = subprocess.run(  # noqa: S607 — git 은 PATH 에서 찾는다(훅 실행 환경)
            ["git", "log", "--oneline", "-60"],
            cwd=project_root(), capture_output=True, text=True, timeout=8,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    hits: list[str] = []
    for ln in out.splitlines():
        low = ln.lower()
        for t in tokens:
            if len(t) >= 3 and t.lower() in low:
                hits.append(ln.strip()[:140])
                break
        if len(hits) >= limit:
            break
    return hits


def _existing_modules(tokens: list[str], limit: int) -> list[str]:
    """같은 도메인 토큰을 가진 **기존 모듈** — 중복 제작 방지."""
    root = project_root()
    want = {t.lower() for t in tokens if len(t) >= 4 and re.fullmatch(r"[a-z_]+", t.lower())}
    if not want:
        return []
    hits: list[str] = []
    for base in ("src", "scripts"):
        d = root / base
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            if p.name.startswith("_") or "__pycache__" in p.parts:
                continue
            stem = {w for w in re.split(r"[^a-z0-9]+", p.stem.lower()) if len(w) >= 4}
            shared = want & stem
            if shared:
                hits.append(f"{p.relative_to(root)}  (공유: {', '.join(sorted(shared))})")
            if len(hits) >= limit:
                return hits
    return hits


_BLOCK_SCALARS = (">", "|", ">-", "|-", ">+", "|+", "")


def _front_matter_description(body: str) -> str:
    """SKILL.md frontmatter 의 `description` — YAML 블록 스칼라(`>` `|`)까지 이어 읽는다.

    첫 줄만 읽으면 `description: >` 형태 스킬(ponytail 6개)이 빈 문자열이 되어
    **자동 소환에서 통째로 증발**한다. 2026-09-04 실측으로 잡힌 결함.
    """
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if not ln.startswith("description:"):
            continue
        head = ln[len("description:"):].strip()
        if head not in _BLOCK_SCALARS:
            return head
        parts: list[str] = []
        for nxt in lines[i + 1:]:
            if nxt.strip() and not nxt.startswith((" ", "\t")):
                break  # 들여쓰기가 끝나면 다음 키 — 블록 종료
            parts.append(nxt.strip())
        return " ".join(p for p in parts if p)
    return ""


def _relevant_skills(tokens: list[str], prompt: str, limit: int = 3) -> list[str]:
    """★ **스킬 자동 소환** — 있는데 안 부르는 게 병이다.

    스킬 저장은 하는데 **소환이 없다**. 앤트로픽: "모델이 포매터를 *돌리기로 선택하는 것*과
    포매터가 *자동으로 돌아가는 것*은 다르다." → 훅이 소환한다.
    """
    root = project_root()
    low = (prompt or "").lower()
    toks = {t.lower() for t in tokens}

    cands: list[Path] = []
    sk_dir = root / ".claude" / "skills"
    if sk_dir.is_dir():
        cands += [d for d in sorted(sk_dir.iterdir()) if d.is_dir()]
    plug = Path.home() / ".claude" / "plugins"
    if plug.is_dir():
        try:
            cands += sorted({f.parent for f in plug.rglob("SKILL.md")})
        except OSError:
            pass
    if not cands:
        return []

    hits: list[tuple[int, str, str]] = []
    for d in cands:
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        desc = _front_matter_description(body)

        # 점수 = **어디에서 맞았는가**로 가중(오탐이 섞이면 아무도 안 읽는다).
        # description 앞부분(정체) > 뒷부분(트리거 존)·"언제" 절 > 본문.
        # 절단은 표시용만 — 매칭은 전문으로 한다(뒤쪽 트리거 토큰 증발 방지).
        low_desc_head = desc[:110].lower()
        low_desc = desc.lower()
        when = ""
        for marker in ("## 언제", "## 어디에 쓰나", "## 트리거"):
            i = body.find(marker)
            if i >= 0:
                when += body[i:i + 900].lower()
        body_low = body[:2500].lower()

        score = 0
        for tok in toks:
            # ★ **한국어는 2글자가 핵심어**다(봇월·품번·마진·재고·매칭·수익…).
            #   영어 기준 len>=3 필터가 한국어 핵심어를 통째로 죽인다.
            min_len = 2 if re.search(r"[가-힣]", tok) else 4
            if len(tok) < min_len:
                continue
            if tok in low_desc_head:
                score += 5
            elif tok in low_desc:
                score += 3
            elif tok in when:
                score += 3
            elif tok in body_low:
                score += 1
        for w in d.name.split("-")[1:]:
            if len(w) >= 4 and w in low:
                score += 4
        if score >= 3:  # 약한 히트(본문 1회)만으로는 소환 안 함 — 노이즈 차단
            hits.append((score, d.name, desc[:110]))
    hits.sort(key=lambda h: -h[0])
    return [f"{n}  —  {desc}" for _s, n, desc in hits[:limit]]


def build_context(prompt: str, root: Path | None = None) -> str:
    """프롬프트 → 주입할 '이미 한 일' 컨텍스트. **순수 조합**(파일 IO 는 하위 함수)."""
    forbidden = forbidden_hits(prompt)
    tokens = extract_tokens(prompt)
    if not tokens and not forbidden:
        return ""
    r = root or project_root()

    done = _grep_file(r / "docs" / "DONE_REGISTRY.md", tokens, MAX_HITS_PER_SOURCE)
    commits = _git_log(tokens, MAX_HITS_PER_SOURCE)
    mods = _existing_modules(tokens, 5)

    # 메모리 = 크림봇의 뇌기록. 정제된 교훈이 일기보다 오래 간다.
    memory: list[str] = []
    mdir = memory_dir(r)
    if mdir.is_dir():
        for f in sorted(mdir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            memory += [f"[{f.stem}] {h}" for h in _grep_file(f, tokens, 2)]
    memory = memory[:MAX_HITS_PER_SOURCE]

    skills = _relevant_skills(tokens, prompt)

    if not (done or commits or mods or memory or skills or forbidden):
        return ""

    out = [
        "═══ [입고 게이트] 이 작업과 관련해 **이미 한 일** — 만들거나 고치기 전에 읽어라 ═══",
        f"(자동 검색 토큰: {', '.join(tokens[:8])})",
        "",
    ]
    if forbidden:
        out += ["## 🚫 CLAUDE.md 금지 리스트 저촉 신호 — 착수 전 확인"]
        out += [f"  · {m}" for m in forbidden]
        out += ["   → 사장이 명시적으로 지시한 게 아니면 **방향이 새고 있는 것**이다.", ""]
    if skills:
        out += ["## ★ 이 작업에 **쓸 수 있는 스킬** (있는데 안 부르는 게 병이다)"]
        out += [f"  · **{s}**" for s in skills]
        out += ["   → 해당되면 **Skill 도구로 먼저 불러라**.", ""]
    if commits:
        out += ["## 최근 커밋 (이미 봉합했을 수 있다)"] + [f"  · {c}" for c in commits] + [""]
    if done:
        out += ["## DONE 원장 (재실행 금지)"] + [f"  · {d}" for d in done] + [""]
    if mods:
        out += ["## 같은 도메인의 **기존 모듈** (중복 제작 금지 — 있으면 확장하라)"]
        out += [f"  · {m}" for m in mods] + [""]
    if memory:
        out += ["## 메모리 — 정제된 교훈(결정·판정은 여기서 끝났을 수 있다)"]
        out += [f"  · {k}" for k in memory] + [""]
    out += ["═══ 위를 **읽고** 시작하라. 이미 있는 걸 다시 만들면 그게 오늘의 반복이다. ═══"]
    return "\n".join(out)


def _write_contract(session: str, prompt: str) -> None:
    """방향 상실 감시용 — 사장이 준 목표를 세션 계약으로 고정."""
    try:
        d = project_root() / ".claude" / "runtime" / (session or "nosession")
        d.mkdir(parents=True, exist_ok=True)
        (d / "contract.json").write_text(
            json.dumps(
                {"prompt": prompt[:4000], "tokens": extract_tokens(prompt),
                 "forbidden": forbidden_hits(prompt)},
                ensure_ascii=False, indent=1),
            encoding="utf-8")
        # 새 사장 지시 = 새 턴 → 증거게이트 차단 카운터 전부 리셋.
        # (특정 fp 만 지우면 같은 프롬프트 반복 시 옛 카운터를 물려받아 즉시 fail-open 된다.)
        for f in d.glob("claim_blocks_*"):
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0  # fail-open
    # `null`·`[]` 도 유효한 JSON 이다 — 파싱은 되고 `.get()` 에서 죽는다(세션 사망).
    if not isinstance(payload, dict):
        return 0

    prompt = str(payload.get("prompt") or "")
    _write_contract(str(payload.get("session_id") or ""), prompt)

    try:
        ctx = build_context(prompt)
    except Exception:  # noqa: BLE001 — 검색 실패해도 프롬프트를 죽이지 않는다
        return 0
    if ctx:
        print(ctx)  # stdout → additionalContext
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
