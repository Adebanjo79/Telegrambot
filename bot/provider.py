from __future__ import annotations

import itertools
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


def _is_garbled_rpc_reason(reason: str) -> bool:
    blob = reason.lower()
    return (
        "codec can't decode" in blob
        or "invalid start byte" in blob
        or "unicodedecodeerror" in blob
    )


class FailoverHTTPProvider(HTTPProvider):
    """
    HTTP provider that spreads work across several RPC endpoints.

    Failover mode (default): use the primary endpoint until it is rate-limited
    or unreachable, then move to the next. After a short calm period the bot
    probes the primary again and returns to it automatically (failback).

    Load-balance mode: send each request to the next endpoint in turn. Failover
    still applies per request; failback is skipped because every node is active.

    A local rate limiter keeps bursts (e.g. 15 wallets minting at once) under
    the plan's requests/second cap, and 429s are retried with backoff.
    """

    def __init__(
        self,
        endpoints: list[str],
        *,
        load_balance: bool = False,
        max_rps: float = 20.0,
        failback_after_sec: float = 30.0,
        slow_ms: float = 1500.0,
        **kwargs: Any,
    ) -> None:
        if not endpoints:
            raise ValueError("At least one RPC endpoint is required")
        self.endpoints = list(endpoints)
        self.load_balance = load_balance and len(self.endpoints) > 1
        self.max_rps = max(1.0, float(max_rps))
        self.failback_after_sec = max(5.0, float(failback_after_sec))
        self._index = 0
        self.last_failover_reason = ""
        self.last_switch_kind = ""
        self.failover_count = 0
        self.failback_count = 0
        self._left_primary_at = 0.0
        # After a garbled (non-UTF-8) primary reply, stay on backup so we
        # don't flap every failback_after_sec.
        self._hold_backup_until = 0.0
        self._garbled_hold_sec = 180.0
        self.slow_ms = max(200.0, float(slow_ms))
        # Mint copies run in threads, so all shared counters need a lock.
        self._samples: deque[tuple[float, float]] = deque(maxlen=4000)
        self._samples_lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._switch_lock = threading.Lock()
        self._next_slot = 0.0
        self._round_robin = itertools.count()
        self.endpoint_requests: dict[str, int] = {u: 0 for u in self.endpoints}
        # web3's own retry loop would delay switching nodes; we handle it here.
        kwargs.setdefault("exception_retry_configuration", None)
        super().__init__(self.endpoints[0], **kwargs)
        # One child per endpoint keeps concurrent requests from racing on a
        # single shared endpoint_uri.
        self._children = [HTTPProvider(url, **kwargs) for url in self.endpoints]

    @property
    def active_endpoint(self) -> str:
        return self.endpoints[self._index]

    @property
    def on_primary(self) -> bool:
        return self._index == 0

    @property
    def switch_count(self) -> int:
        return self.failover_count + self.failback_count

    def _pace(self) -> None:
        """Space requests so bursts stay under max_rps."""
        gap = 1.0 / self.max_rps
        with self._pace_lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + gap
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    def _rotate(self, reason: str) -> None:
        if len(self.endpoints) < 2:
            return
        with self._switch_lock:
            previous = self.active_endpoint
            leaving_primary = self._index == 0
            self._index = (self._index + 1) % len(self.endpoints)
            self.endpoint_uri = self.active_endpoint
            self.last_failover_reason = reason
            self.last_switch_kind = "failover"
            self.failover_count += 1
            if self._index != 0:
                self._left_primary_at = time.monotonic()
                if leaving_primary and _is_garbled_rpc_reason(reason):
                    hold_for = self._garbled_hold_sec
                    self._hold_backup_until = time.monotonic() + hold_for
                    self._garbled_hold_sec = min(hold_for * 2, 900.0)
                    log.warning(
                        "Primary returned garbled data; staying on backup "
                        "for %.0fs",
                        hold_for,
                    )
            log.warning(
                "Switching RPC endpoint %s -> %s (%s)",
                previous,
                self.active_endpoint,
                reason,
            )

    def maybe_failover_slow(self, slow_ms: float | None = None) -> bool:
        """
        If the active node is consistently slow, move to the next RPC.

        Public Robinhood RPC often stays up but takes 1.5s+ per call. That is
        not a 429, so normal failover never fires and mints get missed.
        """
        if self.load_balance or len(self.endpoints) < 2:
            return False
        limit = self.slow_ms if slow_ms is None else max(200.0, float(slow_ms))
        if time.monotonic() < self._hold_backup_until:
            return False
        stats = self.load_stats()
        if stats["requests"] < 3 or stats["avg_ms"] < limit:
            return False
        reason = (
            f"slow RPC ({stats['avg_ms']:.0f} ms avg, limit {limit:.0f} ms)"
        )
        self._rotate(reason)
        # Stay on backup so we don't flap back to the slow public node.
        hold_for = max(self.failback_after_sec, 120.0)
        self._hold_backup_until = time.monotonic() + hold_for
        return True

    def maybe_failback(self) -> bool:
        """
        If we are on a backup, probe the primary and return to it when healthy.

        Returns True when a failback happened.
        """
        if self.load_balance or len(self.endpoints) < 2 or self.on_primary:
            return False
        now = time.monotonic()
        if now < self._hold_backup_until:
            return False
        if now - self._left_primary_at < self.failback_after_sec:
            return False

        primary = self.endpoints[0]
        try:
            self._pace()
            started = time.monotonic()
            # Probe a full latest block — Chainstack can answer eth_blockNumber
            # while eth_getBlockByNumber still returns garbled bytes.
            response = self._children[0].make_request(
                "eth_getBlockByNumber", ["latest", True]
            )
            self._record(started, primary)
        except Exception as exc:  # noqa: BLE001 - stay on backup
            log.info("Primary still unhealthy, staying on backup: %s", exc)
            self._left_primary_at = time.monotonic()
            if _is_garbled_rpc_reason(str(exc)):
                self._hold_backup_until = (
                    time.monotonic() + self._garbled_hold_sec
                )
            return False

        probe_ms = (time.monotonic() - started) * 1000
        if probe_ms >= self.slow_ms:
            log.info(
                "Primary still slow (%.0f ms), staying on backup",
                probe_ms,
            )
            self._left_primary_at = time.monotonic()
            return False

        capacity_error = self._response_capacity_error(response)
        if capacity_error:
            log.info("Primary still rate-limited, staying on backup: %s", capacity_error)
            self._left_primary_at = time.monotonic()
            return False

        with self._switch_lock:
            if self.on_primary:
                return False
            previous = self.active_endpoint
            self._index = 0
            self.endpoint_uri = primary
            self.last_failover_reason = "primary recovered"
            self.last_switch_kind = "failback"
            self.failback_count += 1
            self._garbled_hold_sec = 180.0
            self._hold_backup_until = 0.0
            log.info("Failing back to primary RPC %s (was %s)", primary, previous)
        return True

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

    def _record(self, started: float, endpoint: str) -> None:
        finished = time.monotonic()
        with self._samples_lock:
            self._samples.append((finished, finished - started))
            self.endpoint_requests[endpoint] = (
                self.endpoint_requests.get(endpoint, 0) + 1
            )

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

    def _start_index(self) -> int:
        if not self.load_balance:
            return self._index
        return next(self._round_robin) % len(self.endpoints)

    def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        last_exc: BaseException | None = None
        # Outer loop retries capacity errors with backoff; inner loop rotates
        # across endpoints when more than one is configured.
        for attempt in range(4):
            start = self._start_index()
            for offset in range(len(self.endpoints)):
                index = (start + offset) % len(self.endpoints)
                endpoint = self.endpoints[index]
                self._pace()
                started = time.monotonic()
                try:
                    response = self._children[index].make_request(method, params)
                except Exception as exc:  # noqa: BLE001 - rotate/retry
                    self._record(started, endpoint)
                    last_exc = exc
                    if is_rpc_capacity_error(exc):
                        self._rotate(str(exc))
                        # If we have only one node, back off then retry.
                        if len(self.endpoints) == 1:
                            time.sleep(0.35 * (attempt + 1))
                        break
                    if is_endpoint_down_error(exc) or is_transient_rpc_error(exc):
                        self._rotate(str(exc))
                        continue
                    raise

                self._record(started, endpoint)
                capacity_error = self._response_capacity_error(response)
                if capacity_error:
                    last_exc = Exception(capacity_error)
                    self._rotate(capacity_error)
                    if len(self.endpoints) == 1:
                        time.sleep(0.35 * (attempt + 1))
                    break
                return response
            else:
                # Tried every endpoint without a capacity-retry break.
                break

        assert last_exc is not None
        raise last_exc
