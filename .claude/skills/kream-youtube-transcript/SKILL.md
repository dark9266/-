---
name: kream-youtube-transcript
description: YouTube 영상의 자막(자동/수동 caption)·메타·챕터를 추출해 내용 정리. 사장이 유튜브 URL을 주며 "자막 추출/내용 정리/요약/무슨 내용/스크립트 뽑아줘" 라고 할 때 적용. 서치인서치(curl_cffi)로 메타·자막트랙을 뚫고, PO-token 막힌 자막 본문은 yt-dlp(uvx 임시실행)로 이관. 번들 스크립트 extract.py 로 원커맨드.
---

# YouTube 자막 추출 (서치인서치 메타 + yt-dlp 본문)

`kream-search-in-search` 의 YouTube 특화 고정판. 원리: **메타·자막트랙은 서치인서치로,
자막 본문은 PO-token 차단이라 yt-dlp 로 이관**(STILL_BLOCKED 규정 — 우회 시도가 아니다).

## 언제
- 사장이 유튜브 URL(watch · youtu.be · shorts · live) + "자막/스크립트/내용/요약" 요청.
- 영상 내용을 근거로 정리·판단이 필요할 때(기법 흡수·벤치마킹 등).

## 원커맨드
```bash
python3 .claude/skills/kream-youtube-transcript/extract.py <URL 또는 11자 videoId>
# 옵션: --lang ko,en    (기본: 메타에서 감지 → ko/en 우선 → 전체)
#       --outdir DIR    (기본: 임시폴더)
```
출력: 메타(제목·채널·길이·조회·**설명 + 챕터 타임스탬프**) + **롤링중복 제거한 클린 자막** +
`*.clean.txt` 저장 경로. 여러 언어가 있으면 각각 저장한다. 그 텍스트를 `Read` 로 읽어 정리한다.

## 동작 원리 (막히면 여기 보고 단계별로)
1. **메타·자막트랙 = 서치인서치** — curl_cffi TLS 로테이션(chrome120→safari18_0→firefox133)
   → `ytInitialPlayerResponse` → `videoDetails`(제목/채널/길이/조회/설명) +
   `captions…captionTracks`(언어 · kind=asr 여부).
2. **자막 본문 = yt-dlp** — timedtext 직접 fetch 는 **200 empty = PO-token 차단**(실측:
   raw/srv3/srv1/vtt/json3 전부 empty). 우회하지 않고 이관한다:
   `uvx yt-dlp --write-subs --write-auto-subs --extractor-args
   "youtube:player_client=android,web,tv_embedded"`.
   **uvx = 임시 실행이라 프로젝트 환경 무오염.**
3. **VTT 클린** — auto-sub 은 롤링 중복(각 줄이 다음 줄과 겹침) + `<00:..><c>` 태그 →
   포함관계 dedupe + 태그 스트립 → 한 문단.

## 전제·경계
- 자막·일반 웹 **1회성 조사 fetch 는 사장 요청 자체가 GO**(소싱처 fetch 와는 다르다).
- 로그인 · 페이월 · PO-token **우회 시도 금지**. 자막 본문은 yt-dlp(공식 클라이언트 경로)로만.
- 공개 영상의 공개 자막 텍스트 추출이다 — 영상 다운로드가 아니다(`--skip-download`).

## 함정·메모
- **yt-dlp 미설치가 정상**(프로젝트 의존 아님) → 반드시 `uvx yt-dlp`.
  크림봇 환경 확인됨: `uvx` = `~/.local/bin/uvx`, `curl_cffi` = 시스템 python3 에 설치.
- 자막 자체가 없는 영상 → 자막없음 반환(exit 2). 그땐 **메타·설명·챕터로만** 정리하고
  "자막 없음"을 명시한다(있는 척 지어내지 않는다 — 증거 게이트가 막는다).
- 언어 자동감지 실패 시 `--lang ko,en` 명시. 영어권 영상은 `en`.
- 메타만 급히 필요하면 자막 단계 없이 메타 출력만으로 충분(빠름 · 설치 0).

## 관련
- `kream-search-in-search`(상위 범용 기법) · `kream-instagram-extract`(같은 계열 인스타판)
- 번들: `extract.py`
