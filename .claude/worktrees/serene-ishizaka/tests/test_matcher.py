"""모델번호 매칭 엔진 단위 테스트."""

from src.matcher import model_numbers_match, normalize_model_number


class TestNormalizeModelNumber:
    def test_basic(self):
        assert normalize_model_number("DQ8423-100") == "DQ8423-100"

    def test_lowercase(self):
        assert normalize_model_number("dq8423-100") == "DQ8423-100"

    def test_space_to_hyphen(self):
        assert normalize_model_number("DQ8423 100") == "DQ8423-100"

    def test_multiple_spaces(self):
        assert normalize_model_number("DQ 8423  100") == "DQ-8423-100"

    def test_strip_whitespace(self):
        assert normalize_model_number("  DQ8423-100  ") == "DQ8423-100"

    def test_empty(self):
        assert normalize_model_number("") == ""

    def test_multiple_hyphens(self):
        assert normalize_model_number("DQ8423--100") == "DQ8423-100"

    def test_leading_trailing_hyphen(self):
        assert normalize_model_number("-DQ8423-100-") == "DQ8423-100"


class TestModelNumbersMatch:
    def test_exact_match(self):
        assert model_numbers_match("DQ8423-100", "DQ8423-100") is True

    def test_case_insensitive(self):
        assert model_numbers_match("DQ8423-100", "dq8423-100") is True

    def test_space_vs_hyphen(self):
        assert model_numbers_match("DQ8423-100", "DQ8423 100") is True

    def test_no_separator_match(self):
        """구분자 없는 것과 있는 것이 일치."""
        assert model_numbers_match("DQ8423100", "DQ8423-100") is True

    def test_different_model(self):
        assert model_numbers_match("DQ8423-100", "DQ8423-200") is False

    def test_partial_no_match(self):
        """부분 일치는 매칭하지 않음."""
        assert model_numbers_match("DQ8423-100", "DQ8423") is False

    def test_empty_no_match(self):
        assert model_numbers_match("", "DQ8423-100") is False
        assert model_numbers_match("DQ8423-100", "") is False
        assert model_numbers_match("", "") is False

    def test_completely_different(self):
        assert model_numbers_match("DQ8423-100", "FJ4188-100") is False

    def test_adidas_style(self):
        """아디다스 스타일 모델번호."""
        assert model_numbers_match("GY1056", "GY1056") is True
        assert model_numbers_match("GY1056", "gy1056") is True

    def test_newbalance_style(self):
        """뉴발란스 스타일."""
        assert model_numbers_match("BB550PWB", "BB550PWB") is True
        assert model_numbers_match("BB550PWB", "bb550pwb") is True

    def test_nike_dunk_variants(self):
        """나이키 덩크 색상 코드가 다르면 불일치."""
        assert model_numbers_match("DQ8423-100", "DQ8423-001") is False
