"""Phase B-0 — 인증 세션 관리. dry-run 은 MockSession, 실 호출은 Stage B-1 에서 RealSession 추가."""
from datetime import datetime


class MockSession:
    """Phase B-0 dry-run 용. 실 인증 X. Stage B-1 진입 시 RealSession 추가."""

    def get_headers(self) -> dict[str, str]:
        return {
            "X-KREAM-DEVICE-ID": "web;mock-did",
            "X-KREAM-WEB-REQUEST-SECRET": "kream-djscjsghdkd",
            "X-KREAM-API-VERSION": "57",
            "X-KREAM-CLIENT-DATETIME": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "X-KREAM-WEB-BUILD-VERSION": "26.5.6",
        }
