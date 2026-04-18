"""transaction_history 의 asks/bids/sales 노드가 list 로 올 때 regression.

과거 버그: `th.get("asks", {}).get("items", [])` 패턴 — kream API 가 해당
노드를 list 로 직접 내려보낼 때 `'list' object has no attribute 'get'` 예외 →
snapshot_light 파싱 실패 → no_profit 기록. 2026-04-18 `_th_items` 헬퍼로 수정.
"""
from __future__ import annotations

from src.crawlers.kream import KreamCrawler


def test_th_items_dict_wrapped():
    assert KreamCrawler._th_items({"items": [1, 2, 3]}) == [1, 2, 3]


def test_th_items_bare_list():
    assert KreamCrawler._th_items([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]


def test_th_items_none():
    assert KreamCrawler._th_items(None) == []


def test_th_items_empty_dict():
    assert KreamCrawler._th_items({}) == []


def test_th_items_dict_items_not_list():
    assert KreamCrawler._th_items({"items": "string"}) == []


def test_find_prices_in_pinia_handles_list_shape_asks():
    """asks 가 list 로 직접 내려와도 예외 없이 빈 결과 반환."""
    data = {
        "pinia": {
            "transactionHistorySummary": {
                "previousItem": {
                    "meta": {
                        "transaction_history": {
                            "asks": [],  # list (not {"items": []})
                            "bids": [],
                            "sales": [],
                        }
                    }
                }
            }
        }
    }
    c = KreamCrawler.__new__(KreamCrawler)
    result = c._find_prices_in_pinia(data, "12345")
    assert result == []


def test_find_trades_in_pinia_handles_list_shape_sales():
    data = {
        "pinia": {
            "transactionHistorySummary": {
                "previousItem": {
                    "meta": {
                        "transaction_history": {
                            "sales": [],
                        }
                    }
                }
            }
        }
    }
    c = KreamCrawler.__new__(KreamCrawler)
    result = c._find_trades_in_pinia(data, "12345")
    assert result is None


def test_dig_stops_on_list_intermediate():
    # Why: pinia.get("productDetail") 가 list 를 돌려주면 .get("...") 가
    # AttributeError. _dig 은 각 단계 isinstance(dict) 확인으로 None 반환.
    assert KreamCrawler._dig({"a": [1, 2, 3]}, "a", "b") is None
    assert KreamCrawler._dig({"a": {"b": "x"}}, "a", "b") == "x"
    assert KreamCrawler._dig({"a": {"b": {"c": 1}}}, "a", "b", "c") == 1
    assert KreamCrawler._dig({}, "a") is None
    assert KreamCrawler._dig(None, "a") is None


def test_find_product_in_pinia_handles_list_productdetail():
    """productDetail 가 list 일 때 AttributeError 없이 None 반환."""
    data = {"pinia": {"productDetail": [{"x": 1}]}}
    c = KreamCrawler.__new__(KreamCrawler)
    assert c._find_product_in_pinia(data, "12345") is None


def test_find_prices_in_pinia_handles_list_transactionhistorysummary():
    data = {"pinia": {"transactionHistorySummary": [{"x": 1}]}}
    c = KreamCrawler.__new__(KreamCrawler)
    assert c._find_prices_in_pinia(data, "12345") == []


def test_find_trades_in_pinia_handles_list_transactionhistorysummary():
    data = {"pinia": {"transactionHistorySummary": [{"x": 1}]}}
    c = KreamCrawler.__new__(KreamCrawler)
    assert c._find_trades_in_pinia(data, "12345") is None
