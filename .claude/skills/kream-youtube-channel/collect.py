#!/usr/bin/env python3
"""유튜브 채널 전수 수집 — 채널 URL 1개 → 롱폼+쇼츠 전 편 메타·자막 벌크 다운로드.

단건 스킬(`kream-youtube-transcript`)이 영상 1편용이라면 이쪽은 **경쟁 채널 통째로**다.
링크를 일일이 줄 필요가 없다 — 채널 주소 하나면 열거부터 자막까지 끝난다.

    열거   : yt-dlp --flat-playlist (API 키 0원, 쇼츠 탭 포함)
    수집   : 영상당 yt-dlp 1회 호출로 info.json + 자막 동시 취득
    재개   : <id>.json 이 있으면 건너뜀 — 수백 편 도중 끊겨도 이어받는다
    산출   : index.json / index.csv (메타) + <id>.txt (자막 전문)

usage:
    python3 .claude/skills/kream-youtube-channel/collect.py @핸들 --all
    python3 .claude/skills/kream-youtube-channel/collect.py <채널URL> --limit 50 \
        [--tabs videos,shorts,streams] [--outdir DIR] [--concurrency 3]
"""
from __future__ import annotations

import argparse
import csv
import glob
import html as _html
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

TABS = ("videos", "shorts", "streams")


def channel_base(s: str) -> str:
    """@핸들 · 채널명 · 채널URL · /videos·/shorts 붙은 URL → 탭 없는 베이스 URL."""
    s = s.strip().rstrip("/")
    if not s.startswith("http"):
        return f"https://www.youtube.com/@{s.lstrip('@')}"
    for tab in (*TABS, "featured", "playlists", "community", "shorts"):
        if s.endswith("/" + tab):
            return s[: -(len(tab) + 1)]
    return s


def clean_vtt(path: str) -> str:
    """auto-sub VTT 롤링 중복 제거 + 타이밍/태그 스트립 → 한 문단 텍스트."""
    out: list[str] = []
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for ln in lines:
        if ("-->" in ln or ln.strip() in ("WEBVTT", "")
                or ln.startswith(("Kind:", "Language:", "NOTE"))):
            continue
        t = _html.unescape(re.sub(r"<[^>]+>", "", ln)).strip()
        if not t:
            continue
        if out and (t in out[-1] or out[-1] in t):
            if len(t) > len(out[-1]):
                out[-1] = t
            continue
        out.append(t)
    return " ".join(out)


