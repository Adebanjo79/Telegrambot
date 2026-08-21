from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    "0xcd1c8867": "InsufficientPayment (this mint requires ETH — not a free mint)",
    "0xfb8f41b2": "ERC20InsufficientAllowance (this mint needs an ERC20 token payment/approval — not a free mint)",
    "0xe450d38c": "ERC20InsufficientBalance (this mint needs an ERC20 token — not a free mint)",
    "0xf477d26f": "FeeRecipientNotAllowed",
    "0x13da22f2": "NotActive (public drop window closed or not started)",
    "0x201c04ab": "IncorrectETHAmount (this mint requires a specific ETH payment — not a free mint)",
}

PAID_MINT_SELECTORS = (
    "0x0d35e921",
    "0xcd1c8867",
    "0xfb8f41b2",
    "0xe450d38c",
    "0x201c04ab",
)

_NONCE_STATE_PATTERNS = (
    re.compile(r"\bstate:\s*(\d+)", re.IGNORECASE),
    re.compile(r"\baccount has nonce of:\s*(\d+)", re.IGNORECASE),
)


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
    return seadrop_nft_contract(data)


def seadrop_nft_contract(data: str) -> str:
    """Return SeaDrop's first calldata argument (the NFT contract)."""
    data = _ensure_hex(data)
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


def nonce_required_by_error(exc: BaseException) -> int:
    """Extract the chain's next nonce from common `nonce too low` errors."""
    text = str(exc)
    for pattern in _NONCE_STATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return 0


