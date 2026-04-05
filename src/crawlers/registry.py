"""소싱처 크롤러 레지스트리.

각 크롤러 모듈에서 register()를 호출하여 자동 등록한다.
"""

RETAIL_CRAWLERS: dict[str, dict] = {}


def register(key: str, crawler, label: str) -> None:
    """크롤러를 레지스트리에 등록."""
    RETAIL_CRAWLERS[key] = {"crawler": crawler, "label": label}


def get_all() -> dict[str, dict]:
    """등록된 전체 크롤러 반환."""
    return RETAIL_CRAWLERS


def get_crawler(key: str):
    """키로 크롤러 인스턴스 조회."""
    entry = RETAIL_CRAWLERS.get(key)
    return entry["crawler"] if entry else None


def get_label(key: str) -> str:
    """키로 표시 라벨 조회."""
    entry = RETAIL_CRAWLERS.get(key)
    return entry["label"] if entry else key
