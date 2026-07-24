"""코덱스 협업 게이트 — 중요도 등급 → 모델·effort 라우팅 판정 테스트.

크림봇은 **수동 트리거 전용**(Stop 훅 자동 큐잉 없음, ChatGPT Plus 한도를 구매대행과 공유).
호출했을 때 어느 등급으로 태울지만 자동 판정한다 — 그 판정이 곧 한도 소모량이라 여기서 못박는다.
"""

from src.ops.codex_gate import (
    MODEL_BY_TIER,
    CodexMode,
    CodexTier,
    classify_tier,
    plan_codex_collaboration,
)

# ---------------------------------------------------------------- S: 사고 직결


def test_storage_sale_path_is_top_tier():
    """Phase B 보관판매 = 크림 POST. 버그 1개가 밴 직결 — 지능 낮추지 않는다."""
    assert classify_tier(paths=("src/storage_sale/register.py",)) is CodexTier.S


def test_profit_calculator_is_top_tier():
    assert classify_tier(paths=("src/profit_calculator.py",)) is CodexTier.S


def test_credential_paths_are_top_tier():
    assert classify_tier(paths=("src/config.py",)) is CodexTier.S
    assert classify_tier(paths=(".env",)) is CodexTier.S


def test_claude_hooks_are_top_tier():
    assert classify_tier(paths=(".claude/hooks/orchestration_gate.py",)) is CodexTier.S


def test_korean_safety_terms_reach_top_tier():
    """사장 지시는 한국어다 — 영어 토큰만 보면 안전경계가 샌다."""
    for term in ("보관판매 등록 붙여줘", "수수료 공식 고쳐", "검수비 정정", "자격증명 갈아끼워"):
        assert classify_tier(task=term) is CodexTier.S, term


def test_kream_daily_cap_is_top_tier():
    assert classify_tier(task="KREAM_DAILY_CAP 상향") is CodexTier.S


# ---------------------------------------------------------------- A: 판단 품질


def test_db_schema_is_high_tier():
    assert classify_tier(paths=("src/models/database.py",)) is CodexTier.A


def test_design_discussion_is_high_tier():
    for term in ("아키텍처 방향 잡아줘", "adr 쓸 건데", "설계 선택지 두 개"):
        assert classify_tier(task=term) is CodexTier.A, term


def test_safety_boundary_outranks_design():
    """안전 경계가 걸리면 설계 의논이어도 sol/xhigh."""
    tier = classify_tier(task="보관판매 아키텍처 설계", paths=("src/models/database.py",))
    assert tier is CodexTier.S


# ---------------------------------------------------------------- B: 일상 검증


def test_ordinary_python_change_is_default_tier():
    assert classify_tier(paths=("src/matcher.py",)) is CodexTier.B
    assert classify_tier(paths=("src/core/orchestrator.py",)) is CodexTier.B


def test_new_adapter_is_default_tier():
    assert classify_tier(paths=("src/adapters/newshop_adapter.py",)) is CodexTier.B


def test_three_non_python_code_files_reach_default_tier():
    assert classify_tier(paths=("a.toml", "b.json", "c.yaml")) is CodexTier.B


def test_two_non_python_code_files_stay_no_call():
    assert classify_tier(paths=("a.toml", "b.json")) is CodexTier.N


# ---------------------------------------------------------------- C: 넓게 훑기


def test_full_sweep_request_is_breadth_tier():
    assert classify_tier(task="22 소싱처 경로 전수 열거해줘") is CodexTier.C


def test_code_change_outranks_breadth():
    """훑기 문구가 있어도 실제 코드가 바뀌었으면 판정이 필요하다 — luna 에 판단 위임 금지."""
    assert classify_tier(task="커버리지 훑기", paths=("src/matcher.py",)) is CodexTier.B


def test_breadth_is_judged_from_request_text_only():
    """경로 문자열에 우연히 들어간 단어(diag_coverage.py)로 등급이 바뀌면 안 된다."""
    assert classify_tier(paths=("scripts/diag_coverage.py",)) is CodexTier.N


# ---------------------------------------------------------------- N: 한도 0


def test_docs_only_change_is_no_call():
    assert classify_tier(paths=("docs/ops/foo.md", "README.md")) is CodexTier.N


