"""매칭 가드 공통 모듈 단위 테스트."""

from src.core.matching_guards import (
    COLLAB_KEYWORDS,
    SUBTYPE_KEYWORDS,
    collab_match_fails,
    is_collab,
    normalize_subtypes,
    price_sanity_fails,
    subtype_mismatch,
)


class TestIsCollab:
    def test_detects_english_collab(self):
        assert is_collab("Air Jordan 1 Travis Scott Mocha")
        assert is_collab("Nike Dunk Low SUPREME Box Logo")

    def test_detects_korean_collab(self):
        assert is_collab("에어 조던 1 트래비스 스캇")
        assert is_collab("조던 디올")

    def test_regular_product_not_collab(self):
        assert not is_collab("Nike Air Force 1 '07 White")
        assert not is_collab("에어 포스 1 로우")

    def test_empty_or_none(self):
        assert not is_collab("")
        assert not is_collab(None)  # type: ignore


class TestCollabMatchFails:
    def test_kream_collab_source_regular_blocks(self):
        # 차단: 크림=콜라보, 소싱=일반
        assert collab_match_fails(
            "Air Jordan 1 Travis Scott",
            "Air Jordan 1 Retro High",
        )

    def test_both_collab_passes(self):
        assert not collab_match_fails(
            "Air Jordan 1 Travis Scott",
            "Jordan 1 Travis Scott Mocha",
        )

    def test_both_regular_passes(self):
        assert not collab_match_fails(
            "Air Jordan 1 Chicago",
            "Air Jordan 1 High Chicago",
        )

    def test_kream_regular_source_collab_passes(self):
        # 통과: 크림=일반, 소싱=콜라보 (소싱이 더 상세할 수 있음)
        assert not collab_match_fails(
            "Air Jordan 1 Retro High",
            "Air Jordan 1 Travis Scott",
        )


class TestSubtypeMismatch:
    def test_source_only_subtype_detected(self):
        # 소싱=PRM, 크림=일반 → 차단 대상 반환
        kream = {"air", "force", "1", "07"}
        source = {"air", "force", "1", "prm"}
        result = subtype_mismatch(kream, source)
        assert "premium" in result

    def test_kream_only_subtype_allowed(self):
        # 크림에만 서브타입 있음 → 차단 X
        kream = {"air", "jordan", "1", "se"}
        source = {"air", "jordan", "1"}
        assert subtype_mismatch(kream, source) == set()

    def test_both_same_subtype_allowed(self):
        kream = {"air", "jordan", "1", "retro"}
        source = {"air", "jordan", "1", "retro"}
        assert subtype_mismatch(kream, source) == set()

    def test_prm_premium_alias(self):
        kream = {"air", "force", "1", "premium"}
        source = {"air", "force", "1", "prm"}
        assert subtype_mismatch(kream, source) == set()


class TestNormalizeSubtypes:
    def test_prm_to_premium(self):
        assert normalize_subtypes({"prm"}) == {"premium"}

    def test_passthrough(self):
        assert normalize_subtypes({"retro", "qs"}) == {"retro", "qs"}


class TestPriceSanityFails:
    def test_5x_blocks(self):
        assert price_sanity_fails(600_000, 100_000)

    def test_under_5x_passes(self):
        assert not price_sanity_fails(400_000, 100_000)

    def test_exact_5x_passes(self):
        # 5.0 × source = 500000, sell=500000이면 통과 (엄격 초과만 차단)
        assert not price_sanity_fails(500_000, 100_000)

    def test_zero_source_passes(self):
        # 판단 불가 → 통과
        assert not price_sanity_fails(500_000, 0)

    def test_custom_multiplier(self):
        assert price_sanity_fails(300_000, 100_000, multiplier=2.0)
        assert not price_sanity_fails(200_000, 100_000, multiplier=2.0)


class TestConstants:
    def test_collab_keywords_has_major_brands(self):
        assert "travis scott" in COLLAB_KEYWORDS
        assert "트래비스 스캇" in COLLAB_KEYWORDS
        assert "dior" in COLLAB_KEYWORDS

    def test_subtype_keywords_has_major_suffixes(self):
        assert "prm" in SUBTYPE_KEYWORDS
        assert "qs" in SUBTYPE_KEYWORDS
        assert "se" in SUBTYPE_KEYWORDS
