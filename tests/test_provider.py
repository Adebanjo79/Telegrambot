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


def test_rotates_when_rpc_returns_invalid_utf8():
    provider = FailoverHTTPProvider([PRIMARY, BACKUP])

    def fake_make_request(self, method, params):
        if self.endpoint_uri == PRIMARY:
            raise UnicodeDecodeError(
                "utf-8", b"\x00\xb5", 1, 2, "invalid start byte"
            )
        return {"jsonrpc": "2.0", "id": 1, "result": "0x4"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        result = provider.make_request("eth_blockNumber", [])

    assert result["result"] == "0x4"
    assert provider.active_endpoint == BACKUP


def test_load_balance_alternates_between_endpoints():
    provider = FailoverHTTPProvider([PRIMARY, BACKUP], load_balance=True)
    seen: list[str] = []

    def fake_make_request(self, method, params):
        seen.append(self.endpoint_uri)
        return {"jsonrpc": "2.0", "id": 1, "result": "0x1"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        for _ in range(4):
            provider.make_request("eth_blockNumber", [])

    assert seen == [PRIMARY, BACKUP, PRIMARY, BACKUP]
    assert provider.failover_count == 0
    assert provider.endpoint_requests == {PRIMARY: 2, BACKUP: 2}


def test_load_balance_still_fails_over_when_one_node_is_full():
    provider = FailoverHTTPProvider([PRIMARY, BACKUP], load_balance=True)

    def fake_make_request(self, method, params):
        if self.endpoint_uri == PRIMARY:
            raise Exception("429 Too Many Requests")
        return {"jsonrpc": "2.0", "id": 1, "result": "0x9"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        result = provider.make_request("eth_blockNumber", [])

    assert result["result"] == "0x9"
    assert provider.failover_count == 1


def test_failback_returns_to_primary_when_healthy():
    provider = FailoverHTTPProvider(
        [PRIMARY, BACKUP], max_rps=1000, failback_after_sec=5.0
    )
    calls: list[str] = []

    def fake_make_request(self, method, params):
        calls.append(self.endpoint_uri)
        if self.endpoint_uri == PRIMARY and len(calls) == 1:
            raise Exception("429 Too Many Requests")
        return {"jsonrpc": "2.0", "id": 1, "result": "0x1"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        provider.make_request("eth_blockNumber", [])
        assert provider.active_endpoint == BACKUP
        # Pretend we've been on the backup long enough.
        provider._left_primary_at = 0.0
        assert provider.maybe_failback() is True

    assert provider.active_endpoint == PRIMARY
    assert provider.failback_count == 1
    assert provider.last_switch_kind == "failback"


def test_failback_stays_on_backup_while_primary_is_full():
    provider = FailoverHTTPProvider(
        [PRIMARY, BACKUP], max_rps=1000, failback_after_sec=5.0
    )
    provider._index = 1
    provider._left_primary_at = 0.0

    def fake_make_request(self, method, params):
        if self.endpoint_uri == PRIMARY:
            raise Exception("429 Too Many Requests")
        return {"jsonrpc": "2.0", "id": 1, "result": "0x1"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        assert provider.maybe_failback() is False

    assert provider.active_endpoint == BACKUP
    assert provider.failback_count == 0


def test_retries_429_on_single_endpoint():
    provider = FailoverHTTPProvider([PRIMARY], max_rps=1000)
    calls = {"n": 0}

    def fake_make_request(self, method, params):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("429 Client Error: Too Many Requests for url: " + PRIMARY)
        return {"jsonrpc": "2.0", "id": 1, "result": "0xabc"}

    with patch(
        "web3.providers.rpc.HTTPProvider.make_request", new=fake_make_request
    ):
        result = provider.make_request("eth_blockNumber", [])

    assert result["result"] == "0xabc"
    assert calls["n"] == 3


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
