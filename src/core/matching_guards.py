"""매칭 가드 — 오매칭 차단 순수 함수 집합.

Phase 2 공통화. 기존 `src/matcher.py`·`src/reverse_scanner.py`·
`src/tier2_monitor.py`에 흩어져 있던 가드 로직을 단일 모듈로 이관.

목적:
- 푸시 엔진이 추가되면서 동일 로직이 여러 곳에 복제되는 것을 방지
- 거짓 알림 0 지향 (정확도 하드 제약)
- 수정 시 회귀 방지 — 한 곳만 고치면 전체 파이프라인 반영

가드 4종:
1. 콜라보 불일치 — 크림=콜라보, 소싱=일반이면 오매칭 차단
2. 서브타입 불일치 — 소싱에만 있는 서브타입(PRM/QS/SE 등) → 다른 상품
3. 가격 새너티 — 크림가가 소싱가 5배 이상이면 오매칭 의심
4. 콜라보 감지 — 공통 헬퍼
"""

from __future__ import annotations

# ─── 콜라보 키워드 ────────────────────────────────────────
# 일반 상품과 구분해야 하는 브랜드·디자이너 키워드.
# "크림=콜라보 + 소싱=일반" 매칭 차단에 사용.
COLLAB_KEYWORDS: frozenset[str] = frozenset({
    # 영문
    "travis scott", "supreme", "off-white", "off white", "sacai", "dior",
    "louis vuitton", "fragment", "union", "ambush", "stussy", "peaceminusone",
    "clot", "undercover", "comme des garcons", "cdg", "parra", "undefeated",
    "atmos", "j balvin", "cactus jack", "billie eilish", "tiffany",
    # 한국어 (크림 상품명에 사용)
    "트래비스 스캇", "슈프림", "오프화이트", "사카이", "디올",
    "프라그먼트", "유니온", "앰부쉬", "스투시", "유토피아",
    "캑터스", "언더커버", "피스마이너스원", "빌리 아일리시",
    "자크뮈스", "루이비통", "티파니", "파라", "클롯",
})


# ─── 서브타입 키워드 (특별판 구분) ─────────────────────────
# 소싱에만 있는 서브타입 → 크림 상품은 일반판인데 소싱은 특별판이라 다른 상품.
# 예: 소싱="PRM QS" + 크림="07" → 차단.
SUBTYPE_KEYWORDS: frozenset[str] = frozenset({
    "prm", "premium", "qs", "retro", "레트로", "se", "craft",
    "next", "nature", "lx", "flyknit", "react",
    "gore", "tex", "goretex", "acg",
})

# 서브타입 동의어 (정규화용)
# "레트로"(한글) ↔ "retro"(영문): 크림은 한글, 소싱은 영문 사용해 양방향 오차단 발생.
SUBTYPE_ALIASES: dict[str, str] = {
    "prm": "premium",
    "레트로": "retro",
}

# 가격 새너티 배수 — 크림가가 소싱가의 N배 이상이면 차단
PRICE_SANITY_MULTIPLIER: float = 5.0


# ─── 가드 함수 ────────────────────────────────────────────

def is_collab(name: str) -> bool:
    """상품명에 콜라보 키워드가 있는지."""
    if not name:
        return False
    lowered = name.lower()
    return any(kw in lowered for kw in COLLAB_KEYWORDS)


def collab_match_fails(kream_name: str, source_name: str) -> bool:
    """크림=콜라보인데 소싱=일반이면 True (매칭 차단).

    역: 크림=일반, 소싱=콜라보는 차단하지 않음 (소싱이 더 상세할 수 있음).
    """
    kream_is_collab = is_collab(kream_name)
    if not kream_is_collab:
        return False
    source_is_collab = is_collab(source_name)
    return not source_is_collab


def normalize_subtypes(keywords: set[str]) -> set[str]:
    """서브타입 키워드 정규화 (동의어 통일)."""
    return {SUBTYPE_ALIASES.get(kw, kw) for kw in keywords}


def subtype_mismatch(
    kream_keywords: set[str],
    source_keywords: set[str],
) -> set[str]:
    """소싱에만 있는 서브타입 반환. 비어있지 않으면 매칭 차단.

    kream_keywords / source_keywords는 이미 소문자 분할된 키워드 집합이어야 함.
    """
    kream_subtypes = normalize_subtypes(kream_keywords & SUBTYPE_KEYWORDS)
    source_subtypes = normalize_subtypes(source_keywords & SUBTYPE_KEYWORDS)
    return source_subtypes - kream_subtypes


def price_sanity_fails(
    kream_sell_price: int | float,
    source_price: int | float,
    multiplier: float = PRICE_SANITY_MULTIPLIER,
) -> bool:
    """크림가가 소싱가의 multiplier배 이상이면 True (오매칭 의심).

    source_price가 0 이하면 False (판단 불가, 통과).
    """
    if not source_price or source_price <= 0:
        return False
    return kream_sell_price > source_price * multiplier
