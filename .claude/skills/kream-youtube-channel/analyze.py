#!/usr/bin/env python3
"""수집한 채널을 **벤치마킹 관점 정량 리포트**로 환산 — collect.py 산출물 → report.md.

정성 해석(콘텐츠 전략·차별화 포인트)은 에이전트가 자막을 읽고 하고, 이 스크립트는
"어떤 영상을 읽어야 하는가" 를 숫자로 좁힌다. 수백 편 자막을 통째로 문맥에 넣지 않기 위한 축소기.

    · 업로드 주기 / 쇼츠·롱폼 비중과 성과 격차
    · 조회수 분포(중앙값 기준 — 평균은 대박 1건에 오염된다)
    · 제목 키워드 **리프트**(그 키워드 포함 영상 조회 중앙값 ÷ 채널 전체 중앙값)
    · 태그·해시태그 빈도, 영상 길이 구간별 성과
    · 상위 영상 오프닝 훅(자막 앞 120자) — 후킹 카피 벤치마킹용

usage: python3 analyze.py <수집폴더> [--top 15] [--out report.md]
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict

STOP = {
    "그리고", "하는", "있는", "합니다", "입니다", "해서", "에서", "으로", "이거", "그거",
    "너무", "진짜", "정말", "이번", "우리", "제가", "저는", "여러분", "영상", "구독", "the",
    "and", "for", "with", "this", "that", "you", "your",
}
TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9]{2,}|\d+[만억원%]")


def median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def load(outdir: str) -> list[dict]:
    idx = os.path.join(outdir, "index.json")
    if os.path.exists(idx):
        with open(idx, encoding="utf-8") as f:
            return json.load(f)
    rows = []  # index.json 없으면 개별 <id>.json 에서 복원 (중단된 수집도 분석 가능)
    for p in sorted(os.listdir(outdir)):
        if p.endswith(".json") and len(p) == 16:
            with open(os.path.join(outdir, p), encoding="utf-8") as f:
                rows.append(json.load(f))
    return rows


def tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN.findall(text) if t.lower() not in STOP}


def section_overview(rows: list[dict], out: list[str]) -> None:
    dates = sorted(r["upload_date"] for r in rows if r.get("upload_date"))
    lo, hi = (dates[0], dates[-1]) if dates else ("?", "?")
    out.append(f"- 총 **{len(rows)}편** · 기간 {lo} ~ {hi}")
    per_month = Counter(d[:6] for d in dates)
    recent = sorted(per_month.items())[-12:]
    out.append(f"- 월별 업로드(최근 12개월): {', '.join(f'{m[4:6]}월 {c}편' for m, c in recent)}")
    if len(dates) > 1:
        out.append(f"- 월 평균 **{len(dates) / max(1, len(per_month)):.1f}편**")


def section_format(rows: list[dict], out: list[str]) -> None:
    out.append("\n## 2. 포맷별 성과 (쇼츠 vs 롱폼)\n")
    out.append("| 포맷 | 편수 | 조회 중앙값 | 조회 최고 | 좋아요율 | 댓글율 | 평균 길이 |")
    out.append("|---|--:|--:|--:|--:|--:|--:|")
    for tab in ("shorts", "videos", "streams"):
        sub = [r for r in rows if r.get("tab") == tab]
        if not sub:
            continue
        vc = [r.get("view_count") or 0 for r in sub]
        lr = [(r.get("like_count") or 0) / v for r, v in zip(sub, vc, strict=False) if v]
        cr = [(r.get("comment_count") or 0) / v for r, v in zip(sub, vc, strict=False) if v]
        du = [r.get("duration") or 0 for r in sub]
        out.append(f"| {tab} | {len(sub)} | {median(vc):,.0f} | {max(vc):,} | "
                 f"{median(lr) * 100:.2f}% | {median(cr) * 100:.3f}% | {median(du):.0f}s |")


def section_lift(rows: list[dict], out: list[str], min_n: int = 3, top: int = 20) -> None:
    """제목 키워드 리프트 — '이 단어를 쓰면 조회가 몇 배' 를 중앙값 기준으로."""
    gm = median([r.get("view_count") or 0 for r in rows]) or 1
    bag: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        for t in tokens(r.get("title") or ""):
            bag[t].append(r.get("view_count") or 0)
    scored = [(k, len(v), median(v) / gm) for k, v in bag.items() if len(v) >= min_n]
    scored.sort(key=lambda x: x[2], reverse=True)
    out.append(f"\n## 3. 제목 키워드 리프트 (조회 중앙값 {gm:,.0f} 기준, {min_n}편+ 등장)\n")
    out.append("| 키워드 | 등장 | 리프트 | 해당 영상 조회 중앙값 |")
    out.append("|---|--:|--:|--:|")
    for k, n, lift in scored[:top]:
        out.append(f"| {k} | {n} | **{lift:.2f}x** | {lift * gm:,.0f} |")
    out.append("\n**하위(성과 낮은 키워드)**: " + ", ".join(
        f"{k}({lift:.2f}x)" for k, _, lift in scored[-8:]) if len(scored) > 8 else "")


def section_duration(rows: list[dict], out: list[str]) -> None:
    buckets = [(0, 60, "쇼츠 ~60s"), (60, 300, "1~5분"), (300, 600, "5~10분"),
               (600, 1200, "10~20분"), (1200, 10**9, "20분+")]
    out.append("\n## 4. 길이 구간별 성과\n")
    out.append("| 구간 | 편수 | 조회 중앙값 |")
    out.append("|---|--:|--:|")
    for lo, hi, name in buckets:
        sub = [r for r in rows if lo <= (r.get("duration") or 0) < hi]
        if sub:
            mv = median([r.get("view_count") or 0 for r in sub])
            out.append(f"| {name} | {len(sub)} | {mv:,.0f} |")


def section_tags(rows: list[dict], out: list[str], top: int = 25) -> None:
    tags = Counter(t.lower() for r in rows for t in (r.get("tags") or []))
    hashes = Counter(h.lower() for r in rows
                     for h in re.findall(r"#([\w가-힣]+)", r.get("description") or ""))
    out.append("\n## 5. 태그 · 해시태그\n")
    out.append("- **채널 태그 TOP**: " + ", ".join(f"{k}({c})" for k, c in tags.most_common(top)))
    out.append("- **설명 해시태그 TOP**: "
               + ", ".join(f"#{k}({c})" for k, c in hashes.most_common(top)))


def section_hooks(rows: list[dict], outdir: str, out: list[str], top: int) -> None:
    """상위 영상의 오프닝 훅 — 벤치마킹에서 가장 실전적인 자산."""
    ranked = sorted(rows, key=lambda r: r.get("view_count") or 0, reverse=True)[:top]
    out.append(f"\n## 6. 조회수 TOP {top} — 제목 + 오프닝 훅(자막 앞 120자)\n")
    for i, r in enumerate(ranked, 1):
        head = ""
        p = r.get("transcript_path") or os.path.join(outdir, r["id"] + ".txt")
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                head = f.read(400).strip().replace("\n", " ")[:120]
        out.append(f"{i}. **{r.get('view_count') or 0:,}회** · {r.get('tab', '')} · "
                 f"{r.get('upload_date', '')} · [{r.get('title', '')[:60]}]({r.get('url', '')})")
        out.append(f"   - 훅: {head or '(자막 없음)'}")


def section_recent(rows: list[dict], out: list[str]) -> None:
    dated = sorted((r for r in rows if r.get("upload_date")), key=lambda r: r["upload_date"])
    if len(dated) < 10:
        return
    recent, past = dated[-20:], dated[:-20]
    out.append("\n## 7. 최근 전환 (최근 20편 vs 이전)\n")
    mv_recent = median([r.get("view_count") or 0 for r in recent])
    mv_past = median([r.get("view_count") or 0 for r in past])
    out.append(f"- 최근 20편 조회 중앙값 **{mv_recent:,.0f}** vs 이전 {mv_past:,.0f}")
    rt = Counter(t for r in recent for t in tokens(r.get("title") or ""))
    pt = Counter(t for r in past for t in tokens(r.get("title") or ""))
    fresh = [k for k, c in rt.most_common(30) if c >= 2 and pt.get(k, 0) == 0]
    out.append(f"- 최근에만 등장한 제목 키워드(새 전략 신호): {', '.join(fresh[:15]) or '없음'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir", help="collect.py 수집 폴더")
    ap.add_argument("--top", type=int, default=15, help="TOP N 훅 추출 (기본 15)")
    ap.add_argument("--out", default="", help="리포트 경로 (기본 <outdir>/report.md)")
    a = ap.parse_args()

    rows = [r for r in load(a.outdir) if r.get("view_count") is not None or r.get("title")]
    if not rows:
        print("분석할 데이터 없음 — collect.py 먼저 실행")
        return 2

    out: list[str] = [f"# 채널 벤치마킹 리포트 — `{a.outdir}`\n", "## 1. 개요\n"]
    section_overview(rows, out)
    section_format(rows, out)
    section_lift(rows, out)
    section_duration(rows, out)
    section_tags(rows, out)
    section_hooks(rows, a.outdir, out, a.top)
    section_recent(rows, out)
    out.append("\n---\n정성 분석(전략·차별화·따라할 것/버릴 것)은 "
               "위 TOP 영상 자막 전문을 읽고 판단한다.")

    report = "\n".join(out)
    path = a.out or os.path.join(a.outdir, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print(f"\n→ 저장: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