def enumerate_tab(base: str, tab: str, limit: int = 0) -> list[dict]:
    """탭 하나 열거. `--flat-playlist -j` = 1행 1영상 JSON 스트리밍(대형 채널 안전)."""
    cmd = ["uvx", "yt-dlp", "--flat-playlist", "-j", "--ignore-errors", "--no-warnings"]
    if limit:
        cmd += ["--playlist-end", str(limit)]
    cmd += [f"{base}/{tab}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)  # noqa: S603
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[열거 실패 {tab}: {type(e).__name__}] uvx/yt-dlp 확인", file=sys.stderr)
        return []
    rows: list[dict] = []
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        vid = e.get("id") or ""
        if len(vid) != 11:
            continue
        rows.append({"id": vid, "tab": tab, "title": e.get("title") or "",
                     "view_count": e.get("view_count")})
    if not rows and r.returncode != 0:
        print(f"[{tab} rc={r.returncode}] {(r.stderr or '')[-200:]}", file=sys.stderr)
    return rows


def _pick_vtt(paths: list[str], langs: list[str]) -> str | None:
    """선호 언어 우선으로 VTT 1개 선택 (같은 영상의 다국어 중복 방지)."""
    for want in [*langs, "ko", "en"]:
        for p in paths:
            if f".{want}" in os.path.basename(p):
                return p
    return paths[0] if paths else None


def fetch_one(vid: str, outdir: str, sub_langs: str, timeout: int = 240) -> dict:
    """영상 1편: 메타 + 자막을 yt-dlp **1회 호출**로 동시 수집 → 정규화 dict 저장."""
    row_path = os.path.join(outdir, f"{vid}.json")
    if os.path.exists(row_path):  # resume — 이미 받은 건 재요청 안 한다
        try:
            with open(row_path, encoding="utf-8") as f:
                row = json.load(f)
            row["resumed"] = True
            return row
        except (OSError, json.JSONDecodeError):
            pass

    stem = os.path.join(outdir, vid)
    cmd = [
        "uvx", "yt-dlp", "--skip-download", "--write-info-json",
        "--write-subs", "--write-auto-subs", "--sub-langs", sub_langs,
        "--sub-format", "vtt/best", "--ignore-errors", "--no-warnings",
        "--extractor-args", "youtube:player_client=android,web,tv_embedded",
        "-o", stem + ".%(ext)s", f"https://www.youtube.com/watch?v={vid}",
    ]
    err = ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
        err = (r.stderr or "").strip()[-200:]
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        err = type(e).__name__

    info: dict = {}
    ipath = stem + ".info.json"
    if os.path.exists(ipath):
        try:
            with open(ipath, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, json.JSONDecodeError):
            info = {}
        os.remove(ipath)  # 필요한 필드만 정규화 저장 — 원본 info.json 은 용량만 먹는다

    langs = [x.split(".")[0] for x in sub_langs.split(",") if x]
    vtts = sorted(glob.glob(stem + "*.vtt"))
    vtt = _pick_vtt(vtts, langs)
    text = clean_vtt(vtt) if vtt else ""
    tpath = ""
    if text:
        tpath = stem + ".txt"
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(text)
    for junk in vtts:
        try:
            os.remove(junk)
        except OSError:
            pass

    row = {
        "id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
        "title": info.get("title") or "", "upload_date": info.get("upload_date") or "",
        "duration": info.get("duration"), "view_count": info.get("view_count"),
        "like_count": info.get("like_count"), "comment_count": info.get("comment_count"),
        "tags": (info.get("tags") or [])[:20],
        "description": (info.get("description") or "")[:2000],
        "transcript_chars": len(text), "transcript_path": tpath,
        "error": "" if (info or text) else err,
    }
    with open(row_path, "w", encoding="utf-8") as f:
        json.dump(row, f, ensure_ascii=False, indent=1)
    return row


def write_index(done: list[dict], outdir: str) -> None:
    done.sort(key=lambda d: d.get("view_count") or 0, reverse=True)
    with open(os.path.join(outdir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(done, f, ensure_ascii=False, indent=1)
    cols = ["id", "tab", "upload_date", "view_count", "like_count", "comment_count",
            "duration", "transcript_chars", "title", "url"]
    with open(os.path.join(outdir, "index.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(done)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("channel", help="채널 URL 또는 @핸들 (탭 붙어 있어도 됨)")
    ap.add_argument("--tabs", default="videos,shorts", help="수집 탭 csv (videos,shorts,streams)")
    ap.add_argument("--all", action="store_true", help="채널 전체 수집 (--limit 무시)")
    ap.add_argument("--limit", type=int, default=30, help="탭당 최신 N편 (기본 30)")
    ap.add_argument("--concurrency", type=int, default=3, help="동시 수집 수 (기본 3)")
    ap.add_argument("--sub-langs", default="ko.*,en.*", help="자막 언어 패턴")
    ap.add_argument("--outdir", default="", help="저장 폴더 (기본 data/yt/<핸들>)")
    a = ap.parse_args()

    base = channel_base(a.channel)
    outdir = a.outdir or os.path.join("data", "yt", re.sub(r"[^\w.@-]", "_", base.split("/")[-1]))
    os.makedirs(outdir, exist_ok=True)
    tabs = [t.strip() for t in a.tabs.split(",") if t.strip() in TABS]
    limit = 0 if a.all else a.limit

    print(f"=== 열거: {base} | 탭={tabs} | {'전체' if not limit else f'탭당 최신 {limit}편'} ===")
    listing: list[dict] = []
    seen: set[str] = set()
    for tab in tabs:
        rows = [r for r in enumerate_tab(base, tab, limit)
                if not (r["id"] in seen or seen.add(r["id"]))]
        print(f"  {tab:8s} {len(rows):5d}편")
        listing += rows
    if not listing:
        print("열거 0편 — 채널 주소 확인(핸들 오타/비공개/탭 없음).")
        return 2
    print(f"  합계     {len(listing):5d}편 → 수집 (동시 {a.concurrency}) → {outdir}")

    tab_of = {r["id"]: r["tab"] for r in listing}
    done: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
        futs = [ex.submit(fetch_one, r["id"], outdir, a.sub_langs) for r in listing]
        for i, fu in enumerate(futs, 1):
            row = fu.result()
            row["tab"] = tab_of.get(row["id"], "")
            done.append(row)
            if i % 10 == 0 or i == len(futs):
                got = sum(1 for d in done if d["transcript_chars"])
                print(f"  … {i}/{len(futs)} (자막 {got}편)", flush=True)

    write_index(done, outdir)
    with_t = [d for d in done if d["transcript_chars"]]
    fails = [d for d in done if d.get("error")]
    print(f"\n=== 수집 완료 · {outdir} ===")
    print(f"총 {len(done)}편 | 자막 확보 {len(with_t)}편 | 실패 {len(fails)}편 | "
          f"자막 총 {sum(d['transcript_chars'] for d in done):,}자")
    for tab in tabs:
        sub = [d for d in done if d["tab"] == tab]
        if sub:
            vc = sorted((d["view_count"] or 0) for d in sub)
            print(f"  {tab:8s} {len(sub):4d}편 | 조회 중앙값 {vc[len(vc)//2]:,} | 최고 {vc[-1]:,}")
    print(f"\n다음: python3 {os.path.dirname(__file__)}/analyze.py {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
