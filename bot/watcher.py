from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from web3 import Web3

from bot.config import Settings
from bot.models import MintCandidate
from bot.rpc import is_transient_rpc_error, rpc_call

log = logging.getLogger(__name__)

# OpenSea SeaDrop (same address on many chains, including Robinhood Chain)
SEADROP = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"

# Common public mint / claim selectors (first 4 bytes of keccak of the signature).
# Intentionally excludes approvals / transfers / marketplace selectors.
MINT_SELECTORS: dict[str, str] = {
    # SeaDrop — critical so we still catch mints if receipt fetch blips
    "0x161ac21f": "SeaDrop mintPublic",
    "0x8d7f0ad4": "SeaDrop mintAllowList",
    "0x4b61cd6f": "SeaDrop mintSigned",
    "0xa0712d68": "mint(uint256)",
    "0x40c10f19": "mint(address,uint256)",
    "0x6a627842": "mint(address)",
    "0x94bf804d": "mint(uint256,address)",
    "0x1249c58b": "mint()",
    "0x449a52f8": "mintTo(address,uint256)",
    "0x2db11544": "publicMint(uint256)",
    "0x9b4f3af5": "publicMint()",
    "0x4e71d92d": "claim()",
    "0x84bb1e42": "claim(uint256,...)",
    "0x1e83409a": "claim(address)",
    "0xd37c353b": "mintPublic(uint256)",
    "0xa8a41c70": "mintPublic(uint256,address)",
    "0xefef39a1": "purchase(uint256)",
}


def _topic0(signature: str) -> str:
    raw = Web3.keccak(text=signature).hex()
    return raw if raw.startswith("0x") else "0x" + raw


TRANSFER_TOPIC = _topic0("Transfer(address,address,uint256)")
TRANSFER_SINGLE_TOPIC = _topic0(
    "TransferSingle(address,address,address,uint256,uint256)"
)
ZERO_ADDR = "0x0000000000000000000000000000000000000000"


