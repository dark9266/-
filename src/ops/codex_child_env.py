"""Codex 자식 프로세스용 env — **allowlist**(denylist 아님).

## 왜 allowlist 인가
denylist 는 새 비밀이 생길 때마다 뚫린다. 프로세스 기본값 + Codex 인증에 필요한 키만
통과시키고 **나머지는 전부 제거**한다 — `DISCORD_TOKEN`·`DISCORD_NOTIFY_WEBHOOK`·
`MUSINSA_EMAIL/PASSWORD`·크림 쿠키는 목록에 없으므로 자동 차단된다.

Codex 인증은 `~/.codex/auth.json`(HOME 기준)이라 HOME 만 있으면 된다.
"""

from __future__ import annotations

_EXACT = frozenset({
    "PATH", "HOME", "LANG", "TERM", "TMPDIR", "TMP", "TEMP", "USER", "SHELL",
})
_PREFIXES = ("LC_", "SSL_CERT_", "CODEX_", "XDG_")


def codex_review_child_env(parent_env: dict[str, str]) -> dict[str, str]:
    """read-only Codex 검증 자식에게 물려줄 env 만 남긴다."""
    return {
        k: v
        for k, v in parent_env.items()
        if k in _EXACT or k.startswith(_PREFIXES)
    }
