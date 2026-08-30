from bot.rpc import (
    is_endpoint_down_error,
    is_rpc_capacity_error,
    is_transient_rpc_error,
    rpc_call,
    rpc_capacity_message,
)


def test_is_transient_rpc_error():
    assert is_transient_rpc_error(ConnectionError("Remote end closed connection without response"))
    assert is_transient_rpc_error(TimeoutError("timed out"))
    assert not is_transient_rpc_error(ValueError("bad private key"))
    garbled = UnicodeDecodeError("utf-8", b"\x00\xb5", 1, 2, "invalid start byte")
    assert is_transient_rpc_error(garbled)
    assert is_endpoint_down_error(garbled)
    assert is_transient_rpc_error(
        Exception("'utf-8' codec can't decode byte 0xb5 in position 1: invalid start byte")
    )
    from web3.exceptions import BlockNotFound

    assert is_transient_rpc_error(BlockNotFound("Block with id: '0x3382e38' not found."))


def test_is_rpc_capacity_error():
    assert is_rpc_capacity_error(Exception("429 Too Many Requests"))
    assert is_rpc_capacity_error(Exception("compute units exceeded"))
    assert is_rpc_capacity_error(Exception("Monthly quota exceeded"))
    assert is_rpc_capacity_error(Exception("rate limit reached"))
    assert not is_rpc_capacity_error(ConnectionError("Connection refused"))
    assert not is_rpc_capacity_error(ValueError("bad private key"))


def test_is_endpoint_down_error():
    assert is_endpoint_down_error(Exception("401 Client Error: Unauthorized"))
    assert is_endpoint_down_error(Exception("Max retries exceeded with url"))
    assert not is_endpoint_down_error(Exception("429 Too Many Requests"))
    # A dead endpoint should still be retried/failed over, not crash the loop.
    assert is_transient_rpc_error(Exception("401 Client Error: Unauthorized"))


def test_rpc_capacity_message_mentions_full():
    msg = rpc_capacity_message(Exception("429 Too Many Requests"))
    assert "RPC FULL" in msg
    assert "429" in msg


def test_rpc_call_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("Connection aborted")
        return 42

    assert rpc_call(flaky, retries=4, base_delay=0.01) == 42
    assert calls["n"] == 3
