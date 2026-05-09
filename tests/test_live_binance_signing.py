"""BL-NEW-LIVE-HYBRID M1.5a: Binance HMAC-SHA256 signing primitive tests."""

from __future__ import annotations

from scout.live.binance_signing import sign_request


def test_sign_request_known_fixture():
    """Binance docs HMAC fixture (api.binance.com SIGNED endpoints):
    https://binance-docs.github.io/apidocs/spot/en/#signed-trade-and-user_data-endpoint-security

    apiKey: vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A
    secretKey: NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j
    queryString: symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559
    expected signature: c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71
    """
    params = {
        "symbol": "LTCBTC",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "1",
        "price": "0.1",
        "recvWindow": "5000",
        "timestamp": "1499827319559",
    }
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    signed_params, signature = sign_request(secret, params)
    assert (
        signature == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )
    assert signed_params["signature"] == signature
    for k, v in params.items():
        assert signed_params[k] == v


def test_sign_request_preserves_param_order():
    """The signature is computed from the SAME insertion order as
    params.items(). If we accidentally re-sort keys, the server's
    signature won't match. Lock the contract."""
    params = {"b": "2", "a": "1", "timestamp": "1"}
    secret = "test"
    signed_params, _ = sign_request(secret, params)
    keys = list(signed_params.keys())
    assert keys == ["b", "a", "timestamp", "signature"]


def test_sign_request_does_not_mutate_input():
    """sign_request must return a fresh dict — the caller's dict is read-only."""
    params = {"timestamp": "1"}
    secret = "test"
    sign_request(secret, params)
    assert "signature" not in params


def test_sign_request_signature_changes_with_secret():
    """Sanity: different secrets must produce different signatures."""
    params = {"timestamp": "1"}
    _, sig1 = sign_request("secret_a", params)
    _, sig2 = sign_request("secret_b", params)
    assert sig1 != sig2


def test_sign_request_handles_special_chars_via_urlencode():
    """Binance's spec uses URL-encoded query strings; sign_request must
    delegate to urlencode so e.g. `&` in a value doesn't break signing."""
    params = {"symbol": "BTCUSDT", "comment": "hello world", "timestamp": "1"}
    signed_params, signature = sign_request("test", params)
    assert signed_params["signature"] == signature
    assert signature  # non-empty
