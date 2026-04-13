"""크림 리셀 수익 모니터링 봇 — 메인 진입점.

실행:
    source ~/kream-venv/bin/activate
    PYTHONPATH=. python main.py
"""

import atexit
import os
import sys

from src.config import settings
from src.core.runtime import V3Runtime, _safe_start_v3
from src.discord_bot.bot import bot
from src.utils.logging import setup_logger

logger = setup_logger("main")

# v3 런타임 전역 핸들 — 기본 OFF. 기동 실패해도 v2 는 계속 돌아야 한다.
_v3_runtime: V3Runtime | None = None

PID_FILE = "data/kreambot.pid"


def _acquire_pid_lock() -> None:
    """PID 파일로 중복 실행 방지."""
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            old_pid = int(f.read().strip())
        try:
            os.kill(old_pid, 0)
            print(f"❌ 이미 실행 중 (PID {old_pid}). 중복 실행 차단.")
            sys.exit(1)
        except OSError:
            pass  # 프로세스 없음 — stale PID

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(PID_FILE) and os.unlink(PID_FILE))


def _register_v3_runtime_hook() -> None:
    """봇 setup_hook 타이밍에 v3 런타임 기동을 등록 (기본 OFF 면 no-op).

    기존 bot 객체에 event listener 로 on_ready 한 번 발생 시 v3 기동.
    기동 실패는 WARN 후 흡수 → v2 는 계속 동작한다.
    기본값 `V3_RUNTIME_ENABLED=false` 이면 `start()` 가 즉시 return.
    """
    global _v3_runtime

    if not settings.v3_runtime_enabled:
        logger.info("[v3] V3_RUNTIME_ENABLED=false — v2 단독 운영")
        return

    # 크림 실클라이언트 → V3Runtime candidate 단계 snapshot_fn + delta watcher 배선
    # (실패해도 v3 기동 자체는 실패 안 하게 try/except — fallback: hot_watcher + candidate drop)
    snapshot_fn = None
    delta_client = None
    try:
        from src.crawlers.kream import KreamCrawler
        from src.crawlers.kream_delta_client import (
            KreamDeltaClient,
            build_snapshot_fn,
        )

        _kream_crawler = KreamCrawler()
        delta_client = KreamDeltaClient(crawler=_kream_crawler)
        snapshot_fn = build_snapshot_fn(delta_client)
        logger.info(
            "[v3] KreamDeltaClient 배선 완료 — candidate snapshot + delta watcher 활성"
        )
    except Exception:
        logger.warning("[v3] KreamDeltaClient 배선 실패 — hot_watcher fallback", exc_info=True)

    _v3_runtime = V3Runtime(
        db_path=settings.db_path,
        enabled=settings.v3_runtime_enabled,
        musinsa_interval_sec=settings.v3_musinsa_interval_sec,
        adapter_interval_sec=settings.v3_adapter_interval_sec,
        adapter_stagger_sec=settings.v3_adapter_stagger_sec,
        hot_poll_interval_sec=settings.v3_hot_poll_interval_sec,
        throttle_rate_per_min=settings.v3_throttle_rate_per_min,
        throttle_burst=settings.v3_throttle_burst,
        alert_log_path=settings.v3_alert_log_path,
        kream_snapshot_fn=snapshot_fn,
        kream_delta_client=delta_client,
    )

    _already_started = {"flag": False}

    @bot.listen("on_ready")
    async def _on_ready_start_v3() -> None:
        if _already_started["flag"]:
            return
        _already_started["flag"] = True
        ok = await _safe_start_v3(_v3_runtime)  # type: ignore[arg-type]
        if ok:
            logger.info("[v3] 런타임 기동 성공 (병행 운영)")
        else:
            logger.warning("[v3] 런타임 기동 실패 — v2 단독 운영 지속")


def main():
    if not settings.discord_token:
        print("❌ DISCORD_TOKEN이 설정되지 않았습니다.")
        print("   .env 파일에 DISCORD_TOKEN을 설정하세요.")
        print("   참고: .env.example")
        sys.exit(1)

    _acquire_pid_lock()
    logger.info("크림봇 시작")
    try:
        _register_v3_runtime_hook()
    except Exception:
        logger.warning("[v3] 런타임 hook 등록 실패 — v2 단독 운영 지속", exc_info=True)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
