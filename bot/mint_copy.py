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
    "0x198441cb": "MintQuantityCannotBeZero",
    "0x0d35e921": "IncorrectPayment (this SeaDrop mint requires ETH — not a free mint)",
    "0xf477d26f": "FeeRecipientNotAllowed",
    "0x13da22f2": "NotActive (public drop window closed or not started)",
}


def _addr_word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _ensure_hex(data: str) -> str:
    text = data.lower()
    if not text.startswith("0x"):
        text = "0x" + text
    return text


def normalize_seadrop_mint_public(data: str) -> str:
    """
    Keep only selector + 4 ABI words.

    Some wallets append trailing junk after mintPublic; that breaks naive
    quantity retries (data[:-64]) and can report MintQuantityCannotBeZero.
    """
    data = _ensure_hex(data)
    need = 10 + 64 * 4
    if len(data) < need:
        return data
    return data[:need]


def seadrop_mint_public_quantity(data: str) -> int:
    data = normalize_seadrop_mint_public(data)
    if len(data) < 10 + 64 * 4:
        return 0
    return int(data[10 + 64 * 3 : 10 + 64 * 4], 16)


def seadrop_mint_public_nft(data: str) -> str:
    data = normalize_seadrop_mint_public(data)
    if len(data) < 10 + 64:
        return ""
    return Web3.to_checksum_address("0x" + data[10 + 24 : 10 + 64])


def with_seadrop_mint_public_quantity(data: str, quantity: int) -> str:
    data = normalize_seadrop_mint_public(data)
    qty_word = format(int(quantity), "x").rjust(64, "0")
    return data[: 10 + 64 * 3] + qty_word


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
    data = _ensure_hex(input_data)

    note = "raw replay"
    selector = data[:10]
    contract = contract_address.lower()

    # SeaDrop public mint: third address arg is minterIfNotPayer.
    if contract == SEADROP and selector == SEADROP_MINT_PUBLIC and len(data) >= 10 + 64 * 4:
        body = normalize_seadrop_mint_public(data)[10:]
        # args: nftContract, feeRecipient, minterIfNotPayer, quantity
        zero_word = "0" * 64
        new_body = body[: 64 * 2] + zero_word + body[64 * 3 : 64 * 4]
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
    lower = text.lower()
    for selector, meaning in KNOWN_ERRORS.items():
        if selector in lower:
            # IncorrectPayment(got, want) — decode want when present.
            if selector == "0x0d35e921" and selector in lower:
                try:
                    hexdata = lower[lower.index(selector) :]
                    hexdata = "".join(c for c in hexdata if c in "0123456789abcdef")
                    if len(hexdata) >= 8 + 128:
                        want = int(hexdata[8 + 64 : 8 + 128], 16)
                        want_eth = Web3.from_wei(want, "ether")
                        return (
                            f"IncorrectPayment (needs {want_eth} ETH for this mint) "
                            f"[{selector}]"
                        )
                except Exception:
                    pass
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
        """ Backward-compatible single-wallet copy (first minting wallet). """
        results = self.try_copy_all(candidate)
        _, ok, message, tx_hash = results[0]
        return ok, message, tx_hash

    def seadrop_unit_price(self, nft_contract: str) -> int:
        """Read SeaDrop getPublicDrop(nft).mintPrice (wei)."""
        selector = Web3.keccak(text="getPublicDrop(address)")[:4].hex()
        data = "0x" + selector + _addr_word(nft_contract)
        out = self.w3.eth.call(
            {"to": Web3.to_checksum_address(SEADROP), "data": data}
        )
        raw = out.hex() if hasattr(out, "hex") else bytes(out).hex()
        raw = raw[2:] if raw.startswith("0x") else raw
        if len(raw) < 64:
            return 0
        return int(raw[:64], 16)

    def _resolve_mint_value(
        self, candidate: MintCandidate, data: str
    ) -> tuple[int, str | None]:
        """
        Return (value_wei, skip_reason).

        SeaDrop public drops can require mintPrice even when the watched
        wallet's tx shows value=0 (price changed, or free window ended).
        """
        value = int(candidate.value_wei)
        if (
            candidate.contract_address.lower() != SEADROP
            or not data.startswith(SEADROP_MINT_PUBLIC)
        ):
            return value, None

        try:
            nft = seadrop_mint_public_nft(data)
            unit = int(self.seadrop_unit_price(nft))
        except Exception as exc:
            log.warning("getPublicDrop failed: %s", exc)
            return value, None

        qty = max(seadrop_mint_public_quantity(data), 1)
        required = unit * qty
        if required <= 0:
            return value, None

        required_eth = Web3.from_wei(required, "ether")
        unit_eth = Web3.from_wei(unit, "ether")
        if self.settings.free_mints_only and value < required:
            return (
                value,
                (
                    f"Skipped: paid SeaDrop mint ({unit_eth} ETH × {qty} = "
                    f"{required_eth} ETH). FREE_MINTS_ONLY=true"
                ),
            )
        return max(value, required), None

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

            value_wei, skip_reason = self._resolve_mint_value(candidate, data)
            if skip_reason:
                return False, skip_reason, None

            attempts = [data]
            if (
                candidate.contract_address.lower() == SEADROP
                and data.startswith(SEADROP_MINT_PUBLIC)
                and seadrop_mint_public_quantity(data) > 1
            ):
                attempts.append(with_seadrop_mint_public_quantity(data, 1))

            last_err = ""
            for attempt_i, attempt_data in enumerate(attempts):
                attempt_value = value_wei
                if (
                    attempt_i > 0
                    and candidate.contract_address.lower() == SEADROP
                    and attempt_data.startswith(SEADROP_MINT_PUBLIC)
                    and value_wei > 0
                ):
                    # Scale payment down when retrying quantity=1.
                    full_qty = seadrop_mint_public_quantity(data)
                    if full_qty > 1:
                        attempt_value = value_wei // full_qty

                tx: dict = {
                    "from": my_wallet,
                    "to": Web3.to_checksum_address(candidate.contract_address),
                    "data": attempt_data,
                    "value": attempt_value,
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
                            f"DRY_RUN OK — would mint {Web3.from_wei(attempt_value, 'ether')} ETH to "
                            f"{candidate.contract_address} ({qty_note})"
                        ),
                        None,
                    )

                fee_cap = int(
                    tx.get("maxFeePerGas") or tx.get("gasPrice") or 0
                )
                need = self.settings.gas_limit * fee_cap + int(attempt_value)
                try:
                    bal = int(self.w3.eth.get_balance(my_wallet))
                except Exception as exc:
                    return False, f"Balance check failed: {exc}", None
                if bal < need:
                    have_eth = Web3.from_wei(bal, "ether")
                    need_eth = Web3.from_wei(need, "ether")
                    return (
                        False,
                        f"Insufficient ETH for gas+mint: have {have_eth}, need ~{need_eth}",
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
