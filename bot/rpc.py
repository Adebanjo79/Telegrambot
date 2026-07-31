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
)


def is_transient_rpc_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    blob = f"{name} {text}"
    return any(hint in blob for hint in RETRY_HINTS)


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
            log.warning(
                "RPC transient error (attempt %s/%s): %s; retrying in %.1fs",
                attempt + 1,
                retries,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last is not None
    raise last
