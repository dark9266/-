"""훅 가드 3종 순수 판정 테스트 — 중복 작업·방향 이탈·증거 없는 주장 차단.

마크다운(CLAUDE.md)이 못 막은 것을 기계가 막는다. 그 기계가 맞게 도는지 여기서 고정한다.
**오탐이 나면 사람이 훅을 꺼버린다** — 오탐 케이스를 실패 케이스만큼 촘촘히 둔다.
"""

import json
import sys
from pathlib import Path

_HOOKS = Path(__file__).resolve().parents[1] / ".claude" / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

import claim_evidence_guard as ceg  # noqa: E402
import direction_guard as dg  # noqa: E402
import kream_live_call_guard as klcg  # noqa: E402
import orchestration_gate as og  # noqa: E402
import task_intake_guard as tig  # noqa: E402


def _edit(path: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": path}}


def _write(path: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": path}}


def _bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def _never(_p: str) -> bool:
    return False


def _always(_p: str) -> bool:
    return True


# ═══════════════════════════════════════════════ direction_guard (방향 이탈)


def test_legacy_scanner_edit_is_blocked():
    """폐기 흐름(v2 역방향/Tier) — 리팩터·버그픽스·재활용 전부 금지."""
    for path in ("src/reverse_scanner.py", "src/scanner.py", "src/tier1_scanner.py",
                 "src/continuous_scanner.py"):
        d = dg.decide(_edit(path), exists=_always, env={})
        assert d.allow is False, path
        assert "폐기 흐름" in d.message


def test_tier2_monitor_is_not_blocked():
    """축 ② 보조 감시 — '역방향이니 끄자'는 이미 판정 완료된 예외다."""
    assert dg.decide(_edit("src/tier2_monitor.py"), exists=_always, env={}).allow is True


def test_legacy_edit_passes_with_explicit_override():
    d = dg.decide(_edit("src/scanner.py"), exists=_always, env={"KREAM_LEGACY_EDIT_GO": "1"})
    assert d.allow is True


def test_bash_rewrite_of_legacy_file_is_blocked():
    """Edit 대신 sed -i 로 우회하는 경로도 막는다."""
    d = dg.decide(_bash("sed -i 's/a/b/' src/tier1_scanner.py"), exists=_always, env={})
    assert d.allow is False


def test_bash_read_of_legacy_file_passes():
    assert dg.decide(_bash("grep -n foo src/scanner.py"), exists=_always, env={}).allow is True


def test_new_source_adapter_creation_is_blocked():
    d = dg.decide(_write("src/adapters/newshop_adapter.py"), exists=_never, env={})
    assert d.allow is False
    assert "22 소싱처 안정화" in d.message


def test_existing_adapter_edit_passes():
    """안정화 작업 자체가 기존 22곳을 고치는 일이다 — 막으면 훅이 꺼진다."""
    d = dg.decide(_edit("src/adapters/stussy_adapter.py"), exists=_always, env={})
    assert d.allow is True


def test_crawler_infra_creation_passes():
    for path in ("src/crawlers/registry.py", "src/crawlers/__init__.py"):
        assert dg.decide(_write(path), exists=_never, env={}).allow is True, path


def test_new_source_passes_with_explicit_override():
    d = dg.decide(_write("src/adapters/x_adapter.py"), exists=_never,
                  env={"KREAM_NEW_SOURCE_GO": "1"})
    assert d.allow is True


def test_unrelated_new_file_passes():
    assert dg.decide(_write("src/core/new_thing.py"), exists=_never, env={}).allow is True


def test_absolute_paths_are_normalized():
    root = str(dg.project_root())
    d = dg.decide(_edit(f"{root}/src/scanner.py"), exists=_always, env={})
    assert d.allow is False


# ═══════════════════════════════════════════════ task_intake_guard (중복·망각)


def test_model_number_token_extraction():
    assert "DZ5485-100" in tig.extract_tokens("DZ5485-100 이거 왜 안 잡혀?")


def test_source_name_token_extraction():
    toks = [t.lower() for t in tig.extract_tokens("무신사랑 29cm 매칭 확인해줘")]
    assert "무신사" in toks and "29cm" in toks


def test_korean_particle_is_stripped_into_a_stem():
    """'봇월이라' ≠ '봇월' — 조사가 붙으면 스킬/문서 매칭이 통째로 실패한다."""
    assert "봇월" in tig.extract_tokens("봇월이라 못 뚫었어")


def test_stopwords_are_not_tokens():
    assert "해줘" not in tig.extract_tokens("이거 해줘")


def test_forbidden_reverse_scanner_is_detected():
    hits = tig.forbidden_hits("역방향 스캐너 다시 살려볼까?")
    assert hits and "폐기 흐름" in hits[0]


def test_forbidden_target_shrink_is_detected():
    assert tig.forbidden_hits("hot 위주로만 돌리자")
    assert tig.forbidden_hits("인기 카테고리 먼저 타겟팅할까요")


def test_forbidden_new_source_is_detected():
    assert tig.forbidden_hits("새 소싱처 하나 추가하자")


def test_forbidden_overseas_store_is_detected():
    assert tig.forbidden_hits("미국 스토어도 붙일까")


def test_forbidden_local_db_profit_calc_is_detected():
    assert tig.forbidden_hits("로컬 DB로 수익 계산하면 빠르잖아")


def test_ordinary_request_triggers_no_forbidden_warning():
    """오탐이 잦으면 경고를 아무도 안 읽는다."""
    assert tig.forbidden_hits("무신사 어댑터 로그 좀 봐줘") == []
    assert tig.forbidden_hits("테스트 돌려줘") == []


# ═══════════════════════════════════════════════ claim_evidence_guard (증거)


def _use(tool_id: str, name: str, text: str) -> dict:
    return {"kind": "tool_use", "id": tool_id, "name": name, "text": text}


def _result(tool_id: str, text: str) -> dict:
    return {"kind": "tool_result", "id": tool_id, "text": text}


def test_success_claim_is_detected():
    assert ceg.claims_success("수정 완료했습니다") is True
    assert ceg.claims_success("봉합 완료") is True


def test_future_and_negated_forms_are_not_claims():
    for text in ("성공했는지 확인하겠습니다", "아직 고쳤다고 볼 수 없습니다",
                 "승인 없이 실행할 수 없습니다", "중단은 불가피합니다"):
        assert ceg.claims_success(text) is False, text
        assert ceg.claims_blocked(text) is False, text


def test_blocked_claim_needs_an_operational_noun():
    assert ceg.claims_blocked("adidas 는 403 이라 안 됩니다") is True
    # 개념 서술은 검증할 '시도'가 없다 — 막으면 오탐
    assert ceg.claims_blocked("훅은 판단을 못 합니다") is False


def test_works_claim_and_self_correction_exemption():
    assert ceg.claims_works("adidas 봇월 뚫었습니다") is True
    assert ceg.claims_works("adidas 뚫었다 했는데 틀렸다") is False


def test_metric_claim_is_detected():
    assert ceg.claims_metric("매칭 150건 나왔습니다") is True
    assert ceg.claims_metric("커버율 3.1% 입니다") is True


def test_cited_metric_is_not_a_claim():
    assert ceg.claims_metric("CLAUDE.md 기준 매칭 150건으로 적혀 있습니다") is False


def test_edit_tools_count_as_mutation():
    assert ceg.is_mutation(_use("1", "Edit", "{}")) is True
    assert ceg.is_mutation(_use("1", "Read", "{}")) is False


def test_bash_file_write_counts_as_mutation():
    ev = _use("1", "Bash", json.dumps({"command": "sed -i 's/a/b/' src/x.py"}))
    assert ceg.is_mutation(ev) is True
    ev2 = _use("2", "Bash", json.dumps({"command": "grep -n cp src/x.py"}))
    assert ceg.is_mutation(ev2) is False


def test_verification_before_the_change_is_not_evidence():
    events = [
        _use("a", "Bash", json.dumps({"command": "pytest -q"})),
        _result("a", "31 passed"),
        _use("b", "Edit", "{}"),
        _result("b", "ok"),
    ]
    assert ceg.has_causal_verification(events) is False


def test_successful_verification_after_the_change_is_evidence():
    events = [
        _use("b", "Edit", "{}"),
        _result("b", "ok"),
        _use("a", "Bash", json.dumps({"command": "pytest -q"})),
        _result("a", "31 passed"),
    ]
    assert ceg.has_causal_verification(events) is True


def test_failed_pytest_is_not_evidence():
    events = [
        _use("b", "Edit", "{}"), _result("b", "ok"),
        _use("a", "Bash", json.dumps({"command": "pytest -q"})),
        _result("a", "1 failed, 2 passed\nFAILED tests/x.py"),
    ]
    assert ceg.has_causal_verification(events) is False


def test_no_tests_ran_is_not_evidence():
    """exit 5 는 실패 패턴에 안 걸리지만 성공도 아니다 — 애매하면 증거 아님."""
    events = [
        _use("b", "Edit", "{}"), _result("b", "ok"),
        _use("a", "Bash", json.dumps({"command": "pytest -q tests/none.py"})),
        _result("a", "no tests ran in 0.01s"),
    ]
    assert ceg.has_causal_verification(events) is False


def test_verification_without_a_result_is_not_evidence():
    events = [
        _use("b", "Edit", "{}"), _result("b", "ok"),
        _use("a", "Bash", json.dumps({"command": "pytest -q"})),
    ]
    assert ceg.has_causal_verification(events) is False


def test_parallel_calls_are_paired_by_id():
    """결과 순서가 섞여도 다른 도구의 실패와 잘못 짝지으면 안 된다."""
    events = [
        _use("m", "Edit", "{}"), _result("m", "ok"),
        _use("v", "Bash", json.dumps({"command": "pytest -q"})),
        _use("x", "Bash", json.dumps({"command": "curl example.com"})),
        _result("x", "Error: refused"),
        _result("v", "31 passed"),
    ]
    assert ceg.has_causal_verification(events) is True


# ── evaluate 통합 ────────────────────────────────────────────────────────────


def test_success_claim_after_change_without_verification_is_blocked():
    events = [_use("b", "Edit", "{}"), _result("b", "ok")]
    block, reason = ceg.evaluate("수정 완료했습니다", events)
    assert block is True
    assert "검증 실행이 없다" in reason


def test_report_only_turn_with_completion_wording_passes():
    """오늘 한 일을 보고만 하는 턴 — 검증할 대상 자체가 없다. 막으면 영원히 못 끝낸다."""
    block, _ = ceg.evaluate("어제 수정 완료했습니다", [])
    assert block is False


def test_blocked_claim_without_an_attempt_is_blocked():
    block, reason = ceg.evaluate("adidas 는 403 이라 안 됩니다", [])
    assert block is True
    assert "실제로 시도한 실패 출력이 없다" in reason


def test_blocked_claim_with_failure_output_passes():
    events = [_result("x", "HTTP 403 Forbidden")]
    block, _ = ceg.evaluate("adidas 는 403 이라 안 됩니다", events)
    assert block is False


def test_works_claim_without_real_data_is_blocked():
    block, reason = ceg.evaluate("adidas 봇월 뚫었습니다", [_result("x", "status 200")])
    assert block is True
    assert "실 데이터" in reason


def test_works_claim_with_real_data_passes():
    events = [_result("x", '{"@type": "Product", "name": "..."}')]
    block, _ = ceg.evaluate("adidas 봇월 뚫었습니다", events)
    assert block is False


def test_metric_claim_without_any_read_is_blocked():
    block, reason = ceg.evaluate("매칭 150건 나왔습니다", [])
    assert block is True
    assert "라이브 관측 없이 수치 주장" in reason


def test_metric_claim_with_a_read_passes():
    events = [_result("q", "150|IH1325|adidas 매칭 결과 행이 여기 길게 나온다")]
    block, _ = ceg.evaluate("매칭 150건 나왔습니다", events)
    assert block is False


def test_empty_text_passes():
    assert ceg.evaluate("", [_use("b", "Edit", "{}")])[0] is False


def test_human_turn_detection_filters_tool_result_disguise():
    assert ceg.is_human_turn({"type": "user", "message": {"content": "안녕"}}) is True
    fake = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}
    assert ceg.is_human_turn(fake) is False


# ═══════════════════════════════════════════════ orchestration_gate (Fable)


def test_switch_off_passes_everything():
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "a.py"}, "prompt_id": "p"}
    assert og.decide(payload, "off", {}).allow is True


