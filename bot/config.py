from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3


def _req(key: str) -> str:
    value = os.getenv(key)
    if not value or not value.strip():
        raise ValueError(f"Missing required env var: {key}")
    return value.strip()


def _opt(key: str, default: str) -> str:
    value = os.getenv(key)
    return value.strip() if value and value.strip() else default


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_private_key(raw: str) -> str:
    private_key = raw.strip().strip('"').strip("'")
    if private_key.lower() in {
        "0xyourprivatekeyhere",
        "yourprivatekeyhere",
        "0x...",
        "",
    }:
        raise ValueError(
            "PRIVATE_KEY/PRIVATE_KEYS still has a placeholder. Put real wallet private keys "
            "(64 hex chars, optional 0x prefix). No spaces or quotes."
        )
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    hex_body = private_key[2:]
    if len(hex_body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in hex_body):
        raise ValueError(
            "Each private key must be 64 hexadecimal characters after optional 0x."
        )
    try:
        Account.from_key(private_key)
    except Exception as exc:
        raise ValueError(f"Private key is invalid: {exc}") from exc
    return private_key


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_owner_id: int
    rpc_url: str
    chain_id: int
    explorer_url: str
    target_wallets: tuple[str, ...]
    private_keys: tuple[str, ...]
    my_wallets: tuple[str, ...]
    private_key: str
    my_wallet: str
    free_mints_only: bool
    dry_run: bool
    poll_interval_sec: float
    gas_limit: int
    max_catchup_blocks: int

    @classmethod
    def load(cls, env_file: str | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        # Prefer PRIVATE_KEYS=key1,key2,... ; fall back to single PRIVATE_KEY
        raw_keys = os.getenv("PRIVATE_KEYS", "").strip()
        if raw_keys:
            key_parts = [p.strip() for p in raw_keys.split(",") if p.strip()]
        else:
            key_parts = [_req("PRIVATE_KEY")]

        private_keys = tuple(_normalize_private_key(k) for k in key_parts)
        my_wallets = tuple(Account.from_key(k).address for k in private_keys)

        raw_targets = _req("TARGET_WALLETS")
        try:
            targets = tuple(
                Web3.to_checksum_address(addr.strip())
                for addr in raw_targets.split(",")
                if addr.strip()
            )
        except Exception as exc:
            raise ValueError(
                "TARGET_WALLETS must be one or more 0x addresses, comma-separated. "
                f"Details: {exc}"
            ) from exc
        if not targets:
            raise ValueError("TARGET_WALLETS must include at least one address")
        if any(t.lower() == "0xtargetwalletaddresshere" for t in targets):
            raise ValueError(
                "TARGET_WALLETS is still the placeholder. Put the wallet address you want to copy."
            )

        try:
            owner_id = int(_req("TELEGRAM_OWNER_ID"))
        except ValueError as exc:
            raise ValueError(
                "TELEGRAM_OWNER_ID must be your numeric Telegram user id from @userinfobot"
            ) from exc

        return cls(
            telegram_bot_token=_req("TELEGRAM_BOT_TOKEN"),
            telegram_owner_id=owner_id,
            rpc_url=_opt("RPC_URL", "https://rpc.mainnet.chain.robinhood.com"),
            chain_id=int(_opt("CHAIN_ID", "4663")),
            explorer_url=_opt("EXPLORER_URL", "https://robinhoodchain.blockscout.com").rstrip(
                "/"
            ),
            target_wallets=targets,
            private_keys=private_keys,
            my_wallets=my_wallets,
            private_key=private_keys[0],
            my_wallet=my_wallets[0],
            free_mints_only=_parse_bool(_opt("FREE_MINTS_ONLY", "true"), True),
            dry_run=_parse_bool(_opt("DRY_RUN", "true"), True),
            poll_interval_sec=float(_opt("POLL_INTERVAL_SEC", "1.0")),
            gas_limit=int(_opt("GAS_LIMIT", "500000")),
            max_catchup_blocks=int(_opt("MAX_CATCHUP_BLOCKS", "25")),
        )
