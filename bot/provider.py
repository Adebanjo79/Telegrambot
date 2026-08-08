from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from web3 import HTTPProvider
from web3.types import RPCEndpoint, RPCResponse

from bot.rpc import (
    is_endpoint_down_error,
    is_rpc_capacity_error,
    is_transient_rpc_error,
)

log = logging.getLogger(__name__)


class FailoverHTTPProvider(HTTPProvider):
    """
    HTTP provider that rotates across several RPC endpoints.

    When one node is rate-limited/full or unreachable, the next endpoint is
    tried for the same request and becomes the new primary.
    """

    def __init__(self, endpoints: list[str], **kwargs: Any) -> None:
        if not endpoints:
            raise ValueError("At least one RPC endpoint is required")
        self.endpoints = list(endpoints)
        self._index = 0
        self.last_failover_reason = ""
        self.failover_count = 0
        # (finished_at, duration_seconds) for recent calls; mint copies run in
        # threads, so this is shared state.
        self._samples: deque[tuple[float, float]] = deque(maxlen=4000)
        self._samples_lock = threading.Lock()
        # web3's own retry loop would delay switching nodes; we handle it here.
        kwargs.setdefault("exception_retry_configuration", None)
        super().__init__(self.endpoints[0], **kwargs)

    @property
    def active_endpoint(self) -> str:
        return self.endpoints[self._index]

    def _rotate(self, reason: str) -> None:
        if len(self.endpoints) < 2:
            return
        previous = self.active_endpoint
        self._index = (self._index + 1) % len(self.endpoints)
        self.last_failover_reason = reason
        self.failover_count += 1
        log.warning(
            "Switching RPC endpoint %s -> %s (%s)",
            previous,
            self.active_endpoint,
            reason,
        )

    @staticmethod
    def _response_capacity_error(response: RPCResponse) -> str:
        error = response.get("error") if isinstance(response, dict) else None
        if not error:
            return ""
        message = error.get("message") if isinstance(error, dict) else str(error)
        if not message:
            return ""
        probe = Exception(message)
        if is_rpc_capacity_error(probe):
            return str(message)
        return ""

    def _record(self, started: float) -> None:
        finished = time.monotonic()
        with self._samples_lock:
            self._samples.append((finished, finished - started))

    def load_stats(self, window_sec: float = 10.0) -> dict[str, float]:
        """Requests/second and latency over the recent window."""
        cutoff = time.monotonic() - window_sec
        with self._samples_lock:
            recent = [(t, d) for t, d in self._samples if t >= cutoff]
        if not recent:
            return {"requests": 0.0, "per_sec": 0.0, "avg_ms": 0.0, "max_ms": 0.0}
        durations = [d for _, d in recent]
        return {
            "requests": float(len(recent)),
            "per_sec": len(recent) / window_sec,
            "avg_ms": sum(durations) / len(durations) * 1000,
            "max_ms": max(durations) * 1000,
        }

    def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        last_exc: BaseException | None = None
        for _ in range(len(self.endpoints)):
            self.endpoint_uri = self.active_endpoint
            started = time.monotonic()
            try:
                response = super().make_request(method, params)
            except Exception as exc:  # noqa: BLE001 - rotate then re-raise
                self._record(started)
                last_exc = exc
                if not (
                    is_rpc_capacity_error(exc)
                    or is_endpoint_down_error(exc)
                    or is_transient_rpc_error(exc)
                ):
                    raise
                self._rotate(str(exc))
                continue

            self._record(started)
            capacity_error = self._response_capacity_error(response)
            if capacity_error:
                last_exc = Exception(capacity_error)
                self._rotate(capacity_error)
                continue
            return response

        assert last_exc is not None
        raise last_exc
