"""로깅 설정."""

import logging
import sys
from pathlib import Path

from src.config import LOGS_DIR


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """모듈별 로거 생성."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    # 콘솔 핸들러
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")
    )
    logger.addHandler(console)

    # 파일 핸들러
    log_file = LOGS_DIR / f"{name.replace('.', '_')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)-8s %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)

    return logger