def test_throwaway_diagnostic_scripts_are_no_call():
    """probe_/diag_/repro_ 는 .gitignore 대상 일회용 — 한도 태울 이유 없다."""
    for path in ("scripts/probe_adidas.py", "scripts/diag_coverage.py", "scripts/repro_x.py"):
        assert classify_tier(paths=(path,)) is CodexTier.N, path


def test_empty_input_is_no_call():
    assert classify_tier() is CodexTier.N


# ---------------------------------------------------------------- 모델·effort 매핑


def test_tier_to_model_effort_mapping_is_fixed():
    assert MODEL_BY_TIER[CodexTier.S] == ("gpt-5.6-sol", "xhigh")
    assert MODEL_BY_TIER[CodexTier.A] == ("gpt-5.6-sol", "high")
    assert MODEL_BY_TIER[CodexTier.B] == ("gpt-5.6-terra", "high")
    assert MODEL_BY_TIER[CodexTier.C] == ("gpt-5.6-luna", "high")
    assert MODEL_BY_TIER[CodexTier.N] == ("", "")


def test_max_effort_is_never_automatic():
    """sol/max 는 xhigh 로도 결론이 안 날 때 사람이 1회만 — 자동 승급 금지."""
    assert all(effort != "max" for _, effort in MODEL_BY_TIER.values())


# ---------------------------------------------------------------- plan (모드 결합)


def test_verify_uses_path_tier_as_is():
    plan = plan_codex_collaboration(CodexMode.VERIFY, paths=("src/matcher.py",))
    assert (plan.tier, plan.model, plan.effort) == (CodexTier.B, "gpt-5.6-terra", "high")


def test_verify_no_call_tier_does_not_call():
    plan = plan_codex_collaboration(CodexMode.VERIFY, paths=("docs/x.md",))
    assert plan.tier is CodexTier.N
    assert plan.should_call is False
    assert plan.model == ""


def test_consult_floors_at_high_tier():
    """방향 의논은 판단 품질 우선 — 문서만 건드렸어도 terra 밑으로 안 내려간다."""
    plan = plan_codex_collaboration(CodexMode.CONSULT, task="이거 어떻게 갈까")
    assert plan.tier is CodexTier.A
    assert (plan.model, plan.effort) == ("gpt-5.6-sol", "high")
    assert plan.should_call is True


def test_consult_still_escalates_on_safety_boundary():
    plan = plan_codex_collaboration(CodexMode.CONSULT, task="보관판매 등록 방식 의논")
    assert (plan.tier, plan.effort) == (CodexTier.S, "xhigh")


def test_collaborate_floors_at_high_tier():
    plan = plan_codex_collaboration(CodexMode.COLLABORATE, task="이 난제 같이 풀자")
    assert plan.tier is CodexTier.A
    assert plan.should_call is True


def test_manual_tier_overrides_classification():
    plan = plan_codex_collaboration(CodexMode.VERIFY, paths=("docs/x.md",), tier=CodexTier.S)
    assert (plan.tier, plan.model, plan.effort) == (CodexTier.S, "gpt-5.6-sol", "xhigh")
    assert plan.should_call is True


def test_plan_records_a_reason():
    plan = plan_codex_collaboration(CodexMode.VERIFY, paths=("src/storage_sale/x.py",))
    assert plan.reason


def test_plan_never_spawns_a_process(monkeypatch):
    """순수 정책 — IO 0. 한도 소모 없이 등급만 미리 볼 수 있어야 한다."""
    import subprocess

    def boom(*args, **kwargs):  # pragma: no cover - 호출되면 테스트 실패
        raise AssertionError("gate must not spawn a process")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    plan_codex_collaboration(CodexMode.VERIFY, paths=("src/core/orchestrator.py",))


# ---------------------------------------------------------------- 한도 절약 회귀


def test_default_model_is_terra():
    """일상 검증이 sol 로 새면 plus 한도가 두 배로 탄다 — 기본은 terra 고정."""
    plan = plan_codex_collaboration(CodexMode.VERIFY, paths=("src/crawlers/nike.py",))
    assert plan.model == "gpt-5.6-terra"


def test_korean_sweep_request_routes_to_luna():
    plan = plan_codex_collaboration(CodexMode.VERIFY, task="소싱처 커버리지 전수 훑기")
    assert plan.model == "gpt-5.6-luna"
