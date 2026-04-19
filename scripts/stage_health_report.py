"""Stage health reporter — Phase D 관측용.

재시작 시각(--since ISO8601) 이후 알림/큐/에러 수집 + 크림 캡 + 프로세스 + Discord 웹훅 발송.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kream_bot.db"
LOG = ROOT / "logs" / "bot.out"


def load_webhook() -> str | None:
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DISCORD_NOTIFY_WEBHOOK="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def bot_alive() -> tuple[bool, str]:
    out = subprocess.run(
        ["pgrep", "-af", "python.*main.py"], capture_output=True, text=True
    )
    lines = [l for l in out.stdout.splitlines() if "grep" not in l]
    return (bool(lines), "\n".join(lines) if lines else "DEAD")


def db_counts(since_iso: str) -> dict:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    cur = c.cursor()
    out: dict = {}
    since_ts = datetime.fromisoformat(since_iso).timestamp()
    # alert_sent: fired_at = unix float
    row = cur.execute(
        "SELECT COUNT(*) AS n, MAX(fired_at) AS last FROM alert_sent WHERE fired_at >= ?",
        (since_ts,),
    ).fetchone()
    out["alerts_since"] = row["n"]
    out["alert_last"] = row["last"]
    # kream_collect_queue: added_at = ISO text
    row = cur.execute(
        "SELECT COUNT(*) AS n, MAX(added_at) AS last FROM kream_collect_queue WHERE added_at >= ?",
        (since_iso.replace("T", " ").split("+")[0].split(".")[0],),
    ).fetchone()
    out["queue_since"] = row["n"]
    out["queue_last"] = row["last"]
    # kream_api_calls 24h — ts 는 ISO text ('YYYY-MM-DD HH:MM:SS').
    # strftime('%s',..) (unix second) 와 비교하면 문자열 사전순으로 전 기간이
    # 통과되어 누적 수치가 24h 로 오집계된다. datetime(..) 로 ISO 비교.
    row = cur.execute(
        "SELECT COUNT(*) AS n FROM kream_api_calls WHERE ts >= datetime('now','-1 day')"
    ).fetchone()
    out["kream_calls_24h"] = row["n"]
    # decision_log since (optional)
    try:
        row = cur.execute(
            "SELECT reason, COUNT(*) AS n FROM decision_log WHERE created_at >= ? "
            "GROUP BY reason ORDER BY n DESC LIMIT 8",
            (since_iso.replace("T", " ").split("+")[0].split(".")[0],),
        ).fetchall()
        out["decisions"] = [(r["reason"], r["n"]) for r in row]
    except sqlite3.OperationalError:
        out["decisions"] = []
    c.close()
    return out


def tail_errors(n: int = 2000) -> tuple[int, int, list[str]]:
    if not LOG.exists():
        return 0, 0, []
    # tail last N lines
    with LOG.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = 1 << 16
        data = b""
        while size > 0 and data.count(b"\n") <= n:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()[-n:]
    err_count = sum(1 for l in lines if re.search(r"\bERROR\b|Traceback", l))
    warn_count = sum(1 for l in lines if re.search(r"\bWARNING\b", l))
    samples = [l for l in lines if re.search(r"\bERROR\b|Traceback", l)][-5:]
    return err_count, warn_count, samples


def stage_verdict(alive: bool, alerts: int, queue: int, errors: int) -> str:
    if not alive:
        return "❌ FAIL — 봇 죽음"
    if queue == 0:
        return "❌ FAIL — 큐 갱신 없음"
    if errors > 20:
        return f"⚠️ WARN — 에러 {errors}건"
    if alerts == 0 and queue > 0:
        return "🟡 PARTIAL — 큐 OK, 알림 없음(정상일 수 있음)"
    return "✅ PASS"


def post_webhook(url: str, embed: dict) -> None:
    req = urllib.request.Request(
        url,
        data=__import__("json").dumps({"embeds": [embed]}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "kream-bot-stage-reporter/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="ISO8601 재시작 시각 (UTC)")
    ap.add_argument("--stage", default="1h", help="스테이지 라벨 (1h/2h/6h/12h/24h)")
    args = ap.parse_args()

    alive, proc = bot_alive()
    db = db_counts(args.since)
    err_n, warn_n, err_samples = tail_errors()
    verdict = stage_verdict(alive, db["alerts_since"], db["queue_since"], err_n)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fields = [
        {"name": "프로세스", "value": ("alive" if alive else "DEAD") + f"\n`{proc[:120]}`", "inline": True},
        {"name": "알림 신규", "value": str(db["alerts_since"]), "inline": True},
        {"name": "큐 신규", "value": str(db["queue_since"]), "inline": True},
        {"name": "크림 24h", "value": f"{db['kream_calls_24h']:,}", "inline": True},
        {"name": "에러/경고", "value": f"ERR {err_n} / WARN {warn_n}", "inline": True},
        {"name": "기준", "value": f"since {args.since}", "inline": True},
    ]
    if db["decisions"]:
        fields.append({
            "name": "decision_log TOP",
            "value": "\n".join(f"`{r}` × {n}" for r, n in db["decisions"])[:1000],
            "inline": False,
        })
    if err_samples:
        fields.append({
            "name": "최근 에러 샘플",
            "value": "```\n" + "\n".join(s[:180] for s in err_samples)[:900] + "\n```",
            "inline": False,
        })

    embed = {
        "title": f"Phase D · Stage {args.stage} 헬스리포트 — {verdict}",
        "description": f"관측 기준: `{args.since}` → `{now}`",
        "color": 0x2ECC71 if verdict.startswith("✅") else (0xE67E22 if verdict.startswith("🟡") else 0xE74C3C),
        "fields": fields,
        "timestamp": now,
    }

    hook = load_webhook()
    if not hook:
        print("NO WEBHOOK", file=sys.stderr)
        print(embed)
        return 2
    post_webhook(hook, embed)
    print(f"SENT: {verdict}")
    return 0 if verdict.startswith("✅") else 1


if __name__ == "__main__":
    raise SystemExit(main())
