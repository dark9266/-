#!/bin/bash
# Stage 4 (24h) 완주 자동 판정 — 2026-04-23 11:35 KST 실행 예정.
# 3단 ladder 최종 gate: 2h → 6h → 24h.
set -u
cd /mnt/c/Users/USER/Desktop/크림봇

BOT_START_TS=1776825330  # 2026-04-22 11:35:30 KST (봇 재기동 시각)
WEBHOOK=$(grep -E '^DISCORD_NOTIFY_WEBHOOK=' .env | cut -d= -f2- | tr -d '"\r\n')

if [ -z "$WEBHOOK" ]; then
  echo "[ERROR] DISCORD_NOTIFY_WEBHOOK 미설정" >&2
  exit 1
fi

# 헬스체크 1: 봇 프로세스 생존
BOT_PIDS=$(pgrep -f 'python main.py' | tr '\n' ',' | sed 's/,$//')
if [ -n "$BOT_PIDS" ]; then
  BOT_STATUS="✅ alive (pid=$BOT_PIDS)"
else
  BOT_STATUS="❌ DOWN"
fi

# 헬스체크 2/4 + Stage 4 집계
SQL=$(cat <<EOF
import sqlite3
c = sqlite3.connect('data/kream_bot.db')
cur = c.cursor()
t0 = $BOT_START_TS

# alert_sent 24h (health #2)
cur.execute("SELECT COUNT(*) FROM alert_sent WHERE fired_at >= strftime('%s','now','-24 hours')")
print(f"ALERTS_24H={cur.fetchone()[0]}")

# kream_api_calls 24h (health #3 + Stage 4 집계)
cur.execute("SELECT COUNT(*) FROM kream_api_calls WHERE ts >= datetime(?,'unixepoch')", (t0,))
print(f"KREAM_CALLS_STAGE4={cur.fetchone()[0]}")

# decision_log Stage 4 집계
reasons = ['profit_emitted', 'alert_sent', 'handler_exception', 'handler_missing', 'signal_upgrade', 'throttle_exhausted', 'dedup_recent', 'prefilter_unprofitable']
for r in reasons:
    cur.execute("SELECT COUNT(*) FROM decision_log WHERE ts >= ? AND reason=?", (t0, r))
    print(f"REASON_{r.upper()}={cur.fetchone()[0]}")

cur.execute("SELECT COUNT(*) FROM decision_log WHERE ts >= ?", (t0,))
print(f"DECISION_TOTAL_STAGE4={cur.fetchone()[0]}")
EOF
)
eval $(python3 -c "$SQL")

# Discord 실발사 집계 — v3_alerts.jsonl 에서 signal=매수/강력매수 만
DISCORD_SENT_STAGE4=$(python3 -c "
import json
t0 = $BOT_START_TS
cnt = 0
try:
    with open('logs/v3_alerts.jsonl', encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get('ts', 0) >= t0 and r.get('signal') in ('강력매수', '매수'):
                    cnt += 1
            except Exception:
                continue
except FileNotFoundError:
    pass
print(cnt)
")

# 크림 호출 캡 체크 (24h 최종 gate)
KREAM_CAP=$(grep -E '^KREAM_DAILY_CAP=' .env | cut -d= -f2- | tr -d '"\r\n' || echo "10000")
KREAM_CAP=${KREAM_CAP:-10000}

# 판정 (Stage 4 = 24h, 임계 500 — 6h 150의 ~3.3배, 후반 포화 반영)
if [ -z "$BOT_PIDS" ]; then
  VERDICT="🔴 FAIL — 봇 다운"
elif [ "$REASON_HANDLER_EXCEPTION" != "0" ] || [ "$REASON_HANDLER_MISSING" != "0" ]; then
  VERDICT="🟡 WARN — handler 예외 발견"
elif [ "$KREAM_CALLS_STAGE4" -gt "$KREAM_CAP" ]; then
  VERDICT="🔴 FAIL — 크림 호출 캡 초과 ($KREAM_CALLS_STAGE4 > $KREAM_CAP)"
elif [ "$DECISION_TOTAL_STAGE4" -lt "500" ]; then
  VERDICT="🟡 WARN — 파이프라인 활동 저조 (Stage4 total=$DECISION_TOTAL_STAGE4)"
else
  VERDICT="🟢 PASS — 3단 ladder 완주, 안정화 확정"
fi

MSG=$(cat <<EOF
**Stage 4 (24h) 자동 판정 — 2026-04-23 11:35 KST**
**3단 ladder 최종 gate (2h → 6h → 24h)**

판정: $VERDICT
봇: $BOT_STATUS
가동 시각: 2026-04-22 11:35:30 ~ 2026-04-23 11:35:00 (약 24h)

**헬스체크**
- 알림 24h: $ALERTS_24H 건
- 크림 호출 24h: $KREAM_CALLS_STAGE4 / $KREAM_CAP 회
- Discord 실발사: **$DISCORD_SENT_STAGE4 건** (매수+강력매수만)

**decision_log 24h 집계**
- profit_emitted: $REASON_PROFIT_EMITTED
- alert_sent(JSONL): $REASON_ALERT_SENT (signal_upgrade: $REASON_SIGNAL_UPGRADE) ※ 관망 포함
- handler_exception: $REASON_HANDLER_EXCEPTION / handler_missing: $REASON_HANDLER_MISSING
- throttle_exhausted: $REASON_THROTTLE_EXHAUSTED
- dedup_recent: $REASON_DEDUP_RECENT
- prefilter_unprofitable: $REASON_PREFILTER_UNPROFITABLE
- total: $DECISION_TOTAL_STAGE4

PASS 시 → 소싱처 대거 확장 단계 진입 가능.
EOF
)

PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'content': sys.stdin.read()}))" <<< "$MSG")

curl -s -X POST -H "Content-Type: application/json" -d "$PAYLOAD" "$WEBHOOK" > logs/stage4_webhook_response.txt
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stage 4 check 완료 — verdict: $VERDICT" >> logs/stage4_check.out
