"""크림 리셀 수익 모니터링 봇 — 메인 진입점.

실행:
    source ~/kream-venv/bin/activate
    PYTHONPATH=. python main.py
"""

import asyncio
import atexit
import os
import sys

import discord

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
    if settings.v3_kream_dry_run:
        logger.warning(
            "[v3] V3_KREAM_DRY_RUN=true — 크림 snapshot_fn/delta_client 미배선. "
            "어댑터 덤프·매칭·이벤트버스만 검증 (크림 호출 0건)"
        )
    else:
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

    # dry-run: 크림 호출 0건이므로 throttle/recover cap 불필요 — 파이프라인 수용력 확보
    if settings.v3_kream_dry_run:
        _throttle_rate = 600.0
        _throttle_burst = 1000
        _recover_cap = 20000
        logger.info(
            "[v3] dry-run throttle override: rate=%s/min burst=%s recover_cap=%s",
            _throttle_rate,
            _throttle_burst,
            _recover_cap,
        )
    else:
        _throttle_rate = settings.v3_throttle_rate_per_min
        _throttle_burst = settings.v3_throttle_burst
        _recover_cap = 50

    # v3 수익 알림 → 기존 CHANNEL_PROFIT_ALERT 채널로 직접 send.
    # 새 webhook URL 필요 없음 — 기존 discord.py bot 핸들을 DI 로 재사용.
    async def _discord_channel_send(embed_dict: dict) -> None:
        channel_id = settings.channel_profit_alert
        if not channel_id:
            return
        channel = bot.get_channel(channel_id)
        if channel is None:
            logger.warning("[v3] profit 채널 미발견 id=%s — 알림 drop", channel_id)
            return
        try:
            await channel.send(embed=discord.Embed.from_dict(embed_dict))
        except Exception:
            logger.exception("[v3] profit 채널 send 실패")

    _v3_runtime = V3Runtime(
        db_path=settings.db_path,
        enabled=settings.v3_runtime_enabled,
        musinsa_interval_sec=settings.v3_musinsa_interval_sec,
        adapter_interval_sec=settings.v3_adapter_interval_sec,
        adapter_stagger_sec=settings.v3_adapter_stagger_sec,
        hot_poll_interval_sec=settings.v3_hot_poll_interval_sec,
        throttle_rate_per_min=_throttle_rate,
        throttle_burst=_throttle_burst,
        recover_candidate_cap=_recover_cap,
        alert_log_path=settings.v3_alert_log_path,
        kream_snapshot_fn=snapshot_fn,
        kream_delta_client=delta_client,
        discord_channel_send=_discord_channel_send,
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


async def _main_async() -> None:
    """봇 이벤트 루프 — graceful shutdown 보장 (2026-04-18 incident 대응).

    bot.run()은 SIGINT 시 내부 cleanup만 돌고 `KreamBot.close()`를 안 부른다.
    `async with bot:` 의 __aexit__가 close()를 보장 → WAL checkpoint flush.
    """
    async with bot:
        await bot.start(settings.discord_token)


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
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        logger.info("SIGINT 수신 — graceful shutdown 진행")


if __name__ == "__main__":
    main()
