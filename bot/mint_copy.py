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

# OpenSea SeaDrop 1.0 (same address on many chains, including Robinhood Chain)
SEADROP = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
# mintPublic(address,address,address,uint256)
SEADROP_MINT_PUBLIC = "0x161ac21f"
# mintAllowList(...) — still needs the target's merkle proof; rewriting alone won't help
SEADROP_MINT_ALLOWLIST = "0x8d7f0ad4"

KNOWN_ERRORS = {
    "0x1fe7da08": "PayerNotAllowed (SeaDrop: calldata still pointed at another wallet)",
    "0x815e1d64": "OnlyAllowedSeaDrop",
    "0xd05cb32e": "MintQuantityExceedsMaxSupply",
}


def _addr_word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def rewrite_calldata_for_my_wallet(
    input_data: str,
    target_wallet: str,
    my_wallet: str,
    contract_address: str,
) -> tuple[str, str]:
    """
    Adapt copied mint calldata so it mints to *our* wallet.

    - SeaDrop mintPublic: set minterIfNotPayer to address(0) (payer = minter)
    - Otherwise: replace padded target address words with our address
    """
    data = input_data.lower()
    if not data.startswith("0x"):
        data = "0x" + data

    note = "raw replay"
    selector = data[:10]
    contract = contract_address.lower()

    # SeaDrop public mint: third address arg is minterIfNotPayer.
    if contract == SEADROP and selector == SEADROP_MINT_PUBLIC and len(data) >= 10 + 64 * 4:
        body = data[10:]
        # args: nftContract, feeRecipient, minterIfNotPayer, quantity
        zero_word = "0" * 64
        new_body = body[: 64 * 2] + zero_word + body[64 * 3 :]
        note = "SeaDrop mintPublic → minterIfNotPayer=0x0 (you receive the NFT)"
        return "0x" + selector[2:] + new_body, note

    if contract == SEADROP and selector == SEADROP_MINT_ALLOWLIST:
        return data, "SeaDrop allowlist mint (needs their Merkle proof — usually not copyable)"

    target_word = _addr_word(target_wallet)
    my_word = _addr_word(my_wallet)
    if target_word in data:
        rewritten = data.replace(target_word, my_word)
        count = data.count(target_word)
        note = f"replaced target address in calldata x{count}"
        return rewritten, note

    return data, note


def friendly_revert(exc: BaseException) -> str:
    text = str(exc)
    for selector, meaning in KNOWN_ERRORS.items():
        if selector in text.lower():
            return f"{meaning} [{selector}]"
    return text


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
            data, rewrite_note = rewrite_calldata_for_my_wallet(
                candidate.input_data,
                candidate.target_wallet,
                self.my_wallet,
                candidate.contract_address,
            )
            log.info("Calldata adapt: %s", rewrite_note)

            # Try original quantity first; if SeaDrop public mint fails on amount, retry qty=1.
            attempts = [data]
            if (
                candidate.contract_address.lower() == SEADROP
                and data.startswith(SEADROP_MINT_PUBLIC)
                and len(data) >= 10 + 64 * 4
            ):
                qty_word = data[-64:]
                if int(qty_word, 16) > 1:
                    attempts.append(data[:-64] + "0" * 63 + "1")

            last_err = ""
            for attempt_i, attempt_data in enumerate(attempts):
                tx: dict = {
                    "from": self.my_wallet,
                    "to": Web3.to_checksum_address(candidate.contract_address),
                    "data": attempt_data,
                    "value": candidate.value_wei,
                    "gas": self.settings.gas_limit,
                    "chainId": self.settings.chain_id,
                }

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

                try:
                    self.w3.eth.call(tx)
                except ContractLogicError as exc:
                    last_err = friendly_revert(exc)
                    if attempt_i < len(attempts) - 1:
                        log.info("Sim failed (%s); retrying with quantity=1", last_err)
                        continue
                    return (
                        False,
                        f"Simulation reverted: {last_err} | adapt={rewrite_note}",
                        None,
                    )
                except Exception as exc:
                    return False, f"Simulation failed: {exc} | adapt={rewrite_note}", None

                qty_note = "qty=1 retry" if attempt_i > 0 else rewrite_note
                if self.settings.dry_run:
                    return (
                        True,
                        (
                            f"DRY_RUN OK — would mint {candidate.value_eth} ETH to "
                            f"{candidate.contract_address} ({qty_note})"
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
                return True, f"Copy mint submitted ({qty_note}).", hex_hash

            return False, f"Simulation reverted: {last_err}", None
        except Exception as exc:
            log.exception("Copy mint failed")
            return False, f"Copy mint failed: {exc}", None

    def eth_balance(self) -> Decimal:
        wei = self.w3.eth.get_balance(self.my_wallet)
        return Decimal(wei) / Decimal(10**18)
