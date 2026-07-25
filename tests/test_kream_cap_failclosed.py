"""크림 일일 캡 설정 fail-closed 고정 (2026-07-25).

## 왜
`.env` 에 `KREAM_DAILY_CAP=50000` 이 들어가 있었다 — CLAUDE.md·메모리 기준(10,000)의
5배다. 백그라운드는 소프트캡이 지배해 당장 영향은 없었지만 **최후 방어벽이 5배 열린
상태**였고, 오늘 실사고가 311콜로 끝난 건 운이었다.

여기서 고정하는 계약 2개:
1. **캡은 낮추는 방향만 허용** — 절대 상한(10,000)을 넘는 설정은 크림 워커를 시작
   거부시킨다(fail-closed).
2. **이중 방어** — 설정이 잘못돼 있어도 계산에 쓰이는 값은 항상 안전 범위로 클램프.
   거부만 두면 가드를 우회한 경로가 50,000 으로 계산하고, 클램프만 두면 잘못된 설정을
   아무도 모른 채 계속 돈다.

⚠️ 상수는 **모듈 임포트 시점에 1회** 확정된다(실행 중 `.env` 변경은 반영 안 됨 —
2026-07-25 실측: price_refresher 를 5/1 에 껐는데 5/5 까지 호출이 나갔다).
그래서 여기서는 환경변수를 바꾼 뒤 모듈을 **재임포트**해 유효값을 검증한다.
"""

from __future__ import annotations

import importlib

import pytest

import src.core.kream_budget as kb


def _reload_with_env(monkeypatch, **env):
    """환경변수를 세우고 예산 모듈을 재임포트 — '이 프로세스의 유효값' 재현."""
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return importlib.reload(kb)


@pytest.fixture(autouse=True)
def _restore_module():
    """다른 테스트가 보는 모듈 상태를 원복 — 전역 재임포트라 오염 위험이 크다."""
    yield
    importlib.reload(kb)


# ---------------------------------------------------------------------------
# 1. 절대 상한 — 캡은 올릴 수 없다
# ---------------------------------------------------------------------------


def test_ceiling_is_ten_thousand():
    assert kb.KREAM_HARD_CAP_CEILING == 10000


def test_cap_above_ceiling_is_rejected(monkeypatch):
    """오늘 실제로 `.env` 에 있던 값(50000)이 시작을 막는지."""
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP="50000")

    error = m.kream_budget_config_error()
    assert error is not None and "50000" in error

    with pytest.raises(m.KreamConfigUnsafe):
        m.assert_kream_config_safe()


def test_cap_above_ceiling_is_still_clamped_for_arithmetic(monkeypatch):
    """이중 방어 — 시작을 거부해도, 계산값은 절대 상한을 넘지 않는다."""
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP="50000")
    assert m.BUDGET == 10000, "잘못된 설정이 계산에까지 새면 안 된다"


def test_cap_at_ceiling_is_accepted(monkeypatch):
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP="10000", KREAM_BG_LIVE_RESERVE="5000")
    assert m.kream_budget_config_error() is None
    m.assert_kream_config_safe()  # 예외 없음
    assert (m.BUDGET, m.BG_LIVE_RESERVE) == (10000, 5000)


def test_cap_below_ceiling_is_accepted(monkeypatch):
    """낮추는 방향은 항상 허용 — 더 보수적인 설정을 막을 이유가 없다."""
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP="2000", KREAM_BG_LIVE_RESERVE="500")
    assert m.kream_budget_config_error() is None
    assert m.BUDGET == 2000


# ---------------------------------------------------------------------------
# 2. 파싱 불가·비정상 값
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "  ", "abc", "10_000원", "-1", "0", "1e5"])
def test_unparseable_or_nonpositive_cap_is_rejected(monkeypatch, bad):
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP=bad)
    if bad.strip() == "":
        # 빈 값은 기본값(10000)으로 취급 — 설정 누락과 오타를 구분한다
        assert m.kream_budget_config_error() is None
        return
    assert m.kream_budget_config_error() is not None
    with pytest.raises(m.KreamConfigUnsafe):
        m.assert_kream_config_safe()


def test_reserve_not_below_cap_is_rejected(monkeypatch):
    """예약분이 캡 이상이면 백그라운드 허용치가 영구 0 — 조용히 멈추느니 거부."""
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP="1000", KREAM_BG_LIVE_RESERVE="1000")
    assert m.kream_budget_config_error() is not None
    with pytest.raises(m.KreamConfigUnsafe):
        m.assert_kream_config_safe()


def test_reserve_is_clamped_when_misconfigured(monkeypatch):
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP="1000", KREAM_BG_LIVE_RESERVE="99999")
    assert m.BG_LIVE_RESERVE < m.BUDGET, "예약분이 캡 이상으로 계산되면 안 된다"


# ---------------------------------------------------------------------------
# 3. 유효설정 보고 — "어떤 설정으로 측정한 창인지" 증명용
# ---------------------------------------------------------------------------


def test_effective_config_reports_process_values(monkeypatch):
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP="10000", KREAM_BG_LIVE_RESERVE="5000")
    cfg = m.effective_budget_config()
    assert cfg["daily_cap"] == 10000
    assert cfg["live_reserve"] == 5000
    assert cfg["cap_ceiling"] == 10000
    assert cfg["config_error"] is None
    assert isinstance(cfg["pid"], int)


def test_effective_config_surfaces_the_error(monkeypatch):
    m = _reload_with_env(monkeypatch, KREAM_DAILY_CAP="50000")
    assert m.effective_budget_config()["config_error"] is not None


# ---------------------------------------------------------------------------
# 4. 워커 시작 관문 배선 — 크림 세션 생성이 실제로 막히는가
# ---------------------------------------------------------------------------


async def test_kream_session_refuses_to_start_on_unsafe_config(monkeypatch):
    """설정이 불안전하면 크림 세션 자체가 안 열린다(워커 fail-closed).

    전역 안전망(`KREAM_IN_TEST`)이 먼저 걸리지 않도록 해제한 뒤, 설정 거부가
    독립적으로 작동하는지 본다 — 네트워크는 어차피 conftest 가 막는다.
    """
    from src.crawlers.kream import KreamCrawler

    monkeypatch.delenv("KREAM_IN_TEST", raising=False)
    monkeypatch.setattr(kb, "_RAW_BUDGET", 50000, raising=False)

    crawler = KreamCrawler()
    with pytest.raises(kb.KreamConfigUnsafe):
        await crawler._get_session()
