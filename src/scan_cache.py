"""스캔 캐시 — 모델번호 기반 중복 스캔 방지.

24시간 이내 스캔한 모델번호는 자동 스킵.
수익 발생 상품은 6시간 TTL (시세 변동 재확인).
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from src.utils.logging import setup_logger

logger = setup_logger("scan_cache")

CACHE_PATH = Path("data/scan_cache.json")
DEFAULT_TTL = 24  # 시간
PROFIT_TTL = 6  # 수익 발생 시 TTL


class ScanCache:
    """JSON 기반 스캔 캐시."""

    def __init__(self, path: Path = CACHE_PATH):
        self._path = path
        self._cache: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._cache = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2),
        )

    def cleanup_expired(self) -> int:
        """만료 항목 제거. 제거 건수 반환."""
        now = datetime.now()
        expired = []
        for key, entry in self._cache.items():
            try:
                scanned_at = datetime.fromisoformat(entry["scanned_at"])
                ttl = timedelta(hours=entry.get("ttl_hours", DEFAULT_TTL))
                if now - scanned_at >= ttl:
                    expired.append(key)
            except (KeyError, ValueError):
                expired.append(key)
        for key in expired:
            del self._cache[key]
        if expired:
            self._save()
            logger.info("캐시 만료 정리: %d건 제거", len(expired))
        return len(expired)

    def should_skip(self, model_number: str) -> bool:
        """캐시에 있고 TTL 내이면 True (스킵 대상)."""
        entry = self._cache.get(model_number)
        if not entry:
            return False
        try:
            scanned_at = datetime.fromisoformat(entry["scanned_at"])
            ttl = timedelta(hours=entry.get("ttl_hours", DEFAULT_TTL))
        except (KeyError, ValueError):
            del self._cache[model_number]
            return False
        if datetime.now() - scanned_at >= ttl:
            del self._cache[model_number]
            return False
        return True

    def record(self, model_number: str, profitable: bool = False, source: str = ""):
        """스캔 결과 기록."""
        self._cache[model_number] = {
            "scanned_at": datetime.now().isoformat(),
            "ttl_hours": PROFIT_TTL if profitable else DEFAULT_TTL,
            "profitable": profitable,
            "source": source,
        }
        self._save()

    @property
    def size(self) -> int:
        return len(self._cache)

    def get_stats(self) -> dict:
        """캐시 통계 반환."""
        total = len(self._cache)
        profitable = sum(1 for e in self._cache.values() if e.get("profitable"))
        return {"total": total, "profitable": profitable}
