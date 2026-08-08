from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# Transient network / RPC failures that should be retried.
RETRY_HINTS = (
    "remotedisconnected",
    "remote end closed",
    "connection aborted",
    "connection reset",
    "connection refused",
    "connect: connection refused",
    "timed out",
    "timeout",
    "deadline exceeded",
    "context deadline",
    "temporarily unavailable",
    "503",
    "502",
    "429",
    "too many requests",
    "broken pipe",
    "not found",
    "block with id",
    "header not found",
    "-32000",
    # DNS / socket / TLS failures reaching the node
    "max retries exceeded",
    "failed to resolve",
    "name or service not known",
    "nameresolutionerror",
    "connectionerror",
    "newconnectionerror",
    "connection error",
    "ssl",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
)

# Provider quota / rate-limit / "RPC is full" signals.
CAPACITY_HINTS = (
    "429",
    "too many requests",
    "rate limit",
    "ratelimit",
    "rate-limit",
    "throughput",
    "compute unit",
    "compute units",
    "cu limit",
    "cu exceeded",
    "monthly quota",
    "quota exceeded",
    "quota limit",
    "capacity",
    "over capacity",
    "limit exceeded",
    "request limit",
    "calls limit",
    "payment required",
    "402",
    "out of credits",
    "insufficient credits",
    "credits exhausted",
    "free tier",
)


def _error_blob(exc: BaseException) -> str:
    return f"{type(exc).__name__} {exc}".lower()


def is_rpc_capacity_error(exc: BaseException) -> bool:
    """True when the RPC provider is rate-limited, over quota, or 'full'."""
    blob = _error_blob(exc)
    return any(hint in blob for hint in CAPACITY_HINTS)


def is_transient_rpc_error(exc: BaseException) -> bool:
    blob = _error_blob(exc)
    if is_rpc_capacity_error(exc):
        return True
    return any(hint in blob for hint in RETRY_HINTS)


def rpc_capacity_message(exc: BaseException) -> str:
    return (
        "🚨 RPC FULL / rate-limited\n"
        "Your RPC provider is rejecting requests "
        "(quota, compute units, or rate limit).\n"
        "Bot is retrying, but mints may be missed until this clears.\n"
        f"Detail: {exc}"
    )


def rpc_call(fn: Callable[[], T], *, retries: int = 4, base_delay: float = 0.75) -> T:
    """Call an RPC function with simple exponential backoff on transient errors."""
    last: BaseException | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - surface after retries
            last = exc
            if not is_transient_rpc_error(exc) or attempt == retries - 1:
                raise
            delay = base_delay * (2**attempt)
            kind = "FULL/rate-limit" if is_rpc_capacity_error(exc) else "transient"
            log.warning(
                "RPC %s error (attempt %s/%s): %s; retrying in %.1fs",
                kind,
                attempt + 1,
                retries,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last is not None
    raise last
