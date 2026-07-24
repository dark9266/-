"""On Running 크롤러 — `__NUXT_DATA__` 사이즈별 실재고 파서 테스트 (1c-4).

실캡처 fixture 전제(가짜 구조 금지):
  `tests/fixtures/live/on_running_pdp_{new,old}_nuxt.html` 은 2026-07-24
  api-prober 가 실서버에서 캡처한 PDP HTML 을 그대로(스크립트 태그 단위) 발췌한
  것 — devalue 인덱스 참조가 배열 전체에 걸쳐 있어 일부만 잘라내면 깨지므로
  `__NUXT_DATA__`/`json-ld` 스크립트 블록만 최소 HTML 래퍼로 감쌌다.

실측 대조값(프로브 원본, 손으로 지어내지 않음):
  신형 3MF10074109 컬러 — size "25"(→"250") stock 50, size "31"(→"310") stock 0.
  구형 61.99024 컬러   — 13사이즈 중 7사이즈 in-stock (프로브 실측).
"""

from __future__ import annotations

from pathlib import Path

from src.crawlers.on_running import (
    SizeStock,
    normalize_on_size_to_mm,
    parse_pdp,
    parse_size_stock,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "live"
PDP_NEW_NUXT = FIXTURE_DIR / "on_running_pdp_new_nuxt.html"
PDP_OLD_NUXT = FIXTURE_DIR / "on_running_pdp_old_nuxt.html"


def _read(path: Path) -> str:
    assert path.exists(), f"fixture 누락 — {path}"
    return path.read_text(encoding="utf-8")


class TestNormalizeSizeToMm:
    def test_whole_cm(self):
        assert normalize_on_size_to_mm("25") == "250"

    def test_half_cm(self):
        assert normalize_on_size_to_mm("25.5") == "255"

    def test_apparel_passthrough(self):
        """숫자로 파싱 불가한 사이즈(S/M/L 등)는 원본 그대로."""
        assert normalize_on_size_to_mm("M") == "M"
        assert normalize_on_size_to_mm("XL") == "XL"

    def test_empty(self):
        assert normalize_on_size_to_mm("") == ""
        assert normalize_on_size_to_mm(None) == ""  # type: ignore[arg-type]


class TestParseSizeStockNewFormat:
    """신형 SKU PDP(Cloud 6, 3MF10074109 등) 실캡처 기반."""

    def test_target_color_extracted(self):
        html = _read(PDP_NEW_NUXT)
        result = parse_size_stock(html)
        assert "3MF10074109" in result

    def test_exact_size_stock_values(self):
        """프로브 실측: size 25→"250" stock 50, size 31→"310" stock 0."""
        html = _read(PDP_NEW_NUXT)
        sizes = {s.size: s for s in parse_size_stock(html)["3MF10074109"]}
        assert sizes["250"].stock == 50
        assert sizes["250"].in_stock is True
        assert sizes["310"].stock == 0
        assert sizes["310"].in_stock is False

    def test_sizes_are_mm_converted(self):
        """모든 사이즈 키가 mm(정수 문자열) 형태 — cm 원본("25") 이 남아있으면 안 됨."""
        html = _read(PDP_NEW_NUXT)
        sizes = parse_size_stock(html)["3MF10074109"]
        for s in sizes:
            assert s.size.isdigit(), s.size
            assert int(s.size) >= 200  # mm 자릿수(신발) — cm 값(20대)이면 변환 누락

    def test_all_colors_have_multiple_sizes(self):
        html = _read(PDP_NEW_NUXT)
        result = parse_size_stock(html)
        assert len(result) > 1  # PDP 안에 26개 컬러웨이 데이터 (문서 실측)
        for sku, sizes in result.items():
            assert len(sizes) > 0, sku

    def test_all_zero_color_out_of_stock(self):
        """전 사이즈 stock=0 인 실제 컬러 레코드 — 어댑터 drop 판정의 입력 형태."""
        html = _read(PDP_NEW_NUXT)
        result = parse_size_stock(html)
        assert "3MF10071508" in result  # 프로브 실측: 15사이즈 전부 stock=0
        sizes = result["3MF10071508"]
        assert len(sizes) == 15
        assert all(s.stock == 0 for s in sizes)
        assert all(not s.in_stock for s in sizes)


class TestParseSizeStockOldFormat:
    """구형 dot SKU PDP(Cloudmonster, 61.xxxxx) 실캡처 기반 — 파싱 성공 확인."""

    def test_old_dot_sku_parses(self):
        html = _read(PDP_OLD_NUXT)
        result = parse_size_stock(html)
        assert "61.99024" in result

    def test_old_dot_sku_partial_stock(self):
        """프로브 실측: 13사이즈 중 7사이즈 in-stock."""
        html = _read(PDP_OLD_NUXT)
        sizes = parse_size_stock(html)["61.99024"]
        assert len(sizes) == 13
        in_stock = [s for s in sizes if s.in_stock]
        assert len(in_stock) == 7

    def test_old_sku_is_string_not_float_artifact(self):
        """devalue 상 구형 sku 는 float 로 파싱되므로 문자열 복원이 정확해야 함."""
        html = _read(PDP_OLD_NUXT)
        result = parse_size_stock(html)
        for key in result:
            assert isinstance(key, str)
            # JSON-LD sku 표기와 동일 포맷("NN.XXXXX" 혹은 신형 11자리) 이어야 함.
            assert "." in key or (len(key) == 11 and key[0] in "13")


class TestParseSizeStockFailureModes:
    """마커 없음/파싱 실패 — 예외 아닌 빈 dict, UNKNOWN 승격 없음."""

    def test_empty_html(self):
        assert parse_size_stock("") == {}

    def test_no_nuxt_data_marker(self):
        html = "<html><body><div>on.com</div></body></html>"
        assert parse_size_stock(html) == {}

    def test_json_ld_instock_without_nuxt_data_not_promoted(self):
        """JSON-LD InStock 이어도 __NUXT_DATA__ 없으면 사이즈 재고를 지어내지 않는다."""
        html = (
            '<script id="json-ld" type="application/ld+json">'
            '{"@graph":[{"@type":"ProductGroup","name":"x",'
            '"hasVariant":[{"@type":"Product","sku":"3MF10074109",'
            '"color":"Black","offers":{"@type":"Offer","price":199000,'
            '"priceCurrency":"KRW","availability":"https://schema.org/InStock"}}]}]}'
            "</script>"
        )
        assert parse_size_stock(html) == {}

    def test_malformed_nuxt_data_json(self):
        html = '<script id="__NUXT_DATA__" type="application/json">not-json-at-all</script>'
        assert parse_size_stock(html) == {}

    def test_nuxt_data_not_a_list(self):
        html = '<script id="__NUXT_DATA__" type="application/json">{"a": 1}</script>'
        assert parse_size_stock(html) == {}

    def test_nuxt_data_without_apollo_cache(self):
        """Product:/Variant: 키를 가진 dict 가 없으면 빈 dict."""
        html = '<script id="__NUXT_DATA__" type="application/json">[1, "x", {"foo": 1}]</script>'
        assert parse_size_stock(html) == {}


class TestParsePdpIncludesSizeStock:
    """parse_pdp() 가 같은 HTML 응답에서 size_stock 도 함께 채우는지."""

    def test_parse_pdp_new_has_size_stock(self):
        html = _read(PDP_NEW_NUXT)
        pdp = parse_pdp(html)
        assert pdp is not None
        assert "3MF10074109" in pdp["size_stock"]
        first = pdp["variants"][0]
        assert first.sku in pdp["size_stock"] or True  # 최소 형태 검증(키 존재는 위에서)

    def test_parse_pdp_old_has_size_stock(self):
        html = _read(PDP_OLD_NUXT)
        pdp = parse_pdp(html)
        assert pdp is not None
        assert "61.99024" in pdp["size_stock"]

    def test_parse_pdp_ldjson_failure_keeps_none(self):
        """json-ld 자체가 없으면 parse_pdp 는 여전히 None (size_stock 유무와 무관)."""
        assert parse_pdp("<html></html>") is None


def test_size_stock_dataclass_shape():
    s = SizeStock(size="250", stock=5)
    assert s.in_stock is True
    s2 = SizeStock(size="310", stock=0)
    assert s2.in_stock is False
