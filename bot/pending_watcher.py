from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from bot.config import Settings
from bot.models import MintCandidate
from bot.watcher import WalletWatcher

log = logging.getLogger(__name__)


class PendingWalletWatcher:
    """
    Subscribe to full pending transactions and filter watched senders.

    Alchemy supports a server-side fromAddress filter. Other providers must
    support the non-standard `newPendingTransactions, true` full-transaction
    option; hash-only feeds are deliberately ignored to avoid fetching every
    transaction on the network and exhausting the HTTP RPC quota.
    """

    def __init__(self, settings: Settings, watcher: WalletWatcher) -> None:
        self.settings = settings
        self.watcher = watcher
        self.connected = False
        self.last_error = ""
        self._seen_order: deque[str] = deque(maxlen=10_000)
        self._seen: set[str] = set()

    def subscription_params(self) -> list[Any]:
        mode = self.settings.pending_subscription
        if mode == "auto":
            mode = (
                "alchemy"
                if "alchemy.com" in self.settings.pending_ws_url.lower()
                else "full"
            )
        if mode == "alchemy":
            return [
                "alchemy_pendingTransactions",
                {
                    "fromAddress": list(self.settings.target_wallets),
                    "hashesOnly": False,
                },
            ]
        return ["newPendingTransactions", True]

    def candidate_from_result(self, result: Any) -> MintCandidate | None:
        if not isinstance(result, dict):
            # Never turn a hash-only all-network stream into HTTP lookups.
            self.last_error = (
                "WebSocket returned hashes only; provider must support full "
                "pending transactions"
            )
            return None

        tx_hash = str(result.get("hash") or "").lower()
        if not tx_hash or tx_hash in self._seen:
            return None
        if len(self._seen_order) == self._seen_order.maxlen:
            oldest = self._seen_order.popleft()
            self._seen.discard(oldest)
        self._seen_order.append(tx_hash)
        self._seen.add(tx_hash)
        return self.watcher.inspect_pending(result)

    async def _session(
        self,
        on_candidate: Callable[[MintCandidate], Awaitable[None]],
    ) -> None:
        async with websockets.connect(
            self.settings.pending_ws_url,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**22,
        ) as ws:
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": self.subscription_params(),
            }
            await ws.send(json.dumps(request))
            raw_response = await asyncio.wait_for(ws.recv(), timeout=15)
            response = json.loads(raw_response)
            if response.get("error"):
                raise RuntimeError(
                    f"Pending subscription rejected: {response['error']}"
                )
            subscription_id = response.get("result")
            if not subscription_id:
                raise RuntimeError(
                    f"Pending subscription returned no id: {response}"
                )

            self.connected = True
            self.last_error = ""
            log.info("Pending target watcher connected (%s)", subscription_id)
            async for raw_message in ws:
                message = json.loads(raw_message)
                params = message.get("params") or {}
                if params.get("subscription") != subscription_id:
                    continue
                result = params.get("result")
                candidate = self.candidate_from_result(result)
                if not isinstance(result, dict):
                    raise RuntimeError(self.last_error)
                if candidate is not None:
                    log.info(
                        "Saw pending target mint %s (%s)",
                        candidate.source_tx_hash,
                        candidate.method_hint,
                    )
                    await on_candidate(candidate)

    async def run(
        self,
        on_candidate: Callable[[MintCandidate], Awaitable[None]],
    ) -> None:
        """Reconnect forever with bounded backoff until cancelled."""
        backoff = 1.0
        while True:
            try:
                await self._session(on_candidate)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                log.warning(
                    "Pending watcher disconnected; retrying in %.0fs: %s",
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