def test_subagents_are_exempt_from_the_limit():
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "a.py"},
               "prompt_id": "p", "agent_type": "kream-executor"}
    gate = {"prompt_id": "p", "files": [f"f{i}.py" for i in range(5)]}
    assert og.decide(payload, "on", gate).allow is True


def test_reaching_the_limit_blocks_and_directs_delegation():
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "new.py"}, "prompt_id": "p"}
    gate = {"prompt_id": "p", "files": [f"f{i}.py" for i in range(5)]}
    d = og.decide(payload, "on", gate)
    assert d.allow is False
    assert "kream-executor" in d.message


def test_re_editing_the_same_file_does_not_count():
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "f0.py"}, "prompt_id": "p"}
    gate = {"prompt_id": "p", "files": [f"f{i}.py" for i in range(5)]}
    assert og.decide(payload, "on", gate).allow is True


def test_non_code_files_are_outside_the_limit():
    payload = {"tool_name": "Write", "tool_input": {"file_path": "docs/x.md"}, "prompt_id": "p"}
    gate = {"prompt_id": "p", "files": [f"f{i}.py" for i in range(5)]}
    assert og.decide(payload, "on", gate).allow is True


# ---------------------------------------------------------------------------
# 실호출 가드 (kream_live_call_guard) — 2026-07-25 실계정 311콜 사고 대응
#
# 차단만큼 **오탐 방어**를 촘촘히 둔다: 오탐이 나면 사람이 훅을 꺼버리고,
# 꺼진 훅은 없는 훅이다.
# ---------------------------------------------------------------------------


