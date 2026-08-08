import asyncio
from types import SimpleNamespace

from bot.provider import FailoverHTTPProvider
from main import check_rpc_load

ENDPOINT = "https://node-1.example.com/key"


class FakeTracker:
    def __init__(self, **overrides):
        base = dict(
            rpc_rate_limit=25.0,
            rpc_warn_percent=80.0,
            rpc_slow_ms=1500.0,
        )
        base.update(overrides)
        self.settings = SimpleNamespace(**base)
        self.sent: list[str] = []
        self.rpc_health = "OK"
        self.rpc_per_sec = 0.0
        self.rpc_avg_ms = 0.0

    async def notify(self, text: str) -> None:
        self.sent.append(text)


def _provider_with(count: int, duration: float) -> FailoverHTTPProvider:
    provider = FailoverHTTPProvider([ENDPOINT])
    now = 0.0
    # load_stats() uses a 10s window off time.monotonic(); seed recent samples.
    import time

    now = time.monotonic()
    for i in range(count):
        provider._samples.append((now - (i * 0.001), duration))
    return provider


def test_warns_when_close_to_rate_limit():
    tracker = FakeTracker()
    # 210 requests in the 10s window = 21/s, above 80% of 25/s.
    provider = _provider_with(210, 0.05)

    warned, last = asyncio.run(check_rpc_load(tracker, provider, 100.0, False, 0.0))

    assert warned is True
    assert last == 100.0
    assert len(tracker.sent) == 1
    assert "Quota almost full" in tracker.sent[0]
    assert tracker.rpc_health == "BUSY"


def test_warns_when_rpc_is_slow():
    tracker = FakeTracker()
    provider = _provider_with(5, 2.0)

    warned, _ = asyncio.run(check_rpc_load(tracker, provider, 100.0, False, 0.0))

    assert warned is True
    assert "slow" in tracker.sent[0].lower()
    assert tracker.rpc_health == "SLOW"


def test_quiet_when_healthy_and_reports_recovery():
    tracker = FakeTracker()
    provider = _provider_with(20, 0.05)

    warned, _ = asyncio.run(check_rpc_load(tracker, provider, 100.0, False, 0.0))
    assert warned is False
    assert tracker.sent == []

    # Coming back from a warned state should send exactly one recovery note.
    warned, _ = asyncio.run(check_rpc_load(tracker, provider, 200.0, True, 100.0))
    assert warned is False
    assert len(tracker.sent) == 1
    assert "back to normal" in tracker.sent[0]
    assert tracker.rpc_health == "OK"


def test_repeat_warnings_are_throttled():
    tracker = FakeTracker()
    provider = _provider_with(210, 0.05)

    warned, last = asyncio.run(check_rpc_load(tracker, provider, 100.0, True, 90.0))
    assert warned is True
    assert last == 90.0
    assert tracker.sent == []

    # After the 180s window it reminds again.
    warned, last = asyncio.run(check_rpc_load(tracker, provider, 300.0, True, 90.0))
    assert warned is True
    assert last == 300.0
    assert len(tracker.sent) == 1
