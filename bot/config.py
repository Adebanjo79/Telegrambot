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


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_owner_id: int
    rpc_url: str
    chain_id: int
    explorer_url: str
    target_wallets: tuple[str, ...]
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

        private_key = _req("PRIVATE_KEY")
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        account = Account.from_key(private_key)
        raw_targets = _req("TARGET_WALLETS")
        targets = tuple(
            Web3.to_checksum_address(addr.strip())
            for addr in raw_targets.split(",")
            if addr.strip()
        )
        if not targets:
            raise ValueError("TARGET_WALLETS must include at least one address")

        return cls(
            telegram_bot_token=_req("TELEGRAM_BOT_TOKEN"),
            telegram_owner_id=int(_req("TELEGRAM_OWNER_ID")),
            rpc_url=_opt("RPC_URL", "https://rpc.mainnet.chain.robinhood.com"),
            chain_id=int(_opt("CHAIN_ID", "4663")),
            explorer_url=_opt("EXPLORER_URL", "https://robinhoodchain.blockscout.com").rstrip(
                "/"
            ),
            target_wallets=targets,
            private_key=private_key,
            my_wallet=account.address,
            free_mints_only=_parse_bool(_opt("FREE_MINTS_ONLY", "true"), True),
            dry_run=_parse_bool(_opt("DRY_RUN", "true"), True),
            poll_interval_sec=float(_opt("POLL_INTERVAL_SEC", "1.0")),
            gas_limit=int(_opt("GAS_LIMIT", "500000")),
            max_catchup_blocks=int(_opt("MAX_CATCHUP_BLOCKS", "25")),
        )
