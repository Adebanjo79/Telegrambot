import pytest

from bot.config import _normalize_rpc_url


def test_normalize_rpc_url_strips_pasted_assignment():
    url = "https://robinhood-mainnet.g.alchemy.com/v2/test"
    assert _normalize_rpc_url(f"RPC_URL={url}") == url
    assert _normalize_rpc_url(f'RPC_URLS="{url}"') == url
    assert _normalize_rpc_url(url) == url


def test_normalize_rpc_url_rejects_non_http():
    with pytest.raises(ValueError, match="http"):
        _normalize_rpc_url("wss://example.com")
