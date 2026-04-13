"""로깅 설정."""

import logging
import sys
from pathlib import Path

from src.config import LOGS_DIR

_ROOT_CONFIGURED = False


def _configure_root_once(level: int = logging.INFO) -> None:
    """루트 로거 1회 구성. `logging.getLogger(__name__)` 계열(어댑터/런타임 등)이
    bot_pilot.log(stdout) 로 흘러가도록 콘솔 핸들러를 루트에 붙인다."""
    global _ROOT_CONFIGURED
    if _ROOT_CONFIGURED:
        return
    root = logging.getLogger()
    if root.level == logging.WARNING or root.level > level:
        root.setLevel(level)
    # 이미 StreamHandler 가 있으면 건드리지 않음 (테스트 런너와의 충돌 방지)
    has_stream = any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    if not has_stream:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
                "%H:%M:%S",
            )
        )
        root.addHandler(console)
    _ROOT_CONFIGURED = True


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """모듈별 로거 생성. 첫 호출 시 루트 콘솔 핸들러도 함께 부착."""
    _configure_root_once(level)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    # 콘솔 핸들러 (루트에도 있지만 기존 호환 위해 유지 — propagate=False 목적)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")
    )
    logger.addHandler(console)
    # 기존 동작과 동일하게 자기 핸들러만 쓰도록 상속 차단 (중복 출력 방지).
    logger.propagate = False

    # 파일 핸들러
    log_file = LOGS_DIR / f"{name.replace('.', '_')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)-8s %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)

    return logger
