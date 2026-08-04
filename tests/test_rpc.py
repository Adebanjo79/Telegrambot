from bot.rpc import is_transient_rpc_error, rpc_call


def test_is_transient_rpc_error():
    assert is_transient_rpc_error(ConnectionError("Remote end closed connection without response"))
    assert is_transient_rpc_error(TimeoutError("timed out"))
    assert not is_transient_rpc_error(ValueError("bad private key"))


def test_rpc_call_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("Connection aborted")
        return 42

    assert rpc_call(flaky, retries=4, base_delay=0.01) == 42
    assert calls["n"] == 3
