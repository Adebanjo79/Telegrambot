from __future__ import annotations

import logging
from decimal import Decimal

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import ContractLogicError

from bot.config import Settings
from bot.models import MintCandidate

log = logging.getLogger(__name__)


class MintCopyService:
    """Simulate then (optionally) broadcast a copy of a mint-style call."""

    def __init__(self, settings: Settings, w3: Web3) -> None:
        self.settings = settings
        self.w3 = w3
        self.account: LocalAccount = Account.from_key(settings.private_key)
        self._copied: set[str] = set()

    @property
    def my_wallet(self) -> str:
        return self.account.address

    def already_copied(self, source_tx_hash: str) -> bool:
        return source_tx_hash.lower() in self._copied

    def mark_copied(self, source_tx_hash: str) -> None:
        self._copied.add(source_tx_hash.lower())

    def try_copy(self, candidate: MintCandidate) -> tuple[bool, str, str | None]:
        if self.already_copied(candidate.source_tx_hash):
            return False, "Already processed this source tx.", None

        self.mark_copied(candidate.source_tx_hash)

        try:
            tx: dict = {
                "from": self.my_wallet,
                "to": Web3.to_checksum_address(candidate.contract_address),
                "data": candidate.input_data,
                "value": candidate.value_wei,
                "gas": self.settings.gas_limit,
                "chainId": self.settings.chain_id,
            }

            # Prefer EIP-1559 fees when the node supports them; else legacy gasPrice.
            try:
                latest = self.w3.eth.get_block("latest")
                base_fee = latest.get("baseFeePerGas")
                if base_fee is not None:
                    tip = self.w3.to_wei(0.05, "gwei")
                    tx["maxPriorityFeePerGas"] = tip
                    tx["maxFeePerGas"] = int(base_fee) * 2 + tip
                else:
                    tx["gasPrice"] = self.w3.eth.gas_price
            except Exception:
                tx["gasPrice"] = self.w3.eth.gas_price

            # Dry-run first so we do not burn gas on whitelist/sold-out reverts.
            try:
                self.w3.eth.call(tx)
            except ContractLogicError as exc:
                return (
                    False,
                    f"Simulation reverted (whitelist/signature/sold out?): {exc}",
                    None,
                )
            except Exception as exc:
                return False, f"Simulation failed: {exc}", None

            if self.settings.dry_run:
                return (
                    True,
                    (
                        f"DRY_RUN OK — would mint {candidate.value_eth} ETH to "
                        f"{candidate.contract_address}"
                    ),
                    None,
                )

            tx["nonce"] = self.w3.eth.get_transaction_count(self.my_wallet, "pending")
            signed = self.account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = self.w3.eth.send_raw_transaction(raw)
            hex_hash = tx_hash.hex()
            if not hex_hash.startswith("0x"):
                hex_hash = "0x" + hex_hash
            return True, "Copy mint submitted.", hex_hash
        except Exception as exc:
            log.exception("Copy mint failed")
            return False, f"Copy mint failed: {exc}", None

    def eth_balance(self) -> Decimal:
        wei = self.w3.eth.get_balance(self.my_wallet)
        return Decimal(wei) / Decimal(10**18)
