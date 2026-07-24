#!/usr/bin/env python3
"""YouTube 자막 추출 — 서치인서치(메타) + yt-dlp(본문, PO-token 차단 이관).

`kream-search-in-search` 로 메타·자막트랙은 뚫리지만 **자막 본문은 timedtext PO-token
차단(200 empty)** → 전용도구 yt-dlp 로 이관(스킬 STILL_BLOCKED 규정. 우회 시도 아님).
프로젝트 환경 무오염 위해 `uvx yt-dlp`(임시 실행) 사용.

usage:
    python3 .claude/skills/kream-youtube-transcript/extract.py <url|videoId> \
        [--lang ko,en] [--outdir DIR]
출력: 메타(제목/채널/길이/조회/설명·챕터) + 롤링중복 제거한 클린 자막 텍스트, 파일 저장.
"""
from __future__ import annotations

import argparse
import glob
import html as _html
import json
import os
import re
import subprocess
import sys
import tempfile

_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def _json_after(text: str, marker: str) -> str | None:
    """marker 뒤 첫 `{` 부터 **문자열 리터럴을 존중해** 균형 중괄호까지 잘라낸다.

    Codex 2026-07-04: `\\{.+?\\}` 비탐욕 정규식은 설명에 `{};` 가 있으면 JSON 문자열 중간에서
    끊겨 파싱 실패 → 균형 파서로 교체(설명 내 중괄호 안전).
    """
    i = text.find(marker)
    if i < 0:
        return None
    i = text.find("{", i)
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return None


def video_id(s: str) -> str | None:
    m = _ID_RE.search(s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_-]{11}", s) else None


def fetch_metadata(vid: str) -> dict | None:
    """서치인서치: curl_cffi TLS 로테이션 → ytInitialPlayerResponse."""
    try:
        from curl_cffi import requests
    except ImportError:
        return None
    for imp in ("chrome120", "safari18_0", "firefox133"):
        try:
            r = requests.get(f"https://www.youtube.com/watch?v={vid}", impersonate=imp,
                             timeout=25, headers={"Accept-Language": "ko,en;q=0.9"})
        except Exception:  # noqa: BLE001,S112 — 다음 impersonate 로 폴백
            continue
        if r.status_code != 200 or "ytInitialPlayerResponse" not in r.text:
            continue
        blob = _json_after(r.text, "ytInitialPlayerResponse")
        if not blob:
            continue
        try:
            pr = json.loads(blob)
        except json.JSONDecodeError:
            continue
        vd = pr.get("videoDetails", {})
        caps = (pr.get("captions", {}).get("playerCaptionsTracklistRenderer", {})
                .get("captionTracks", []))
        return {
            "title": vd.get("title"), "author": vd.get("author"),
            "length_sec": vd.get("lengthSeconds"), "views": vd.get("viewCount"),
            "desc": vd.get("shortDescription", "") or "",
            "caption_langs": [(c.get("languageCode"), c.get("kind", "")) for c in caps],
            "via": imp,
        }
    return None


def clean_vtt(path: str) -> str:
    """auto-sub VTT 롤링 중복 제거 + 타이밍/태그 스트립 → 한 문단 텍스트."""
    out: list[str] = []
    for ln in open(path, encoding="utf-8").read().splitlines():
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
        if out and t == out[-1]:
            continue
        out.append(t)
    return " ".join(out)


def download_subs(vid: str, langs: list[str], outdir: str) -> list[str]:
    """uvx yt-dlp — 수동자막 우선, 없으면 자동자막. VTT 파일 경로들 반환.

    Codex 2026-07-04: 재사용 outdir 의 이전(다른 영상/언어) cap*.vtt 를 먼저 제거하고,
    yt-dlp 성공 여부를 확인한 뒤에만 새로 생성된 파일을 반환한다(엉뚱한 영상 자막 오반환 방지).
    """
    url = f"https://www.youtube.com/watch?v={vid}"
    base = os.path.join(outdir, "cap")
    for stale in glob.glob(base + "*.vtt"):  # 이전 호출 잔재 제거
        try:
            os.remove(stale)
        except OSError:
            pass
    sub_langs = ",".join(langs) if langs else "ko,en,.*"
    cmd = [
        "uvx", "yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
        "--sub-langs", sub_langs, "--sub-format", "vtt/best",
        "--extractor-args", "youtube:player_client=android,web,tv_embedded",
        "-o", base + ".%(ext)s", url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)  # noqa: S603
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[yt-dlp 실행 실패: {type(e).__name__}] uvx/yt-dlp 확인 필요", file=sys.stderr)
        return []
    files = sorted(glob.glob(base + "*.vtt"))
    if not files and r.returncode != 0:
        print(f"[yt-dlp rc={r.returncode}] {(r.stderr or '')[-300:]}", file=sys.stderr)
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="YouTube URL 또는 11자 videoId")
    ap.add_argument("--lang", default="", help="자막 언어 csv (기본: 메타감지→ko,en)")
    ap.add_argument("--outdir", default="", help="자막 저장 폴더 (기본 scratchpad/tmp)")
    a = ap.parse_args()

    vid = video_id(a.target)
    if not vid:
        print("videoId 추출 실패:", a.target)
        return 1
    outdir = a.outdir or tempfile.mkdtemp(prefix="yt_")

    meta = fetch_metadata(vid)
    print(f"=== 메타 (서치인서치{'/'+meta['via'] if meta else ' 실패→yt-dlp만'}) ===")
    if meta:
        print("제목  :", meta["title"])
        print("채널  :", meta["author"])
        print("길이  :", meta["length_sec"], "sec / 조회:", meta["views"])
        print("자막  :", meta["caption_langs"] or "없음(자동자막만 가능성)")
        print("\n--- 설명(챕터 포함, 앞 1500자) ---\n", meta["desc"][:1500])

    # 언어 선택: --lang > 메타 감지 > 기본
    if a.lang:
        langs = [x.strip() for x in a.lang.split(",") if x.strip()]
    elif meta and meta["caption_langs"]:
        codes = [c for c, _ in meta["caption_langs"] if c]
        pref = [c for c in codes if c.startswith(("ko", "en"))] or codes
        langs = pref
    else:
        langs = []

    print(f"\n=== 자막 본문 (yt-dlp, langs={langs or 'ko,en,전체'}) ===")
    files = download_subs(vid, langs, outdir)
    if not files:
        print("자막 없음/차단 — STILL_BLOCKED. (동영상에 자막 자체가 없거나 yt-dlp 미설치)")
        return 2
    for f in files:
        text = clean_vtt(f)
        txt_path = f.rsplit(".vtt", 1)[0] + ".clean.txt"
        open(txt_path, "w", encoding="utf-8").write(text)
        lang = os.path.basename(f).split(".")[-2]
        print(f"\n[{lang}] {len(text)} chars → {txt_path}")
        print("--- 앞 600자 ---\n", text[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
