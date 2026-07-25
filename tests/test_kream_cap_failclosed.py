"""크림 일일 캡 설정 fail-closed 고정 (2026-07-25).

## 왜
`.env` 에 `KREAM_DAILY_CAP=50000` 이 들어가 있었다 — CLAUDE.md·메모리 기준(10,000)의
5배다. 백그라운드는 소프트캡이 지배해 당장 영향은 없었지만 **최후 방어벽이 5배 열린
상태**였고, 오늘 실사고가 311콜에서 멈춘 건 캡이 아니라 타임아웃 덕이었다.

여기서 고정하는 계약 2개:
1. **캡은 낮추는 방향만 허용** — 절대 상한(10,000)을 넘는 설정은 크림 워커를 시작
   거부시킨다(fail-closed).
2. **이중 방어** — 설정이 잘못돼 있어도 계산에 쓰이는 값은 항상 안전 범위로 클램프.
   거부만 두면 가드를 우회한 경로가 50,000 으로 계산하고, 클램프만 두면 잘못된 설정을
   아무도 모른 채 계속 돈다.

## ⚠️ `importlib.reload` 를 쓰지 않는다 (2026-07-25 리뷰에서 실제 버그로 확인)
모듈 상수는 import 시점 1회 확정이라 처음엔 reload 로 검증했는데, reload 는 예외
**클래스 identity** 를 갈아치운다. 배치 스크립트들은 import 시점에
`from src.core.kream_budget import KreamBatchLockLost` 로 이름을 캡처해 두므로,
reload 이후 `acquire_background()` 가 던지는 새 클래스를 스크립트의 `except` 절이
못 잡는다 — 실측으로 무관한 테스트 6건이 **파일 실행 순서에 따라** 깨졌다
(`pytest tests/test_kream_cap_failclosed.py tests/test_bootstrap_cold_volumes_light.py`).
그래서 판정·클램프 로직을 순수 함수로 분리하고 여기서는 그 함수를 직접 검증한다.
"""

from __future__ import annotations

import pytest

import src.core.kream_budget as kb

# 오늘 실제로 `.env` 에 있던 값 — 회귀의 기준점
LIVE_INCIDENT_CAP = 50000


# ---------------------------------------------------------------------------
# 1. 절대 상한 — 캡은 올릴 수 없다
# ---------------------------------------------------------------------------


def test_ceiling_is_ten_thousand():
    assert kb.KREAM_HARD_CAP_CEILING == 10000


def test_cap_above_ceiling_is_rejected():
    """오늘 실제로 `.env` 에 있던 값(50000)이 판정에서 걸리는지."""
    error = kb.kream_budget_config_error(LIVE_INCIDENT_CAP, 5000)
    assert error is not None
    assert "50000" in error and "10000" in error


def test_cap_above_ceiling_is_still_clamped_for_arithmetic():
    """이중 방어 — 거부와 별개로, 계산값은 절대 상한을 넘지 않는다."""
    budget, _reserve = kb.clamp_budget_config(LIVE_INCIDENT_CAP, 5000)
    assert budget == 10000, "잘못된 설정이 계산에까지 새면 안 된다"


def test_cap_at_ceiling_is_accepted():
    assert kb.kream_budget_config_error(10000, 5000) is None
    assert kb.clamp_budget_config(10000, 5000) == (10000, 5000)


def test_cap_below_ceiling_is_accepted():
    """낮추는 방향은 항상 허용 — 더 보수적인 설정을 막을 이유가 없다."""
    assert kb.kream_budget_config_error(2000, 500) is None
    assert kb.clamp_budget_config(2000, 500) == (2000, 500)


