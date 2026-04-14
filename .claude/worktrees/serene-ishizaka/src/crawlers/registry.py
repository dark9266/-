"""소싱처 크롤러 레지스트리.

각 크롤러 모듈에서 register()를 호출하여 자동 등록한다.
서킷브레이커: 연속 3회 실패 시 30분 비활성화.
"""

from datetime import datetime, timedelta

from src.utils.logging import setup_logger

logger = setup_logger("registry")

RETAIL_CRAWLERS: dict[str, dict] = {}

MAX_FAILURES = 3
DISABLE_MINUTES = 30


def register(key: str, crawler, label: str) -> None:
    """크롤러를 레지스트리에 등록."""
    RETAIL_CRAWLERS[key] = {
        "crawler": crawler,
        "label": label,
        "fail_count": 0,
        "disabled_until": None,
    }


def get_all() -> dict[str, dict]:
    """등록된 전체 크롤러 반환."""
    return RETAIL_CRAWLERS


def get_active() -> dict[str, dict]:
    """활성 크롤러만 반환."""
    return {k: v for k, v in RETAIL_CRAWLERS.items() if is_active(k)}


def get_crawler(key: str):
    """키로 크롤러 인스턴스 조회."""
    entry = RETAIL_CRAWLERS.get(key)
    return entry["crawler"] if entry else None


def get_label(key: str) -> str:
    """키로 표시 라벨 조회."""
    entry = RETAIL_CRAWLERS.get(key)
    return entry["label"] if entry else key


def is_active(key: str) -> bool:
    """크롤러가 활성 상태인지 확인."""
    entry = RETAIL_CRAWLERS.get(key)
    if not entry:
        return False
    disabled_until = entry.get("disabled_until")
    if disabled_until:
        if datetime.now() < disabled_until:
            return False
        # 비활성화 기간 종료 → 재활성화
        entry["disabled_until"] = None
        entry["fail_count"] = 0
        logger.info("%s 재활성화", entry["label"])
    return True


def record_failure(key: str) -> None:
    """실패 기록. 연속 3회 실패 시 30분 비활성화."""
    entry = RETAIL_CRAWLERS.get(key)
    if not entry:
        return
    entry["fail_count"] = entry.get("fail_count", 0) + 1
    if entry["fail_count"] >= MAX_FAILURES:
        entry["disabled_until"] = datetime.now() + timedelta(minutes=DISABLE_MINUTES)
        logger.warning(
            "%s 연속 %d회 실패 → %d분 비활성화",
            entry["label"], entry["fail_count"], DISABLE_MINUTES,
        )


def record_success(key: str) -> None:
    """성공 시 실패 카운터 초기화."""
    entry = RETAIL_CRAWLERS.get(key)
    if entry:
        entry["fail_count"] = 0
        entry["disabled_until"] = None


def get_status() -> dict[str, str]:
    """전체 소싱처 상태 요약."""
    status = {}
    for key, info in RETAIL_CRAWLERS.items():
        if is_active(key):
            status[key] = f"✅ {info['label']}"
        else:
            until = info.get("disabled_until")
            if until:
                status[key] = f"⏸️ {info['label']} (재시도: {until.strftime('%H:%M')})"
            else:
                status[key] = f"⏸️ {info['label']}"
    return status
