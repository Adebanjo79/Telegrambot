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
    network_name: str
    target_wallets: tuple[str, ...]
    private_key: str
    my_wallet: str
    free_mints_only: bool
    dry_run: bool
    poll_interval_sec: float
    gas_limit: int
    max_catchup_blocks: int

    @staticmethod
    def network_label(chain_id: int) -> str:
        return {
            1: "Ethereum Mainnet",
            11155111: "Ethereum Sepolia",
            4663: "Robinhood Chain",
            46630: "Robinhood Chain Testnet",
        }.get(chain_id, f"Chain {chain_id}")

    @classmethod
    def load(cls, env_file: str | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        private_key = _req("PRIVATE_KEY").strip().strip('"').strip("'")
        if private_key.lower() in {
            "0xyourprivatekeyhere",
            "yourprivatekeyhere",
            "0x...",
        }:
            raise ValueError(
                "PRIVATE_KEY is still the placeholder. Put your real wallet private key "
                "(64 hex chars, optional 0x prefix). No spaces or quotes."
            )
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        hex_body = private_key[2:]
        if len(hex_body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in hex_body):
            raise ValueError(
                "PRIVATE_KEY must be 64 hexadecimal characters after optional 0x "
                "(example shape: 0x followed by 64 chars 0-9/a-f). "
                "Remove spaces, quotes, and placeholder text."
            )

        try:
            account = Account.from_key(private_key)
        except Exception as exc:
            raise ValueError(f"PRIVATE_KEY is invalid: {exc}") from exc

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

        chain_id = int(_opt("CHAIN_ID", "4663"))
        default_explorer = {
            1: "https://etherscan.io",
            11155111: "https://sepolia.etherscan.io",
            4663: "https://robinhoodchain.blockscout.com",
            46630: "https://explorer.testnet.chain.robinhood.com",
        }.get(chain_id, "https://robinhoodchain.blockscout.com")
        default_rpc = {
            1: "https://ethereum.publicnode.com",
            11155111: "https://ethereum-sepolia-rpc.publicnode.com",
            4663: "https://rpc.mainnet.chain.robinhood.com",
            46630: "https://rpc.testnet.chain.robinhood.com",
        }.get(chain_id, "https://rpc.mainnet.chain.robinhood.com")

        return cls(
            telegram_bot_token=_req("TELEGRAM_BOT_TOKEN"),
            telegram_owner_id=owner_id,
            rpc_url=_opt("RPC_URL", default_rpc),
            chain_id=chain_id,
            explorer_url=_opt("EXPLORER_URL", default_explorer).rstrip("/"),
            network_name=_opt("NETWORK_NAME", cls.network_label(chain_id)),
            target_wallets=targets,
            private_key=private_key,
            my_wallet=account.address,
            free_mints_only=_parse_bool(_opt("FREE_MINTS_ONLY", "true"), True),
            dry_run=_parse_bool(_opt("DRY_RUN", "true"), True),
            poll_interval_sec=float(_opt("POLL_INTERVAL_SEC", "1.0")),
            gas_limit=int(_opt("GAS_LIMIT", "500000")),
            max_catchup_blocks=int(_opt("MAX_CATCHUP_BLOCKS", "25")),
        )
