크림봇 현재 상태 요약 대시보드.

아래 정보를 수집하여 한눈에 보이는 요약을 출력해주세요:

## 수집할 정보

1. **크림 DB 현황** (SQLite 쿼리):
   - `SELECT COUNT(*) FROM kream_products` — 총 상품 수
   - `SELECT COUNT(*) FROM kream_products WHERE refresh_tier = 'hot'` — hot 상품 수
   - `SELECT COUNT(*) FROM kream_products WHERE refresh_tier = 'cold'` — cold 상품 수
   - `SELECT MIN(updated_at), MAX(updated_at) FROM kream_products` — 갱신 시간 범위
   - `SELECT COUNT(*) FROM kream_price_history` — 가격 이력 수
   - `SELECT COUNT(*) FROM kream_volume_snapshots` — 거래량 스냅샷 수

2. **워치리스트** (`data/watchlist.json`):
   - 총 항목 수, 상위 5개 (gap 기준)

3. **최근 알림** (SQLite):
   - `SELECT * FROM alert_history ORDER BY created_at DESC LIMIT 5`

4. **스캔 캐시** (`data/scan_cache.json`):
   - 총 캐시 항목 수, 만료 항목 수

5. **Git 상태**:
   - `git log --oneline -5` — 최근 커밋
   - `git status --short` — 미커밋 변경

## 출력 형식

DB 경로: `data/kream_bot.db`
python3으로 sqlite3 모듈을 사용하여 쿼리 실행.
결과를 마크다운 표로 깔끔하게 정리.