class MintCopyService:
    """Simulate then (optionally) broadcast copy mints from one or more wallets."""

    def __init__(self, settings: Settings, w3: Web3) -> None:
        self.settings = settings
        self.w3 = w3
        self.store_path = DEFAULT_STORE
        self._copied: set[str] = set()
        self._collection_names: dict[str, str] = {}
        # address -> (balance wei, next pending nonce, refreshed monotonic time)
        self._wallet_state: dict[str, tuple[int, int, float]] = {}
        # Accepted transactions may not immediately appear on every
        # load-balanced RPC. Keep a temporary local nonce floor.
        self._nonce_floor: dict[str, tuple[int, float]] = {}
        self._wallet_state_lock = threading.Lock()
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

    def refresh_wallet_state(self, wallet: str | None = None) -> None:
        """
        Refresh balance + pending nonce outside the critical mint path.

        When wallet is omitted this warms every configured wallet at startup.
        The background loop refreshes one wallet at a time so it doesn't
        reserve a large burst of rate-limiter slots ahead of a mint.
        """
        wallets = [wallet] if wallet else self.my_wallets
        for address in wallets:
            checksum = Web3.to_checksum_address(address)
            balance = int(self.w3.eth.get_balance(checksum))
            nonce = int(self.w3.eth.get_transaction_count(checksum, "pending"))
            now = time.monotonic()
            with self._wallet_state_lock:
                floor = self._nonce_floor.get(checksum.lower())
                if floor and now < floor[1]:
                    nonce = max(nonce, floor[0])
                elif floor:
                    self._nonce_floor.pop(checksum.lower(), None)
                self._wallet_state[checksum.lower()] = (
                    balance,
                    nonce,
                    now,
                )

    def wallet_state(self, wallet: str) -> tuple[int, int]:
        """Use fresh cached state, falling back to an immediate RPC refresh."""
        key = wallet.lower()
        with self._wallet_state_lock:
            cached = self._wallet_state.get(key)
        if cached and time.monotonic() - cached[2] <= self.settings.wallet_state_ttl_sec:
            return cached[0], cached[1]

        self.refresh_wallet_state(wallet)
        with self._wallet_state_lock:
            balance, nonce, _ = self._wallet_state[key]
        return balance, nonce

    def force_wallet_state(
        self, wallet: str, minimum_nonce: int = 0
    ) -> tuple[int, int]:
        """Discard stale cache and read state again after a nonce rejection."""
        key = wallet.lower()
        with self._wallet_state_lock:
            self._wallet_state.pop(key, None)
            self._nonce_floor.pop(key, None)
        self.refresh_wallet_state(wallet)
        with self._wallet_state_lock:
            balance, nonce, refreshed = self._wallet_state[key]
            nonce = max(nonce, int(minimum_nonce))
            self._wallet_state[key] = (balance, nonce, refreshed)
        return balance, nonce

    def consume_wallet_state(self, wallet: str, estimated_cost: int) -> None:
        """Advance cached nonce after a transaction is accepted by the RPC."""
        key = wallet.lower()
        with self._wallet_state_lock:
            cached = self._wallet_state.get(key)
            if not cached:
                return
            balance, nonce, _ = cached
            next_nonce = nonce + 1
            self._wallet_state[key] = (
                max(0, balance - int(estimated_cost)),
                next_nonce,
                time.monotonic(),
            )
            self._nonce_floor[key] = (next_nonce, time.monotonic() + 120.0)

    def collection_info(self, candidate: MintCandidate) -> tuple[str, str]:
        """
        Return (collection name, NFT contract) for Telegram.

        SeaDrop is only the mint router, so its first calldata argument is the
        actual NFT collection. Direct mints use the transaction destination.
        """
        collection = candidate.contract_address
        if candidate.contract_address.lower() == SEADROP:
            try:
                decoded = seadrop_nft_contract(candidate.input_data)
                if decoded:
                    collection = decoded
            except (ValueError, TypeError):
                pass

        collection = Web3.to_checksum_address(collection)
        cached = self._collection_names.get(collection.lower())
        if cached:
            return cached, collection

        try:
            out = self.w3.eth.call({"to": collection, "data": "0x06fdde03"})
            raw = bytes(out)
            name = ""

            # Standard ABI string: offset, then length + UTF-8 bytes.
            if len(raw) >= 64:
                offset = int.from_bytes(raw[:32], "big")
                if 0 < offset and offset + 32 <= len(raw):
                    length = int.from_bytes(raw[offset : offset + 32], "big")
                    end = offset + 32 + length
                    if length <= 512 and end <= len(raw):
                        name = raw[offset + 32 : end].decode(
                            "utf-8", errors="replace"
                        )

            # Some older NFT contracts return bytes32 from name().
            if not name and len(raw) >= 32:
                name = raw[:32].rstrip(b"\x00").decode("utf-8", errors="replace")

            name = "".join(c for c in name.strip() if c.isprintable())[:120]
            if name:
                self._collection_names[collection.lower()] = name
                return name, collection
        except Exception as exc:
            log.info("Collection name lookup failed for %s: %s", collection, exc)

        return "Unknown collection", collection

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

        # SeaDrop signed/allowlist mints are never copyable — skip all wallets
        # up front so Telegram is not flooded with 15 identical lines.
        data = candidate.input_data.lower()
        if not data.startswith("0x"):
            data = "0x" + data
        selector = data[:10]
        if candidate.contract_address.lower() == SEADROP:
            if selector == SEADROP_MINT_SIGNED:
                skip = (
                    "Skipped: SeaDrop mintSigned "
                    "(signature bound to their wallet — not copyable)"
                )
                return [(a.address, False, skip, None) for a in accounts]
            if selector == SEADROP_MINT_ALLOWLIST:
                skip = (
                    "Skipped: SeaDrop allowlist mint "
                    "(needs their Merkle proof — usually not copyable)"
                )
                return [(a.address, False, skip, None) for a in accounts]
            if selector == SEADROP_MINT_PUBLIC and int(candidate.value_wei) == 0:
                # Free public SeaDrop: simulate once, then blast every wallet
                # so short drop windows don't close mid-queue.
                return self._try_copy_seadrop_public_fast(accounts, candidate)

        if len(accounts) == 1:
            ok, message, tx_hash = self._try_copy_with(accounts[0], candidate)
            return [(accounts[0].address, ok, message, tx_hash)]

        # With free-mints-only, probe once first. Paid drops revert the same for
        # every wallet — no point burning RPC on all 15.
        if self.settings.free_mints_only:
            ok, message, tx_hash = self._try_copy_with(accounts[0], candidate)
            lower = message.lower()
            if (not ok) and any(sel in lower for sel in PAID_MINT_SELECTORS):
                skip = (
                    "Skipped: paid mint (needs ETH). FREE_MINTS_ONLY=true — "
                    f"{message}"
                )
                log.info("Paid mint detected; skipping remaining wallets")
                return [(a.address, False, skip, None) for a in accounts]
            if (not ok) and "0x13da22f2" in lower:
                skip = f"Skipped: drop already NotActive — {message}"
                return [(a.address, False, skip, None) for a in accounts]
            # First wallet finished; run the rest (skip index 0).
            results: list[tuple[str, bool, str, str | None] | None] = [None] * len(
                accounts
            )
            results[0] = (accounts[0].address, ok, message, tx_hash)
            rest = list(enumerate(accounts))[1:]
        else:
            results = [None] * len(accounts)
            rest = list(enumerate(accounts))

        # Keep concurrency modest; the provider also paces requests. Too many
        # parallel wallets on a 25 req/s plan causes 429s mid-mint.
        workers = min(len(rest) or 1, 3)
        stop_reason: list[str] = []

        def _run(account, i: int) -> None:
            if stop_reason:
                results[i] = (
                    account.address,
                    False,
                    f"Skipped: {stop_reason[0]}",
                    None,
                )
                return
            try:
                ok, message, tx_hash = self._try_copy_with(account, candidate)
            except Exception as exc:
                log.exception("Wallet copy crashed for %s", account.address)
                ok, message, tx_hash = False, f"Copy crashed: {exc}", None
            results[i] = (account.address, ok, message, tx_hash)
            if (not ok) and "0x13da22f2" in message.lower() and not stop_reason:
                stop_reason.append("drop became NotActive while copying")
            log.info(
                "Wallet %s/%s %s -> ok=%s",
                i + 1,
                len(accounts),
                account.address,
                ok,
            )

        if rest:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(
                    pool.map(
                        lambda item: _run(item[1], item[0]),
                        rest,
                    )
                )

        # Second pass for wallets that only failed because the RPC was full.
        retry_idxs = [
            i
            for i, r in enumerate(results)
            if r is not None
            and not r[1]
            and "429" in r[2].lower()
            and "notactive" not in r[2].lower()
        ]
        if retry_idxs and not stop_reason:
            log.warning(
                "Retrying %s wallet(s) after RPC 429 on mint %s",
                len(retry_idxs),
                candidate.source_tx_hash,
            )
            time.sleep(0.8)
            with ThreadPoolExecutor(max_workers=min(2, len(retry_idxs))) as pool:
                list(
                    pool.map(
                        lambda i: _run(accounts[i], i),
                        retry_idxs,
                    )
                )

        return [r for r in results if r is not None]

    def _fee_fields(self) -> dict:
        tx_fees: dict = {}
        try:
            latest = self.w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas")
            if base_fee is not None:
                tip = self.w3.to_wei(0.05, "gwei")
                tx_fees["maxPriorityFeePerGas"] = tip
                tx_fees["maxFeePerGas"] = int(base_fee) * 2 + tip
            else:
                tx_fees["gasPrice"] = self.w3.eth.gas_price
        except Exception:
            tx_fees["gasPrice"] = self.w3.eth.gas_price
        return tx_fees

    def _try_copy_seadrop_public_fast(
        self, accounts: list[LocalAccount], candidate: MintCandidate
    ) -> list[tuple[str, bool, str, str | None]]:
        """
        Free SeaDrop mintPublic fast path.

        Simulate once, prefetch nonce+balance for every wallet, sign locally,
        then blast sendRawTransaction. Avoids per-wallet eth_call so short
        public windows don't close before later wallets fire.
        """
        primary = accounts[0]
        data, rewrite_note = rewrite_calldata_for_my_wallet(
            candidate.input_data,
            candidate.target_wallet,
            primary.address,
            candidate.contract_address,
        )
        value_wei, skip_reason = self._resolve_mint_value(candidate, data)
        if skip_reason:
            return [(a.address, False, skip_reason, None) for a in accounts]

        to_addr = Web3.to_checksum_address(candidate.contract_address)
        fees = self._fee_fields()

        # No eth_call on the live path: getPublicDrop already rejected closed
        # windows, and an extra simulation RTT is enough for short free drops
        # to expire. Remaining reverts (supply, per-wallet cap) surface on send.

        if self.settings.dry_run:
            # One cheap sim in dry-run so Telegram reports real eligibility.
            sim_tx: dict = {
                "from": primary.address,
                "to": to_addr,
                "data": data,
                "value": value_wei,
                "gas": self.settings.gas_limit,
                "chainId": self.settings.chain_id,
                **fees,
            }
            try:
                self.w3.eth.call(sim_tx)
            except ContractLogicError as exc:
                err = friendly_revert(exc)
                if (
                    seadrop_mint_public_quantity(data) > 1
                    and "0x13da22f2" not in err.lower()
                ):
                    data = with_seadrop_mint_public_quantity(data, 1)
                    sim_tx["data"] = data
                    try:
                        self.w3.eth.call(sim_tx)
                        rewrite_note = f"{rewrite_note}; qty=1 retry"
                    except ContractLogicError as exc2:
                        err = friendly_revert(exc2)
                        skip = f"Simulation reverted: {err} | adapt={rewrite_note}"
                        return [(a.address, False, skip, None) for a in accounts]
                else:
                    skip = f"Simulation reverted: {err} | adapt={rewrite_note}"
                    return [(a.address, False, skip, None) for a in accounts]
            except Exception as exc:
                skip = f"Simulation failed: {exc} | adapt={rewrite_note}"
                return [(a.address, False, skip, None) for a in accounts]
            msg = (
                f"DRY_RUN OK — would mint {Web3.from_wei(value_wei, 'ether')} ETH "
                f"({rewrite_note}) [fast SeaDrop blast]"
            )
            return [(a.address, True, msg, None) for a in accounts]

        fee_cap = int(fees.get("maxFeePerGas") or fees.get("gasPrice") or 0)
        need = self.settings.gas_limit * fee_cap + int(value_wei)
        n = len(accounts)
        results: list[tuple[str, bool, str, str | None] | None] = [None] * n
        # Cached balance+nonce means prep is normally local-only. Broadcast
        # remains all-at-once because the public window is the bottleneck.
        workers = min(n, 15)
        window_closed = False

        def _sign(account: LocalAccount, nonce: int):
            tx = {
                "from": account.address,
                "to": to_addr,
                "data": data,
                "value": value_wei,
                "gas": self.settings.gas_limit,
                "chainId": self.settings.chain_id,
                "nonce": nonce,
                **fees,
            }
            signed = account.sign_transaction(tx)
            return getattr(signed, "raw_transaction", None) or signed.rawTransaction

        def _prep(account: LocalAccount, i: int) -> bytes | None:
            """Return signed raw tx bytes, or set results[i] on skip/fail."""
            nonlocal window_closed
            wallet = account.address
            if window_closed:
                results[i] = (
                    wallet,
                    False,
                    "Skipped: drop became NotActive while copying",
                    None,
                )
                return None
            try:
                bal, nonce = self.wallet_state(wallet)
                if bal < need:
                    results[i] = (
                        wallet,
                        False,
                        (
                            f"Insufficient ETH for gas+mint: have "
                            f"{Web3.from_wei(bal, 'ether')}, need ~"
                            f"{Web3.from_wei(need, 'ether')}"
                        ),
                        None,
                    )
                    return None
                return _sign(account, nonce)
            except Exception as exc:
                results[i] = (wallet, False, f"Prep failed: {exc}", None)
                log.warning("Fast SeaDrop prep failed for %s: %s", wallet, exc)
                return None

        def _broadcast(account: LocalAccount, i: int, raw: bytes | None) -> None:
            nonlocal window_closed
            wallet = account.address
            if results[i] is not None:
                return
            if raw is None:
                if results[i] is None:
                    results[i] = (wallet, False, "Prep failed: no signed tx", None)
                return
            if window_closed:
                results[i] = (
                    wallet,
                    False,
                    "Skipped: drop became NotActive while copying",
                    None,
                )
                return
            try:
                tx_hash = self.w3.eth.send_raw_transaction(raw)
                hex_hash = tx_hash.hex()
                if not hex_hash.startswith("0x"):
                    hex_hash = "0x" + hex_hash
                results[i] = (
                    wallet,
                    True,
                    f"Copy mint submitted ({rewrite_note}) [fast].",
                    hex_hash,
                )
                self.consume_wallet_state(wallet, need)
            except Exception as exc:
                text = str(exc).lower()
                if "nonce too low" in text:
                    try:
                        required_nonce = nonce_required_by_error(exc)
                        bal, fresh_nonce = self.force_wallet_state(
                            wallet, required_nonce
                        )
                        if bal < need:
                            raise RuntimeError(
                                "insufficient ETH after nonce refresh"
                            )
                        retry_raw = _sign(account, fresh_nonce)
                        tx_hash = self.w3.eth.send_raw_transaction(retry_raw)
                        hex_hash = tx_hash.hex()
                        if not hex_hash.startswith("0x"):
                            hex_hash = "0x" + hex_hash
                        results[i] = (
                            wallet,
                            True,
                            (
                                f"Copy mint submitted ({rewrite_note}) "
                                "[fast; nonce refreshed]."
                            ),
                            hex_hash,
                        )
                        self.consume_wallet_state(wallet, need)
                        log.info(
                            "Recovered stale nonce for %s: retried with %s",
                            wallet,
                            fresh_nonce,
                        )
                        return
                    except Exception as retry_exc:
                        exc = retry_exc
                        text = str(retry_exc).lower()
                if "0x13da22f2" in text or "notactive" in text:
                    window_closed = True
                    msg = (
                        "NotActive (public drop window closed or not started) "
                        "[0x13da22f2]"
                    )
                else:
                    msg = f"Broadcast failed: {exc}"
                results[i] = (wallet, False, msg, None)
                log.warning("Fast SeaDrop send failed for %s: %s", wallet, exc)

        log.info(
            "Fast SeaDrop blast for %s with %s wallets",
            candidate.source_tx_hash,
            n,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            signed_raw = list(
                pool.map(
                    lambda item: _prep(item[1], item[0]),
                    enumerate(accounts),
                )
            )
        # Blast sends as a separate wave so signing/RPC prep doesn't serialize
        # behind earlier wallets' broadcasts.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(
                pool.map(
                    lambda item: _broadcast(item[1], item[0], signed_raw[item[0]]),
                    enumerate(accounts),
                )
            )

        out: list[tuple[str, bool, str, str | None]] = []
        for i, account in enumerate(accounts):
            if results[i] is None:
                out.append((account.address, False, "Unknown fast-path failure", None))
            else:
                out.append(results[i])
        return out

    def try_copy(self, candidate: MintCandidate) -> tuple[bool, str, str | None]:
        """ Backward-compatible single-wallet copy (first minting wallet). """
        results = self.try_copy_all(candidate)
        _, ok, message, tx_hash = results[0]
        return ok, message, tx_hash

    def get_public_drop(self, nft_contract: str) -> dict[str, int | bool]:
        """
        Decode SeaDrop getPublicDrop(nft).

        ABI-encoded PublicDrop: mintPrice, startTime, endTime,
        maxTotalMintableByWallet, feeBps, restrictFeeRecipients.
        """
        selector = Web3.keccak(text="getPublicDrop(address)")[:4].hex()
        data = "0x" + selector + _addr_word(nft_contract)
        out = self.w3.eth.call(
            {"to": Web3.to_checksum_address(SEADROP), "data": data}
        )
        raw = out.hex() if hasattr(out, "hex") else bytes(out).hex()
        raw = raw[2:] if raw.startswith("0x") else raw
        if len(raw) < 64 * 3:
            return {
                "mint_price": 0,
                "start_time": 0,
                "end_time": 0,
                "max_per_wallet": 0,
                "fee_bps": 0,
                "restrict_fee_recipients": False,
            }

        def _word(i: int) -> int:
            return int(raw[64 * i : 64 * (i + 1)], 16)

        return {
            "mint_price": _word(0),
            "start_time": _word(1),
            "end_time": _word(2),
            "max_per_wallet": _word(3) if len(raw) >= 64 * 4 else 0,
            "fee_bps": _word(4) if len(raw) >= 64 * 5 else 0,
            "restrict_fee_recipients": bool(_word(5)) if len(raw) >= 64 * 6 else False,
        }

    def seadrop_unit_price(self, nft_contract: str) -> int:
        """Read SeaDrop getPublicDrop(nft).mintPrice (wei)."""
        return int(self.get_public_drop(nft_contract)["mint_price"])

    @staticmethod
    def _window_skip_message(drop: dict[str, int | bool], now: int | None = None) -> str | None:
        """Return a skip reason if the public drop window is not active."""
        start = int(drop.get("start_time") or 0)
        end = int(drop.get("end_time") or 0)
        if start <= 0 and end <= 0:
            return None
        if now is None:
            now = int(time.time())
        if now < start:
            return (
                f"NotActive (public drop not started yet — opens in "
                f"{start - now}s) [0x13da22f2]"
            )
        if end > 0 and now >= end:
            return (
                f"NotActive (public drop window closed {now - end}s ago) "
                f"[0x13da22f2]"
            )
        return None

    def _resolve_mint_value(
        self, candidate: MintCandidate, data: str
    ) -> tuple[int, str | None]:
        """
        Return (value_wei, skip_reason).

        SeaDrop public drops can require mintPrice even when the watched
        wallet's tx shows value=0 (price changed, or free window ended).
        Also skips early when getPublicDrop says the window is closed.
        """
        value = int(candidate.value_wei)
        if (
            candidate.contract_address.lower() != SEADROP
            or not data.startswith(SEADROP_MINT_PUBLIC)
        ):
            return value, None

        try:
            nft = seadrop_mint_public_nft(data)
            drop = self.get_public_drop(nft)
        except Exception as exc:
            log.warning("getPublicDrop failed: %s", exc)
            return value, None

        window_skip = self._window_skip_message(drop)
        if window_skip:
            return value, f"Skipped: {window_skip}"

        unit = int(drop["mint_price"])
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
                    bal, nonce = self.wallet_state(my_wallet)
                except Exception as exc:
                    return False, f"Wallet state check failed: {exc}", None
                if bal < need:
                    have_eth = Web3.from_wei(bal, "ether")
                    need_eth = Web3.from_wei(need, "ether")
                    return (
                        False,
                        f"Insufficient ETH for gas+mint: have {have_eth}, need ~{need_eth}",
                        None,
                    )

                tx["nonce"] = nonce
                signed = account.sign_transaction(tx)
                raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
                try:
                    tx_hash = self.w3.eth.send_raw_transaction(raw)
                except Exception as exc:
                    if "nonce too low" not in str(exc).lower():
                        raise
                    required_nonce = nonce_required_by_error(exc)
                    bal, fresh_nonce = self.force_wallet_state(
                        my_wallet, required_nonce
                    )
                    if bal < need:
                        return (
                            False,
                            "Insufficient ETH after nonce refresh",
                            None,
                        )
                    tx["nonce"] = fresh_nonce
                    signed = account.sign_transaction(tx)
                    raw = (
                        getattr(signed, "raw_transaction", None)
                        or signed.rawTransaction
                    )
                    tx_hash = self.w3.eth.send_raw_transaction(raw)
                    qty_note = f"{qty_note}; nonce refreshed"
                hex_hash = tx_hash.hex()
                if not hex_hash.startswith("0x"):
                    hex_hash = "0x" + hex_hash
                self.consume_wallet_state(my_wallet, need)
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
