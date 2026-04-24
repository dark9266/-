#!/usr/bin/env bash
# 크림봇 watchdog — main.py PID 사망 감지 시 자동 재기동 + 디스코드 알림.
# Windows 부팅 직후 자동 가동: scripts/bot_runner_install_task.ps1 참조.
set -u

BOT_DIR="/mnt/c/Users/USER/Desktop/크림봇"
LOG_OUT="$BOT_DIR/logs/bot.out"
RUNNER_LOG="$BOT_DIR/logs/bot_runner.log"
PIDFILE="$BOT_DIR/data/bot_runner.pid"
RESTART_WAIT=300              # 정상 사망 후 재기동 대기 (크림 throttle 회복 여유)
BACKOFF_WAIT=1800             # 연속 크래시 시 backoff
CRASH_LOOP_THRESHOLD=3        # 10분 내 N회 사망 → backoff

cd "$BOT_DIR" || { echo "BOT_DIR not found"; exit 1; }

# venv 활성화 — Windows 작업스케줄러가 빈 PATH로 실행하면 `python` 을 못 찾아 즉사
# shellcheck disable=SC1091
source "$BOT_DIR/venv/bin/activate" || { echo "venv activate failed"; exit 1; }

# 단일 인스턴스 가드
if [ -f "$PIDFILE" ]; then
    OLD=$(cat "$PIDFILE" 2>/dev/null || echo "")
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        echo "$(date '+%F %T') runner already running pid=$OLD" >> "$RUNNER_LOG"
        exit 0
    fi
fi
echo $$ > "$PIDFILE"

WEBHOOK=$(grep -E '^DISCORD_NOTIFY_WEBHOOK=' .env 2>/dev/null | cut -d= -f2-)

log()    { echo "$(date '+%F %T') $*" >> "$RUNNER_LOG"; }
notify() { [ -n "$WEBHOOK" ] && curl -s -X POST -H "Content-Type: application/json" \
           -d "{\"content\":\"$1\"}" "$WEBHOOK" > /dev/null 2>&1; }

cleanup() { log "runner exit (signal)"; rm -f "$PIDFILE"; exit 0; }
trap cleanup INT TERM

log "watchdog started runner_pid=$$"
notify "🟢 크림봇 watchdog 가동 (runner pid=$$)."

CRASH_COUNT=0
LAST_CRASH=0

while true; do
    # -fx (exact full cmdline) — 부모 셸/검색명령 false positive 회피.
    # 봇 단독 사망을 정확히 감지하기 위해 cmdline 정확 매칭만 인정.
    EXISTING=$(pgrep -fx 'python main\.py' | head -1)

    if [ -n "$EXISTING" ]; then
        log "attach existing main.py pid=$EXISTING"
        BOT_PID="$EXISTING"
    else
        log "spawning main.py"
        nohup python main.py >> "$LOG_OUT" 2>&1 &
        BOT_PID=$!
        log "spawned pid=$BOT_PID"
        sleep 5
        if ! kill -0 "$BOT_PID" 2>/dev/null; then
            log "spawn failed (instant death) pid=$BOT_PID"
            notify "🚨 크림봇 즉사 — 부팅 실패. ${RESTART_WAIT}s 후 재시도."
            sleep "$RESTART_WAIT"
            continue
        fi
        notify "🟢 크림봇 가동 (pid=$BOT_PID)."
    fi

    # PID 살아있는 동안 대기
    while kill -0 "$BOT_PID" 2>/dev/null; do
        sleep 30
    done

    NOW=$(date +%s)
    UPTIME=$((NOW - LAST_CRASH))
    if [ "$UPTIME" -lt 600 ]; then
        CRASH_COUNT=$((CRASH_COUNT + 1))
    else
        CRASH_COUNT=1
    fi
    LAST_CRASH=$NOW

    log "main.py died pid=$BOT_PID crash_count=$CRASH_COUNT"

    WAIT="$RESTART_WAIT"
    if [ "$CRASH_COUNT" -ge "$CRASH_LOOP_THRESHOLD" ]; then
        WAIT="$BACKOFF_WAIT"
        log "crash loop detected — backoff to ${WAIT}s"
        notify "⚠️ 크림봇 연속 크래시 ${CRASH_COUNT}회 — $((WAIT/60))분 backoff."
    else
        notify "🚨 크림봇 사망 감지 (pid=$BOT_PID, 연속 ${CRASH_COUNT}회). $((WAIT/60))분 후 재기동."
    fi
    sleep "$WAIT"
done
