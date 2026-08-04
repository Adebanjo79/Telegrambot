from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from web3 import Web3

from bot.config import Settings
from bot.models import MintCandidate
from bot.rpc import rpc_call

log = logging.getLogger(__name__)

# Common public mint / claim selectors (first 4 bytes of keccak of the signature).
# Intentionally excludes approvals / transfers / marketplace selectors.
MINT_SELECTORS: dict[str, str] = {
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

# ERC-721 Transfer(address,address,uint256) and ERC-1155 TransferSingle
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


class WalletWatcher:
    """Poll Robinhood Chain for mint-like txs from watched wallets."""

    def __init__(self, settings: Settings, w3: Web3) -> None:
        self.settings = settings
        self.w3 = w3
        self.enabled = True
        self.last_block = 0
        self.target_set = {addr.lower() for addr in settings.target_wallets}

    def bootstrap(self) -> int:
        head = rpc_call(lambda: self.w3.eth.block_number)
        self.last_block = head
        return head

    def poll(self) -> list[MintCandidate]:
        if not self.enabled:
            return []

        head = rpc_call(lambda: self.w3.eth.block_number)
        if head <= self.last_block:
            return []

        start = max(self.last_block + 1, head - self.settings.max_catchup_blocks + 1)
        found: list[MintCandidate] = []
        last_ok = self.last_block

        for block_number in range(start, head + 1):
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

        self.last_block = head
        return found

    def _inspect_tx(self, tx: Any, block_number: int) -> MintCandidate | None:
        tx_from = _normalize_hex(tx.get("from"))
        if tx_from not in self.target_set:
            return None

        to_addr = tx.get("to")
        if not to_addr:
            return None  # contract creation — skip

        input_data = _normalize_hex(tx.get("input") or tx.get("data") or "0x")
        if input_data in {"0x", "0x0", ""}:
            return None

        value_wei = int(tx.get("value") or 0)
        value_eth = Decimal(value_wei) / Decimal(10**18)
        if self.settings.free_mints_only and value_wei > 0:
            log.info("Skip paid tx %s value=%s ETH", _normalize_hex(tx.get("hash")), value_eth)
            return None

        selector = input_data[:10] if len(input_data) >= 10 else input_data
        method_hint = MINT_SELECTORS.get(selector)
        looks_like_mint = method_hint is not None

        receipt_mint = False
        tx_hash = _normalize_hex(tx.get("hash"))
        try:
            receipt = rpc_call(lambda: self.w3.eth.get_transaction_receipt(tx_hash))
            receipt_mint = self._receipt_shows_mint(receipt)
            if receipt_mint:
                looks_like_mint = True
                method_hint = method_hint or "NFT mint (Transfer from 0x0)"
        except Exception as exc:
            log.debug("Receipt check failed for %s: %s", tx_hash, exc)

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
        logs = receipt.get("logs") or []
        for entry in logs:
            topics = [_normalize_hex(t) for t in (entry.get("topics") or [])]
            if not topics:
                continue

            topic0 = topics[0]
            # ERC-721 Transfer: topics[1]=from, topics[2]=to
            if topic0 == _normalize_hex(TRANSFER_TOPIC) and len(topics) >= 3:
                if _topic_address(topics[1]) == ZERO_ADDR:
                    return True
            # ERC-1155 TransferSingle: topics[2]=from, topics[3]=to
            if topic0 == _normalize_hex(TRANSFER_SINGLE_TOPIC) and len(topics) >= 3:
                # Operator is topics[1]; from is topics[2] when indexed
                if len(topics) >= 3 and _topic_address(topics[2]) == ZERO_ADDR:
                    return True
        return False
