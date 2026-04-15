"""Canary regression test — pytest 진입점.

scripts/run_canary.py 를 래핑해서 pytest 수집에 포함.
실제 검증 로직은 run_canary.py 에 있음.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_canary_matches_intact():
    """소싱처↔크림 매칭 정답셋 20페어가 전부 살아있는지 확인."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_canary.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        f"Canary 매칭이 깨졌습니다.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
