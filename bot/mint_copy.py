from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import ContractLogicError

from bot.config import Settings, _normalize_private_key
from bot.models import MintCandidate
from bot.wallet_store import DEFAULT_STORE, load_extra_keys, merge_keys, save_keys

log = logging.getLogger(__name__)

# OpenSea SeaDrop 1.0 (same address on many chains, including Robinhood Chain)
SEADROP = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
# mintPublic(address,address,address,uint256)
SEADROP_MINT_PUBLIC = "0x161ac21f"
# mintAllowList(...) — still needs the target's merkle proof; rewriting alone won't help
SEADROP_MINT_ALLOWLIST = "0x8d7f0ad4"
# mintSigned(...) — signature is bound to the original minter; not copyable
SEADROP_MINT_SIGNED = "0x4b61cd6f"

KNOWN_ERRORS = {
    "0x1fe7da08": "PayerNotAllowed (SeaDrop: calldata still pointed at another wallet)",
    "0x815e1d64": "OnlyAllowedSeaDrop",
    "0xd05cb32e": "MintQuantityExceedsMaxSupply",
    "0xd855c4f4": "InvalidSignature (signed/allowlist mint for another wallet)",
    "0x7f023c72": "InvalidAuthSignature (auth-signed mint; not copyable)",
    "0xedc01273": "MintQuantityExceedsMaxMintedPerWallet (you already hit this drop's wallet limit)",
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

    if contract == SEADROP and selector == SEADROP_MINT_SIGNED:
        return data, "SeaDrop mintSigned (signature bound to their wallet — not copyable)"

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
    """Simulate then (optionally) broadcast copy mints from one or more wallets."""

    def __init__(self, settings: Settings, w3: Web3) -> None:
        self.settings = settings
        self.w3 = w3
        self.store_path = DEFAULT_STORE
        self._copied: set[str] = set()
        self.reload_accounts()

    def reload_accounts(self) -> None:
        keys = merge_keys(self.settings.private_keys, self.store_path)
        if not keys:
            raise ValueError("No minting private keys configured")
        self.accounts: list[LocalAccount] = [Account.from_key(k) for k in keys]
        self._key_by_addr = {
            Account.from_key(k).address.lower(): k for k in keys
        }
        self.account: LocalAccount = self.accounts[0]
        env_n = len(self.settings.private_keys)
        file_n = max(0, len(self.accounts) - env_n)
        log.info(
            "Loaded %s minting wallet(s): %s from .env, %s from %s",
            len(self.accounts),
            env_n,
            file_n,
            self.store_path,
        )

    def add_private_key(self, raw_key: str) -> str:
        key = _normalize_private_key(raw_key)
        addr = Account.from_key(key).address
        env_addrs = {
            Account.from_key(k).address.lower() for k in self.settings.private_keys
        }
        if addr.lower() in env_addrs:
            self.reload_accounts()
            return addr

        existing = load_extra_keys(self.store_path)
        extra_map = {Account.from_key(k).address.lower(): k for k in existing}
        extra_map[addr.lower()] = key
        save_keys(list(extra_map.values()), self.store_path)
        self.reload_accounts()
        return addr

    def remove_wallet(self, address: str) -> bool:
        addr = Web3.to_checksum_address(address).lower()
        env_addrs = {
            Account.from_key(k).address.lower() for k in self.settings.private_keys
        }
        if addr in env_addrs:
            raise ValueError(
                "That wallet comes from .env PRIVATE_KEY(S). Remove it from .env instead."
            )
        existing = load_extra_keys(self.store_path)
        remaining = [
            k for k in existing if Account.from_key(k).address.lower() != addr
        ]
        if len(remaining) == len(existing):
            return False
        save_keys(remaining, self.store_path)
        self.reload_accounts()
        return True

    @property
    def my_wallet(self) -> str:
        return self.account.address

    @property
    def my_wallets(self) -> list[str]:
        return [a.address for a in self.accounts]

    def already_copied(self, source_tx_hash: str) -> bool:
        return source_tx_hash.lower() in self._copied

    def mark_copied(self, source_tx_hash: str) -> None:
        self._copied.add(source_tx_hash.lower())

    def try_copy_all(
        self, candidate: MintCandidate
    ) -> list[tuple[str, bool, str, str | None]]:
        """
        Copy the mint with every configured minting wallet in parallel.
        Returns list of (wallet, ok, message, tx_hash) in wallet order.
        """
        if self.already_copied(candidate.source_tx_hash):
            return [
                (self.my_wallet, False, "Already processed this source tx.", None)
            ]

        self.mark_copied(candidate.source_tx_hash)
        # Snapshot so /addwallet during a run can't shrink/skip the list mid-loop.
        accounts = list(self.accounts)
        log.info(
            "Copying mint %s with %s wallet(s) in parallel",
            candidate.source_tx_hash,
            len(accounts),
        )
        if len(accounts) == 1:
            ok, message, tx_hash = self._try_copy_with(accounts[0], candidate)
            return [(accounts[0].address, ok, message, tx_hash)]

        results: list[tuple[str, bool, str, str | None] | None] = [None] * len(
            accounts
        )
        workers = min(len(accounts), 8)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._try_copy_with, account, candidate): i
                for i, account in enumerate(accounts)
            }
            for fut in as_completed(futures):
                i = futures[fut]
                account = accounts[i]
                try:
                    ok, message, tx_hash = fut.result()
                except Exception as exc:
                    log.exception("Wallet copy crashed for %s", account.address)
                    ok, message, tx_hash = False, f"Copy crashed: {exc}", None
                results[i] = (account.address, ok, message, tx_hash)
                log.info(
                    "Wallet %s/%s %s -> ok=%s",
                    i + 1,
                    len(accounts),
                    account.address,
                    ok,
                )
        return [r for r in results if r is not None]

    def try_copy(self, candidate: MintCandidate) -> tuple[bool, str, str | None]:
        """Backward-compatible single-wallet copy (first minting wallet). """
        results = self.try_copy_all(candidate)
        _, ok, message, tx_hash = results[0]
        return ok, message, tx_hash

    def _try_copy_with(
        self, account: LocalAccount, candidate: MintCandidate
    ) -> tuple[bool, str, str | None]:
        my_wallet = account.address
        try:
            data, rewrite_note = rewrite_calldata_for_my_wallet(
                candidate.input_data,
                candidate.target_wallet,
                my_wallet,
                candidate.contract_address,
            )
            log.info("Calldata adapt [%s]: %s", my_wallet, rewrite_note)

            if "not copyable" in rewrite_note.lower():
                return False, f"Skipped: {rewrite_note}", None

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
                    "from": my_wallet,
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
                        log.info(
                            "Sim failed for %s (%s); retrying with quantity=1",
                            my_wallet,
                            last_err,
                        )
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

                fee_cap = int(
                    tx.get("maxFeePerGas") or tx.get("gasPrice") or 0
                )
                need = self.settings.gas_limit * fee_cap + int(candidate.value_wei)
                try:
                    bal = int(self.w3.eth.get_balance(my_wallet))
                except Exception as exc:
                    return False, f"Balance check failed: {exc}", None
                if bal < need:
                    have_eth = Web3.from_wei(bal, "ether")
                    need_eth = Web3.from_wei(need, "ether")
                    return (
                        False,
                        f"Insufficient ETH for gas: have {have_eth}, need ~{need_eth}",
                        None,
                    )

                tx["nonce"] = self.w3.eth.get_transaction_count(my_wallet, "pending")
                signed = account.sign_transaction(tx)
                raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
                tx_hash = self.w3.eth.send_raw_transaction(raw)
                hex_hash = tx_hash.hex()
                if not hex_hash.startswith("0x"):
                    hex_hash = "0x" + hex_hash
                return True, f"Copy mint submitted ({qty_note}).", hex_hash

            return False, f"Simulation reverted: {last_err}", None
        except Exception as exc:
            log.exception("Copy mint failed for %s", my_wallet)
            return False, f"Copy mint failed: {exc}", None

    def eth_balance(self, wallet: str | None = None) -> Decimal:
        address = wallet or self.my_wallet
        wei = self.w3.eth.get_balance(address)
        return Decimal(wei) / Decimal(10**18)

    def all_balances(self) -> list[tuple[str, Decimal]]:
        return [(w, self.eth_balance(w)) for w in self.my_wallets]