def _blocks(cmd: str) -> bool:
    return not klcg.evaluate(cmd, go_enabled=False).allow


# --- 막아야 하는 것 ---------------------------------------------------------


def test_live_guard_blocks_batch_without_dry_run():
    assert _blocks("python scripts/bootstrap_cold_volumes_light.py")
    assert _blocks("python3 scripts/drain_collect_queue.py --limit 10")


def test_live_guard_blocks_legacy_bypass_script_and_env():
    assert _blocks("python scripts/bootstrap_cold_volumes.py")
    assert _blocks("KREAM_LEGACY_BOOTSTRAP_GO=1 python scripts/bootstrap_cold_volumes.py")


def test_live_guard_blocks_kream_probe_scripts():
    assert _blocks("python scripts/probe_kream_500_v3.py")
    assert _blocks("./scripts/probe_kream_api_ratelimit.py")


def test_live_guard_blocks_direct_kream_hitting_scripts():
    assert _blocks("PYTHONPATH=. python scripts/push_dump_full.py")
    assert _blocks("python scripts/manual_auto_scan_live.py")
    assert _blocks("python scripts/pilot_musinsa_push.py")


def test_live_guard_blocks_test_safety_net_bypass():
    """전역 안전망을 끄는 실행 자체를 막는다 — 사고 경로가 정확히 이것이었다."""
    assert _blocks("KREAM_TEST_ALLOW_NETWORK=1 python -m pytest tests/ -q")
    assert _blocks("KREAM_TEST_ALLOW_REAL_DB=1 pytest tests/ -q")


