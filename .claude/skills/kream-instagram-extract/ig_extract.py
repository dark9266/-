"""인스타그램 추출기 — 공개 릴/포스트의 og 메타 + 캡션 전문 (read-only).

사용: python3 .claude/skills/kream-instagram-extract/ig_extract.py \
      <URL 또는 shortcode> [--all]
(크림봇은 별도 venv 없이 시스템 python3 를 쓴다 — curl_cffi 설치 확인됨.)

원리(서치인서치 계열, 2026-07-06 실증):
  1) TLS impersonate 로테이션(chrome120→safari18_0→firefox133)으로 공개 페이지 1-fetch.
  2) og:title/og:description 메타에서 작성자·날짜·likes/comments·캡션 서두 확보.
  3) 페이지 내장 JSON 의 "text":"..." 필드들에서 캡션 전문 후보 수집.
  4) ⚠️ 페이지에는 같은 계정의 **인접 릴 캡션도 섞여 온다** — og:description 의 캡션 서두와
     대조해 링크된 릴의 캡션을 확정한다(2026-07-06 qjc.ai 릴에서 실측한 함정).
  5) 로그인월(loginForm / accounts/login 리다이렉트) 감지 시 우회 없이 STILL_BLOCKED 보고.

금지: 로그인/페이월/비공개 계정 우회 시도. 이 스크립트는 공개 페이지가 이미 들고 있는
데이터만 읽는다. 영상 프레임/음성은 범위 밖(텍스트형 릴은 캡션이 사실상 본문).
"""
import html as htmllib
import json
import re
import sys
from contextlib import suppress

from curl_cffi import requests as creq

IMPERSONATES = ["chrome120", "safari18_0", "firefox133"]
HEADERS = {"accept-language": "en-US,en;q=0.9,ko;q=0.8"}
_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


def parse_shortcode(arg: str) -> str:
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", arg)
    return m.group(1) if m else arg.strip().strip("/")


def fetch(url: str):
    last = None
    for imp in IMPERSONATES:
        try:
            r = creq.get(url, impersonate=imp, headers=HEADERS, timeout=30,
                         allow_redirects=True)
        except Exception as e:
            last = f"{imp}: {e!r}"
            continue
        html = r.text or ""
        if r.status_code == 200 and ("loginForm" not in html
                                     and "/accounts/login" not in str(r.url)):
            return html, imp
        last = f"{imp}: status={r.status_code} login_wall?"
    raise SystemExit(f"STILL_BLOCKED — 공개 접근 실패({last}). 우회 금지, 사장 보고 대상.")


def og_meta(page: str) -> dict:
    out = {}
    for m in re.finditer(
        r'<meta[^>]+(?:property|name)="(og:[^"]+|description)"[^>]+content="([^"]*)"', page
    ):
        out[m.group(1)] = htmllib.unescape(m.group(2))
    return out


def caption_candidates(page: str) -> list[str]:
    cands = []
    for m in re.finditer(r'"text"\s*:\s*"((?:[^"\\]|\\.){80,8000})"', page):
        with suppress(Exception):  # 손상 이스케이프 후보는 건너뜀
            cands.append(json.loads('"' + m.group(1) + '"'))
    # dedupe (동일 캡션이 여러 노드에 반복됨)
    seen, uniq = set(), []
    for c in sorted(cands, key=len, reverse=True):
        k = norm(c)[:120]
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    show_all = "--all" in sys.argv[2:]
    code = parse_shortcode(sys.argv[1])
    url = f"https://www.instagram.com/reel/{code}/"
    page, imp = fetch(url)
    og = og_meta(page)
    desc = og.get("og:description", "")

    head = re.match(
        r"\s*([\d,]+) likes, ([\d,]+) comments - ([\w.@_]+) on ([^:]+):", desc)
    author = head.group(3) if head else "?"
    date = head.group(4) if head else "?"
    stats = f"{head.group(1)} likes · {head.group(2)} comments" if head else "?"

    # og:description 의 캡션 서두(따옴표 뒤)로 본문 후보 확정
    lead = ""
    qm = re.search(r':\s*"(.{15,120})', desc, re.S)
    if qm:
        lead = norm(qm.group(1))[:60]
    cands = caption_candidates(page)
    caption = next((c for c in cands if lead and lead[:40] in norm(c)), None)
    matched = caption is not None
    if caption is None:
        caption = og.get("og:title", desc) or "(캡션 추출 실패 — og 메타만 확보)"

    print(f"작성자 @{author} · {date} · {stats} · fetch={imp}")
    print(f"URL   {og.get('og:url', url)}")
    print(f"캡션 매칭: {'내장JSON 전문(확정)' if matched else 'og 메타 fallback(잘림 가능)'}")
    print("=" * 60)
    print(caption[:4000])
    others = [c for c in cands if c is not caption]
    if others:
        print("=" * 60)
        print(f"[인접 릴 캡션 {len(others)}건 동봉 — 본 릴 아님]")
        for c in (others if show_all else others[:2]):
            print(f"  · {norm(c)[:100]}…")


if __name__ == "__main__":
    main()
