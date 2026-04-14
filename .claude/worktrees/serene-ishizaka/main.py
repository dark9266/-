"""크림 리셀 수익 모니터링 봇 — 메인 진입점.

실행:
    source ~/kream-venv/bin/activate
    PYTHONPATH=. python main.py
"""

import asyncio
import sys

from src.config import settings
from src.discord_bot.bot import bot
from src.utils.logging import setup_logger

logger = setup_logger("main")


def main():
    if not settings.discord_token:
        print("❌ DISCORD_TOKEN이 설정되지 않았습니다.")
        print("   .env 파일에 DISCORD_TOKEN을 설정하세요.")
        print("   참고: .env.example")
        sys.exit(1)

    logger.info("크림봇 시작")
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