def test_live_guard_blocks_when_hidden_in_later_segment():
    assert _blocks("echo start && python scripts/bootstrap_cold_volumes.py")


# --- 막으면 안 되는 것 (오탐 방어) -------------------------------------------


def test_live_guard_allows_dry_run_batches():
    assert not _blocks("python scripts/bootstrap_cold_volumes_light.py --dry-run")
    assert not _blocks("python scripts/drain_collect_queue.py --dry-run")


def test_live_guard_allows_source_only_probes():
    """소싱처 probe 는 크림 캡과 무관한 읽기전용 — 막지 않는다."""
    for cmd in (
        "python scripts/probe_nike_2.py",
        "python scripts/probe_adidas.py",
        "python scripts/probe_abcmart_full.py",
        "python scripts/probe_kasina.py",
        "python scripts/test_sources_live.py",
    ):
        assert not _blocks(cmd), cmd


def test_live_guard_allows_plain_pytest_and_reads():
    assert not _blocks("python -m pytest tests/ -q")
    assert not _blocks("grep -n scripts/bootstrap_cold_volumes.py CLAUDE.md")
    assert not _blocks("cat scripts/manual_auto_scan_live.py | head -20")
    assert not _blocks("git log --oneline -5")


def test_live_guard_allows_everything_when_owner_go_is_set():
    """사장 GO 는 전부 통과 — 자물쇠가 아니라 속도방지턱."""
    assert klcg.evaluate("python scripts/bootstrap_cold_volumes.py", go_enabled=True).allow
    assert klcg.evaluate(
        "python scripts/drain_collect_queue.py", go_enabled=True
    ).allow


def test_live_guard_ignores_non_bash_payloads():
    """PreToolUse 는 Edit/Write 에도 걸린다 — Bash 가 아니면 판정 자체를 안 한다."""
    assert klcg.evaluate("", go_enabled=False).allow


def test_live_guard_message_tells_how_to_proceed():
    decision = klcg.evaluate("python scripts/bootstrap_cold_volumes_light.py", False)
    assert not decision.allow
    assert "dry-run" in decision.message


