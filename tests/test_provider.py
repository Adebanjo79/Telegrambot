from unittest.mock import patch

from bot.provider import FailoverHTTPProvider

PRIMARY = "https://node-1.example.com/key"
BACKUP = "https://node-2.example.com/key"


def test_rotates_to_backup_when_primary_is_rate_limited():
    provider = FailoverHTTPProvider([PRIMARY, BACKUP])
    seen: list[str] = []

    def fake_make_request(self, method, params):
        seen.append(self.endpoint_uri)
        if self.endpoint_uri == PRIMARY:
            raise Exception("429 Too Many Requests")
        return {"jsonrpc": "2.0", "id": 1, "result": "0x1"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        result = provider.make_request("eth_blockNumber", [])

    assert result["result"] == "0x1"
    assert seen == [PRIMARY, BACKUP]
    assert provider.active_endpoint == BACKUP


def test_rotates_on_json_rpc_quota_error_response():
    provider = FailoverHTTPProvider([PRIMARY, BACKUP])

    def fake_make_request(self, method, params):
        if self.endpoint_uri == PRIMARY:
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32005, "message": "monthly quota exceeded"},
            }
        return {"jsonrpc": "2.0", "id": 1, "result": "0x2"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        result = provider.make_request("eth_blockNumber", [])

    assert result["result"] == "0x2"
    assert provider.active_endpoint == BACKUP


def test_rotates_when_endpoint_key_is_revoked():
    provider = FailoverHTTPProvider([PRIMARY, BACKUP])

    def fake_make_request(self, method, params):
        if self.endpoint_uri == PRIMARY:
            raise Exception("401 Client Error: Unauthorized for url: " + PRIMARY)
        return {"jsonrpc": "2.0", "id": 1, "result": "0x3"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        result = provider.make_request("eth_blockNumber", [])

    assert result["result"] == "0x3"
    assert provider.active_endpoint == BACKUP
    assert provider.failover_count == 1


def test_non_transient_error_is_raised_without_rotation():
    provider = FailoverHTTPProvider([PRIMARY, BACKUP])

    def fake_make_request(self, method, params):
        raise ValueError("execution reverted")

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        try:
            provider.make_request("eth_call", [])
        except ValueError:
            pass
        else:  # pragma: no cover - guard
            raise AssertionError("expected ValueError")

    assert provider.active_endpoint == PRIMARY
