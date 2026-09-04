---
paths:
  - "src/**/*.py"
---

# 핵심 모듈 지도

> 상시 로드 아님 — `src/` 파일을 열 때만 들어온다.
> 소싱처 어댑터는 `.claude/rules/adapters.md`, 크림 API 는 `.claude/rules/kream-api.md`.

## 파이프라인 (푸시 단일 트랙)
```
22 소싱처 어댑터 → 카탈로그 덤프
  → src/matcher.py        (크림 DB 로컬 교집합)
  → kream_collect_queue   (크림 후보)
  → kream_delta_watcher + orchestrator (sell_now 조회)
  → src/profit_calculator.py
  → Discord 알림
```

## 매칭 · 수익
- `src/matcher.py` — 모델번호 정규화 + **exact match. fuzzy 없음.** 콜라보 키워드 감지 + 서브타입(PRM/QS/SE 등) 필터
- `src/profit_calculator.py` — 크림 수수료 차감 후 수익/ROI/시그널 판정. **변경 시 `profit-analyzer` 의무**
- `src/scan_cache.py` — 모델번호 중복 스캔 방지 (일반 24h, 역방향 2h, 수익 6h TTL)

## 인프라
- `src/scheduler.py` — discord.ext.tasks 루프 6개 (Tier1/Tier2/일일리포트/수집/갱신/급등)
- `src/watchlist.py` — `watchlist.json` 기반 모니터링 대상
- `src/models/database.py` — Async SQLite (aiosqlite)
- `src/config.py` — Pydantic BaseSettings, `.env` 로드. **변경 시 Codex S 등급**
- `src/discord_bot/bot.py` — 슬래시 명령, embed 알림, 6시간 중복 알림 방지 (시그널 업그레이드/수익 20%↑ 시 재전송)
- `src/ops/codex_gate.py` — Codex 협업 등급 자동 판정
- `src/coupon_store.py` — Chrome 확장 catch row 저장소. 키 = (sourcing, native_id, color_code, size_code). 색·사이즈 일치 검증은 호출자(`profit_calculator`) 담당. 페이지 우선순위 checkout > pdp

## 🗑 폐기 흐름 — 손대지 말 것
`src/reverse_scanner.py` · `src/scanner.py` · `src/tier1_scanner.py` · `src/continuous_scanner.py`
— v2 시절 역방향/Tier 구조. 현행 푸시 트랙과 무관. **리팩터링·버그픽스·재활용 제안 금지.**
`direction_guard` 훅이 수정을 물리 차단한다 (해제 = `KREAM_LEGACY_EDIT_GO=1`).

hot/warm/cold 큐 컬럼 · `next_scan_at` · `scan_priority` 도 같은 계열이다.
상세는 메모리 `project_history_archive.md` 참조.

**예외**: `src/tier2_monitor.py` 는 역방향 hot 폴링이지만 **축 ② 보조 감시로 유지 판정 완료**.
"역방향이니 끄자" 제안 금지.

## DB 스키마 변경 시
마이그레이션 전후 데이터 무결성 확인. Codex A 등급 (`src/models/` 스키마·마이그레이션).

## 코드 스타일
- Ruff: line-length 100, rules E/F/I/N/W, target Python 3.13
- pytest: `asyncio_mode = "auto"`
- **모든 I/O async** (aiosqlite, aiohttp, httpx)
- 한국어 = 사용자 메시지·Discord·문서 / 영어 = 코드 식별자
