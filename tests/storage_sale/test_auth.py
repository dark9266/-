def test_mock_session_returns_headers():
    from src.storage_sale.auth import MockSession

    s = MockSession()
    h = s.get_headers()
    assert "X-KREAM-DEVICE-ID" in h
    assert "X-KREAM-WEB-REQUEST-SECRET" in h
