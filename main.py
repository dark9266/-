"""크림 리셀 수익 모니터링 봇 — 메인 진입점.

실행:
    source ~/kream-venv/bin/activate
    PYTHONPATH=. python main.py
"""

import asyncio
import atexit
import os
import sys

from src.config import settings
from src.discord_bot.bot import bot
from src.utils.logging import setup_logger

logger = setup_logger("main")

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


def main():
    if not settings.discord_token:
        print("❌ DISCORD_TOKEN이 설정되지 않았습니다.")
        print("   .env 파일에 DISCORD_TOKEN을 설정하세요.")
        print("   참고: .env.example")
        sys.exit(1)

    _acquire_pid_lock()
    logger.info("크림봇 시작")
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