def test_live_guard_blocks_wrapper_and_shell_c_evasions():
    """래퍼(`nohup`/`env`/`timeout`)와 `bash -c "..."` 우회를 막는다.

    오케스트레이터 자체 우회 실험(2026-07-25)에서 실제로 통과했던 형태들이다 —
    가드는 "정면"만 막으면 가드가 아니다.
    """
    assert _blocks('bash -c "python scripts/bootstrap_cold_volumes.py"')
    assert _blocks('sh -c "python3 scripts/drain_collect_queue.py"')
    assert _blocks("nohup python scripts/push_dump_full.py &")
    assert _blocks("env FOO=1 python scripts/probe_kream_500.py")
    assert _blocks("timeout 300 python scripts/manual_auto_scan_live.py")
    assert _blocks("cd scripts && python bootstrap_cold_volumes.py")


def test_live_guard_wrapper_handling_does_not_overblock():
    """래퍼/셸 재귀가 정상 명령까지 삼키면 안 된다."""
    assert not _blocks('bash -c "python scripts/drain_collect_queue.py --dry-run"')
    assert not _blocks('bash -c "pytest tests/ -q"')
    assert not _blocks('bash -c "grep -rn scripts/push_dump_full.py docs/"')
    assert not _blocks("bash scripts/fable.sh status")
    assert not _blocks("nohup python scripts/probe_nike_2.py &")


def test_live_guard_blocks_codex_reported_evasions():
    """코덱스 적대검증(F5)이 짚은 우회 — 전부 실측으로 통과하던 것들이다."""
    # 주석으로 dry-run 을 위장해 라이브 실행 → 가장 위험한 방향의 오탐
    assert _blocks("python scripts/drain_collect_queue.py # --dry-run")
    # 전역 안전망 자체를 끄고 도는 pytest
    assert _blocks("python -m pytest tests/ --noconftest")
    # 파일 경로가 안 보이는 모듈 실행 형태
    assert _blocks("python -m scripts.drain_collect_queue")
    # 파이썬 레이어를 통째로 우회한 직접 호출
    assert _blocks("curl -s https://kream.co.kr/api/p/e/products/123")
    # export 로 안전망 해제
    assert _blocks("export KREAM_TEST_ALLOW_REAL_DB=1")


def test_live_guard_does_not_block_mere_mentions_of_bypass_env():
    """코덱스가 짚은 반대 방향 오탐 — 설명·문서 작성까지 막으면 안 된다."""
    assert not _blocks("echo KREAM_TEST_ALLOW_NETWORK=1")
    assert not _blocks('echo "주석 예시: python x.py # --dry-run"')
    assert not _blocks("grep -rn KREAM_TEST_ALLOW_NETWORK docs/")


def test_live_guard_allows_non_kream_curl_and_normal_pytest():
    assert not _blocks("curl -s https://www.nike.com/kr/")
    assert not _blocks("python -m pytest tests/ -q")


# 스크립트 파일명 — 문자열로 조립해서 이 파일 자체가 훅에 걸리지 않게 한다.
# (이 훅은 명령 원문을 스캔하므로, 테스트 코드에 실행 명령을 그대로 적으면
#  그 텍스트를 담은 Bash 명령이 차단된다 — 2026-07-25 실측)
_BOOT = "scripts/bootstrap_cold" + "_volumes_light.py"
_DRAIN = "scripts/drain_collect" + "_queue.py"


def test_live_guard_does_not_block_tests_named_after_scripts():
    """오탐 회귀 — 테스트 파일명이 스크립트 파일명으로 *끝난다*.

    2026-07-25 실측: 이 훅이 `pytest tests/test_bootstrap_cold_volumes_light.py` 를
    "배치 라이브 실행" 으로 막아 정상 작업(lint·테스트)을 중단시켰다.
    단순 endswith 판정의 대가 — 경로 경계로 좁혔다.
    """
    assert not _blocks(f"python -m pytest tests/test_{_BOOT.split('/')[-1]} -q")
    assert not _blocks(f"python -m pytest tests/test_{_DRAIN.split('/')[-1]} -q")
    assert not _blocks(f"ruff check --fix {_BOOT}")
    assert not _blocks("python -m pytest tests/ -q")


def test_live_guard_still_blocks_the_real_scripts_after_boundary_fix():
    """경계 판정으로 좁힌 뒤에도 진짜 실행은 그대로 막힌다."""
    assert _blocks(f"python {_BOOT}")
    assert _blocks(f"python ./{_DRAIN}")
    assert _blocks(f"cd scripts && python {_BOOT.split('/')[-1]}")
    assert not _blocks(f"python {_BOOT} --dry-run")
