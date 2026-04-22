#!/bin/bash
# Stage 3 완주 자동 판정 — 2026-04-22 17:35 KST 실행 예정.
# 헬스체크 4종 + decision_log 6h 집계 + Discord 웹훅 발사.
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

# 헬스체크 2/4 + Stage 3 집계: decision_log
SQL=$(cat <<EOF
import sqlite3
c = sqlite3.connect('data/kream_bot.db')
cur = c.cursor()
t0 = $BOT_START_TS

# alert_sent 24h (health #2)
cur.execute("SELECT COUNT(*) FROM alert_sent WHERE fired_at >= strftime('%s','now','-24 hours')")
print(f"ALERTS_24H={cur.fetchone()[0]}")

# kream_api_calls 6h (health #3)
cur.execute("SELECT COUNT(*) FROM kream_api_calls WHERE ts >= datetime(?,'unixepoch')", (t0,))
print(f"KREAM_CALLS_STAGE3={cur.fetchone()[0]}")

# decision_log Stage 3 집계
reasons = ['profit_emitted', 'alert_sent', 'handler_exception', 'handler_missing', 'signal_upgrade', 'throttle_exhausted', 'dedup_recent', 'prefilter_unprofitable']
for r in reasons:
    cur.execute("SELECT COUNT(*) FROM decision_log WHERE ts >= ? AND reason=?", (t0, r))
    print(f"REASON_{r.upper()}={cur.fetchone()[0]}")

cur.execute("SELECT COUNT(*) FROM decision_log WHERE ts >= ?", (t0,))
print(f"DECISION_TOTAL_STAGE3={cur.fetchone()[0]}")
EOF
)
eval $(python3 -c "$SQL")

# Discord 실발사 집계 — v3_alerts.jsonl 에서 signal=매수/강력매수 만 카운트
# (decision_log.alert_sent 는 관망까지 포함 → Discord 미도착 → 판정 오염 방지)
DISCORD_SENT_STAGE3=$(python3 -c "
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

# 판정 (Stage 3 = 6h, 임계 150 — Stage 2 50의 3배 비례)
if [ -z "$BOT_PIDS" ]; then
  VERDICT="🔴 FAIL — 봇 다운"
elif [ "$REASON_HANDLER_EXCEPTION" != "0" ] || [ "$REASON_HANDLER_MISSING" != "0" ]; then
  VERDICT="🟡 WARN — handler 예외 발견"
elif [ "$DECISION_TOTAL_STAGE3" -lt "150" ]; then
  VERDICT="🟡 WARN — 파이프라인 활동 저조 (Stage3 total=$DECISION_TOTAL_STAGE3)"
else
  VERDICT="🟢 PASS — Stage 4 (24h) 승격 가능"
fi

# Discord 메시지 조립
MSG=$(cat <<EOF
**Stage 3 (6h) 자동 판정 — 2026-04-22 17:35 KST**

판정: $VERDICT
봇: $BOT_STATUS
가동 시각: 11:35:30 ~ 17:35:00 (약 6h)

**헬스체크**
- 알림 24h: $ALERTS_24H 건
- 크림 호출 Stage 3: $KREAM_CALLS_STAGE3 회
- Discord 실발사 Stage 3: **$DISCORD_SENT_STAGE3 건** (매수+강력매수만)

**decision_log Stage 3 집계**
- profit_emitted: $REASON_PROFIT_EMITTED
- alert_sent(JSONL 기록): $REASON_ALERT_SENT (signal_upgrade: $REASON_SIGNAL_UPGRADE) ※ 관망 포함
- handler_exception: $REASON_HANDLER_EXCEPTION / handler_missing: $REASON_HANDLER_MISSING
- throttle_exhausted: $REASON_THROTTLE_EXHAUSTED
- dedup_recent: $REASON_DEDUP_RECENT
- prefilter_unprofitable: $REASON_PREFILTER_UNPROFITABLE
- total: $DECISION_TOTAL_STAGE3

다음 세션에서 "이어서" → Stage 4 (24h) 진입 판단.
EOF
)

# JSON escape
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'content': sys.stdin.read()}))" <<< "$MSG")

curl -s -X POST -H "Content-Type: application/json" -d "$PAYLOAD" "$WEBHOOK" > logs/stage3_webhook_response.txt
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stage 3 check 완료 — verdict: $VERDICT" >> logs/stage3_check.out
