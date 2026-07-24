"""크림봇 ↔ Codex 협업 게이트 — 순수 정책. IO 없음, Codex 를 호출하지 않는다.

## 왜 이 모듈이 따로 있나
ChatGPT Plus 한도는 **계정 하나를 구매대행 프로젝트와 공유**한다. 그래서 크림봇에서는
Codex 를 **사장이 부를 때만** 쓴다(Stop 훅 자동 큐잉 미배선). 대신 부른 뒤의 판단 —
"이 변경을 어느 모델·effort 로 태울 것인가" — 는 자동이어야 사람이 매번 고르지 않는다.
그 판단이 곧 한도 소모량이므로 여기 한 곳에 못박고 테스트로 고정한다.

## 등급
| 등급 | 모델·effort | 무엇 | 근거 |
|:-:|---|---|---|
| S | sol/xhigh | 보관판매(크림 POST)·수익계산·수수료·자격증명·훅 | 버그 1개 = 밴/실손실 |
| A | sol/high | DB 스키마·아키텍처/ADR 의논·난제 협업 | 판단 품질 우선 |
| B | terra/high | **기본값** — 코어·크롤러·어댑터·매처 등 일상 코드 | sol 대비 1/2 크레딧 |
| C | luna/high | 넓게 훑기(열거·커버리지 전수) | 판단 아닌 커버리지. 1/5 크레딧 |
| N | 호출 없음 | 문서·일회용 진단 스크립트·읽기전용 | 한도 0 |

`sol/max` 는 자동 경로에 없다 — xhigh 로도 결론이 안 날 때 사람이 1회만 지정한다.
luna 에게 최종 판단을 맡기지 않는다(열거·breadth 전용).

크레딧 실측(1M 토큰 입력/출력): sol 125/750 · terra 62.5/375 · luna 25/150.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CodexTier(str, Enum):
    S = "S"
    A = "A"
    B = "B"
    C = "C"
    N = "N"


class CodexMode(str, Enum):
    VERIFY = "VERIFY"
    CONSULT = "CONSULT"
    COLLABORATE = "COLLABORATE"


MODEL_BY_TIER: dict[CodexTier, tuple[str, str]] = {
    CodexTier.S: ("gpt-5.6-sol", "xhigh"),
    CodexTier.A: ("gpt-5.6-sol", "high"),
    CodexTier.B: ("gpt-5.6-terra", "high"),
    CodexTier.C: ("gpt-5.6-luna", "high"),
    CodexTier.N: ("", ""),
}

_TIER_RANK = {CodexTier.N: 0, CodexTier.C: 1, CodexTier.B: 2, CodexTier.A: 3, CodexTier.S: 4}

# ── S: 사고 직결 ────────────────────────────────────────────────────────────
# CLAUDE.md 의 "POST 금지 예외"(보관판매·가격갱신) + 수익 로직 + 자격증명 + 훅.
# 넓히면 일상 변경까지 sol/xhigh 를 태우므로 근거 있는 경로만 넣는다.
_S_PATH_PREFIXES = (
    "src/storage_sale/",
    "src/profit_calculator.py",
    "src/config.py",
    ".claude/hooks/",
    ".env",
)
_S_TERMS = (
    # 한국어 — 사장 지시는 한국어라 영어 토큰만 보면 안전경계가 샌다
    "보관판매", "자동경쟁", "가격 갱신", "가격갱신", "수수료", "정산", "검수비",
    "하드플로어", "하드 플로어", "시그널 기준", "자격증명", "웹훅", "결제",
    "호출캡", "일일캡", "일일 캡",
    # 영어·식별자
    "storage_sale", "credential", "secret", "webhook", "api_key", "apikey",
    "payment", "kream_daily_cap", "daily_cap", "hard floor",
)

# ── A: 판단 품질 ────────────────────────────────────────────────────────────
_A_PATH_PREFIXES = (
    "src/models/",
    "scripts/migrate_",
)
_A_TERMS = (
    "아키텍처", "설계", "방향 결정", "선택지", "스키마", "마이그레이션",
    "adr", "architecture", "trade-off", "tradeoff",
)

# ── C: 넓게 훑기 ────────────────────────────────────────────────────────────
# **요청 문구에서만** 판정한다. 경로 문자열(diag_coverage.py 등)에 우연히 섞인 단어로
# 등급이 바뀌면 안 되기 때문.
_C_TERMS = (
    "전수", "열거", "커버리지", "훑기", "훑어", "breadth", "sweep", "enumerate",
)

_CODE_SUFFIXES = (".py", ".sh", ".toml", ".json", ".yaml", ".yml")
# .gitignore 대상 일회용 스크립트 — 한도 태울 이유 없다
_THROWAWAY_PREFIXES = (
    "scripts/probe_", "scripts/diag_", "scripts/repro_", "scripts/_",
)


@dataclass(frozen=True)
class CodexPlan:
    """가장 싼, 그러나 충분한 독립검토 계획."""

    tier: CodexTier
    mode: CodexMode
    model: str
    effort: str
    reason: str

    @property
    def should_call(self) -> bool:
        """False 면 Codex 를 부르지 않는다(한도 0)."""
        return self.tier is not CodexTier.N

    def label(self) -> str:
        if not self.should_call:
            return f"{self.tier.value} · 호출 없음 — {self.reason}"
        return f"{self.tier.value} · {self.model}/{self.effort} — {self.reason}"


def _hit(text: str, terms: tuple[str, ...]) -> str:
    """매칭된 첫 용어를 돌려준다(빈 문자열 = 미매칭) — 판정 이유를 남기기 위함."""
    return next((t for t in terms if t in text), "")


def _is_throwaway(path: str) -> bool:
    return path.startswith(_THROWAWAY_PREFIXES)


def _code_paths(paths: tuple[str, ...]) -> list[str]:
    return [
        p for p in paths
        if p.lower().endswith(_CODE_SUFFIXES) and not _is_throwaway(p)
    ]


def classify_tier(task: str = "", paths: tuple[str, ...] = ()) -> CodexTier:
    """경로·요청문구로 중요도 등급을 판정한다(모드 무관·IO 없음)."""
    return _classify(task, paths)[0]


def _classify(task: str, paths: tuple[str, ...]) -> tuple[CodexTier, str]:
    text = " ".join((task, *paths)).lower()

    if any(p.startswith(_S_PATH_PREFIXES) for p in paths):
        return CodexTier.S, "크림 POST·수익·자격증명 경계 경로 — 버그 1개가 밴/실손실"
    term = _hit(text, _S_TERMS)
    if term:
        return CodexTier.S, f"안전·금전 경계 용어('{term}') — 지능을 낮추지 않는다"

    if any(p.startswith(_A_PATH_PREFIXES) for p in paths):
        return CodexTier.A, "DB 스키마·마이그레이션 — 데이터 유실 위험"
    term = _hit(text, _A_TERMS)
    if term:
        return CodexTier.A, f"설계·구조 판단('{term}') — 판단 품질 우선"

    code = _code_paths(paths)

    # 훑기는 **요청 문구에서만** 판정하고, 실제 코드 변경이 있으면 검증이 우선한다
    # (luna 에게 최종 판단을 맡기지 않는다).
    if not code:
        term = _hit(task.lower(), _C_TERMS)
        if term:
            return CodexTier.C, f"넓게 훑기('{term}') — 판단 아닌 커버리지"

    if code and (any(p.endswith((".py", ".sh")) for p in code) or len(code) >= 3):
        return CodexTier.B, f"코드 변경 {len(code)}건 — 일상 적대검증"

    return CodexTier.N, "문서·일회용 진단·읽기전용 — 한도 소모 없음"


def plan_codex_collaboration(
    mode: CodexMode,
    task: str = "",
    paths: tuple[str, ...] = (),
    tier: CodexTier | None = None,
) -> CodexPlan:
    """모드 + 판정 등급 → 최종 모델·effort.

    `tier` 를 주면 판정을 이긴다(사람이 등급을 아는 경우). 의논·협업은 사람이 이미
    "판단이 필요하다"고 선언한 것이므로 **최소 A 등급**으로 올린다 — 문서만 건드린
    상태에서 방향을 물어도 terra 밑으로는 내려가지 않는다.
    """
    if tier is not None:
        model, effort = MODEL_BY_TIER[tier]
        return CodexPlan(tier, mode, model, effort, "등급 수동 지정")

    resolved, reason = _classify(task, paths)
    if mode in (CodexMode.CONSULT, CodexMode.COLLABORATE) and (
        _TIER_RANK[resolved] < _TIER_RANK[CodexTier.A]
    ):
        resolved, reason = CodexTier.A, f"{mode.value} 요청 — 판단 품질 우선(최소 A)"

    model, effort = MODEL_BY_TIER[resolved]
    return CodexPlan(resolved, mode, model, effort, reason)