# ---------------------------------------------------------------------------
# 2. 파싱 — 누락과 오타를 구분한다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 10000),  # 미설정 → 기본값
        ("", 10000),  # 빈 값 → 미설정과 동일 취급
        ("   ", 10000),  # 공백만 → 미설정과 동일 취급
        ("10000", 10000),
        (" 2000 ", 2000),  # 앞뒤 공백 허용
        ("abc", None),  # 오타 → 설정 오류
        ("1e5", None),  # 지수 표기 미지원 → 설정 오류
        ("10_000원", None),
        ("-1", None),
        ("0", None),
    ],
)
def test_env_parsing_distinguishes_missing_from_typo(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("KREAM_DAILY_CAP", raising=False)
    else:
        monkeypatch.setenv("KREAM_DAILY_CAP", raw)
    assert kb._parse_positive_int("KREAM_DAILY_CAP", 10000) == expected


def test_unparseable_cap_is_rejected_with_raw_value_in_message(monkeypatch):
    """오타를 로그만으로 진단할 수 있어야 한다 — 원문이 메시지에 실린다."""
    monkeypatch.setenv("KREAM_DAILY_CAP", "십만")
    error = kb.kream_budget_config_error(None, 5000)
    assert error is not None and "십만" in error


def test_reserve_not_below_cap_is_rejected():
    """예약분이 캡 이상이면 백그라운드 허용치가 영구 0 — 조용히 멈추느니 거부."""
    error = kb.kream_budget_config_error(1000, 1000)
    assert error is not None and "1000" in error


@pytest.mark.parametrize(
    ("raw_budget", "raw_reserve"),
    [(1000, 99999), (1000, None), (1000, 0), (1, None), (1, 5)],
)
def test_reserve_is_always_below_cap_after_clamp(raw_budget, raw_reserve):
    """클램프 결과는 어떤 입력에서도 0 <= reserve < budget 을 만족한다."""
    budget, reserve = kb.clamp_budget_config(raw_budget, raw_reserve)
    assert 0 <= reserve < budget, f"raw({raw_budget},{raw_reserve}) -> ({budget},{reserve})"


# ---------------------------------------------------------------------------
# 3. 이 프로세스의 유효값 — "어떤 설정으로 측정한 창인지" 증명용
# ---------------------------------------------------------------------------


def test_effective_config_reports_process_values():
    cfg = kb.effective_budget_config()
    assert cfg["cap_ceiling"] == 10000
    assert isinstance(cfg["pid"], int)
    assert cfg["daily_cap"] <= kb.KREAM_HARD_CAP_CEILING
    assert 0 <= cfg["live_reserve"] < cfg["daily_cap"]


def test_current_process_config_is_safe():
    """지금 이 프로세스 설정이 안전 범위 안 — 아니면 `.env` 가 다시 열린 것이다."""
    assert kb.kream_budget_config_error() is None
    kb.assert_kream_config_safe()  # 예외 없음


# ---------------------------------------------------------------------------
# 4. 워커 시작 관문 배선 — 크림 세션 생성이 실제로 막히는가
# ---------------------------------------------------------------------------


async def test_kream_session_refuses_to_start_on_unsafe_config(monkeypatch):
    """설정이 불안전하면 크림 세션 자체가 안 열린다(워커 fail-closed).

    전역 안전망(`KREAM_IN_TEST`)이 먼저 걸리지 않게 해제한 뒤, 설정 거부가
    독립적으로 작동하는지 본다 — 네트워크는 어차피 conftest 가 막는다.
    """
    from src.crawlers.kream import KreamCrawler

    monkeypatch.delenv("KREAM_IN_TEST", raising=False)
    monkeypatch.setattr(kb, "_RAW_BUDGET", LIVE_INCIDENT_CAP)

    crawler = KreamCrawler()
    with pytest.raises(kb.KreamConfigUnsafe):
        await crawler._get_session()


async def test_kream_session_proceeds_when_config_safe(monkeypatch):
    """정상 설정에서는 설정 관문이 막지 않는다(과잉 차단 방어).

    막히더라도 그건 전역 안전망(테스트 프로세스)이지 설정 관문이 아니어야 한다.
    """
    from src.crawlers.kream import KreamCrawler, KreamLiveCallUnderTest

    crawler = KreamCrawler()
    with pytest.raises(KreamLiveCallUnderTest):
        await crawler._get_session()
