"""CDP 연결 테스트 스크립트.

실행 방법:
    python -m scripts.test_cdp_connection

이 스크립트는:
1. Windows Chrome을 디버그 모드로 실행
2. CDP로 연결
3. 크림 로그인 상태 확인
4. 간단한 페이지 접속 테스트
"""

import asyncio
import sys
from pathlib import Path

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.crawlers.chrome_cdp import cdp_manager
from src.utils.logging import setup_logger

logger = setup_logger("test_cdp")


async def main():
    print("=" * 50)
    print("Chrome CDP 연결 테스트")
    print("=" * 50)

    try:
        # 1. Chrome 시작 및 CDP 연결
        print("\n[1/4] Chrome 디버그 모드 시작...")
        ctx = await cdp_manager.connect()
        print(f"  ✓ 연결 성공! 컨텍스트: {len(cdp_manager._browser.contexts)}개")

        # 2. 페이지 열기
        print("\n[2/4] 크림 메인 페이지 접속...")
        page = await cdp_manager.get_page("https://kream.co.kr")
        title = await page.title()
        print(f"  ✓ 페이지 제목: {title}")

        # 3. 로그인 확인
        print("\n[3/4] 크림 로그인 상태 확인...")
        is_logged_in = await cdp_manager.check_login_status("kream")
        if is_logged_in:
            print("  ✓ 로그인 상태: 로그인됨")
        else:
            print("  ✗ 로그인 상태: 로그아웃")
            print("    → 디버그 Chrome에서 수동으로 크림에 로그인하세요.")
            print(f"    → Chrome이 포트 {cdp_manager._chrome_process or 9222}에서 실행 중입니다.")
            print("    → 로그인 후 이 스크립트를 다시 실행하면 세션이 유지됩니다.")

        # 4. 탭 정리
        print("\n[4/4] 탭 정리...")
        await cdp_manager.close_extra_tabs(keep=1)
        print("  ✓ 완료")

    except Exception as e:
        print(f"\n✗ 오류 발생: {e}")
        logger.exception("CDP 테스트 실패")
    finally:
        print("\n연결 해제 중...")
        await cdp_manager.disconnect()
        print("완료!")


if __name__ == "__main__":
    asyncio.run(main())