def _normalize_hex(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.hex()
    text = value if isinstance(value, str) else str(value)
    if not text.startswith("0x"):
        text = "0x" + text
    return text.lower()


def _topic_address(topic: str) -> str:
    topic = _normalize_hex(topic)
    if len(topic) >= 42:
        return "0x" + topic[-40:]
    return topic


def _tx_get(tx: Any, key: str, default: Any = None) -> Any:
    if isinstance(tx, dict):
        return tx.get(key, default)
    try:
        return tx[key]
    except Exception:
        return getattr(tx, key, default)


def _quantity_int(value: Any) -> int:
    """Decode Web3 ints and raw JSON-RPC hex quantities."""
    if isinstance(value, str):
        return int(value, 0)
    return int(value or 0)


class WalletWatcher:
    """Poll Robinhood Chain for mint-like txs from watched wallets."""

    def __init__(self, settings: Settings, w3: Web3) -> None:
        self.settings = settings
        self.w3 = w3
        self.enabled = True
        self.last_block = 0
        self.target_set = {addr.lower() for addr in settings.target_wallets}
        self.lag_blocks = 0

    def bootstrap(self) -> int:
        head = rpc_call(lambda: self.w3.eth.block_number)
        self.last_block = head
        return head

    def poll(self) -> list[MintCandidate]:
        """
        Scan every new block from last_block+1 onward.

        Never jumps ahead / drops blocks. If the bot is behind, it processes
        up to max_catchup_blocks per poll and resumes next cycle.
        """
        if not self.enabled:
            return []

        head = rpc_call(lambda: self.w3.eth.block_number)
        if head <= self.last_block:
            self.lag_blocks = 0
            return []

        start = self.last_block + 1
        behind = head - self.last_block
        self.lag_blocks = behind
        end = min(head, self.last_block + self.settings.max_catchup_blocks)
        if behind > self.settings.max_catchup_blocks:
            log.warning(
                "Watcher is %s blocks behind; scanning %s..%s this cycle "
                "(will continue next poll — no blocks skipped)",
                behind,
                start,
                end,
            )

        found: list[MintCandidate] = []
        last_ok = self.last_block

        for block_number in range(start, end + 1):
            try:
                block = rpc_call(
                    lambda n=block_number: self.w3.eth.get_block(n, full_transactions=True)
                )
            except Exception as exc:
                msg = str(exc).lower()
                log.warning("Failed to fetch block %s: %s", block_number, exc)
                # Keep progress up to the last successful block so we retry later.
                self.last_block = last_ok
                # Missing/reorged blocks are common on fast L2 RPCs — retry next poll.
                if "not found" in msg or "header not found" in msg or "block with id" in msg:
                    return found
                raise

            for tx in block.transactions:
                candidate = self._inspect_tx(tx, block_number)
                if candidate:
                    found.append(candidate)
            last_ok = block_number

        self.last_block = last_ok
        return found

    def inspect_pending(self, tx: Any) -> MintCandidate | None:
        """Inspect a full pending transaction without waiting for a receipt."""
        return self._inspect_tx(tx, block_number=0, pending=True)

    def _inspect_tx(
        self, tx: Any, block_number: int, pending: bool = False
    ) -> MintCandidate | None:
        tx_from = _normalize_hex(_tx_get(tx, "from"))
        if tx_from not in self.target_set:
            return None

        to_addr = _tx_get(tx, "to")
        if not to_addr:
            return None  # contract creation — skip

        input_data = _normalize_hex(_tx_get(tx, "input") or _tx_get(tx, "data") or "0x")
        if input_data in {"0x", "0x0", ""}:
            return None

        value_wei = _quantity_int(_tx_get(tx, "value"))
        value_eth = Decimal(value_wei) / Decimal(10**18)
        if self.settings.free_mints_only and value_wei > 0:
            log.info(
                "Skip paid tx %s value=%s ETH",
                _normalize_hex(_tx_get(tx, "hash")),
                value_eth,
            )
            return None

        selector = input_data[:10] if len(input_data) >= 10 else input_data
        method_hint = MINT_SELECTORS.get(selector)
        looks_like_mint = method_hint is not None
        to_norm = _normalize_hex(to_addr)

        # SeaDrop calls are always mint candidates even before receipt lands.
        is_seadrop_mint = to_norm == SEADROP and selector in MINT_SELECTORS
        if is_seadrop_mint:
            looks_like_mint = True

        tx_hash = _normalize_hex(_tx_get(tx, "hash"))
        receipt_mint = False
        receipt_error: BaseException | None = None
        # Known SeaDrop mint selectors: skip receipt round-trips so we can
        # copy before a short public window closes.
        if not is_seadrop_mint and not pending:
            for attempt in range(3):
                try:
                    receipt = rpc_call(
                        lambda: self.w3.eth.get_transaction_receipt(tx_hash)
                    )
                    receipt_mint = self._receipt_shows_mint(receipt)
                    if receipt_mint:
                        looks_like_mint = True
                        method_hint = method_hint or "NFT mint (Transfer from 0x0)"
                    receipt_error = None
                    break
                except Exception as exc:
                    receipt_error = exc
                    if not is_transient_rpc_error(exc):
                        break
                    log.warning(
                        "Receipt fetch retry %s for %s: %s",
                        attempt + 1,
                        tx_hash,
                        exc,
                    )

        if receipt_error is not None:
            # If we already know it's a mint selector / SeaDrop, still copy it.
            if looks_like_mint:
                log.warning(
                    "Receipt unavailable for mint-like tx %s (%s); copying from calldata",
                    tx_hash,
                    receipt_error,
                )
            else:
                log.info(
                    "Target tx %s receipt check failed and selector unknown: %s",
                    tx_hash,
                    receipt_error,
                )
                return None

        if not looks_like_mint:
            log.info(
                "Target tx %s to %s ignored (not a recognized mint).",
                tx_hash,
                to_addr,
            )
            return None

        return MintCandidate(
            source_tx_hash=tx_hash,
            contract_address=Web3.to_checksum_address(to_addr),
            input_data=input_data if input_data.startswith("0x") else "0x" + input_data,
            value_wei=value_wei,
            value_eth=value_eth,
            block_number=block_number,
            method_hint=method_hint or f"call {selector}",
            target_wallet=Web3.to_checksum_address(tx_from),
        )

    def _receipt_shows_mint(self, receipt: Any) -> bool:
        logs = receipt.get("logs") if isinstance(receipt, dict) else getattr(receipt, "logs", None)
        logs = logs or []
        for entry in logs:
            raw_topics = (
                entry.get("topics")
                if isinstance(entry, dict)
                else getattr(entry, "topics", None)
            )
            topics = [_normalize_hex(t) for t in (raw_topics or [])]
            if not topics:
                continue

            topic0 = topics[0]
            # ERC-721 Transfer: topics[1]=from, topics[2]=to
            if topic0 == _normalize_hex(TRANSFER_TOPIC) and len(topics) >= 3:
                if _topic_address(topics[1]) == ZERO_ADDR:
                    return True
            # ERC-1155 TransferSingle: topics[2]=from, topics[3]=to
            if topic0 == _normalize_hex(TRANSFER_SINGLE_TOPIC) and len(topics) >= 3:
                if len(topics) >= 3 and _topic_address(topics[2]) == ZERO_ADDR:
                    return True
        return False
