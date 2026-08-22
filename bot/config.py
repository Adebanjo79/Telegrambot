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


def _normalize_rpc_url(raw: str) -> str:
    """
    Accept a bare https URL. Strip a pasted `RPC_URL=` / `RPC_URLS=` prefix
    so a .env typo does not become the request URL.
    """
    url = raw.strip().strip('"').strip("'")
    lowered = url.lower()
    for prefix in ("rpc_urls=", "rpc_url="):
        if lowered.startswith(prefix):
            url = url[len(prefix) :].strip().strip('"').strip("'")
            break
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            "Each RPC URL must start with http:// or https://. "
            "Do not include RPC_URL= in the value — only the URL."
        )
    return url


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
    rpc_urls: tuple[str, ...]
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
    rpc_rate_limit: float
    rpc_warn_percent: float
    rpc_slow_ms: float
    rpc_load_balance: bool
    rpc_failback_sec: float
    pending_detection: bool = False
    pending_ws_url: str = ""
    pending_subscription: str = "auto"
    wallet_state_refresh_sec: float = 5.0
    wallet_state_ttl_sec: float = 15.0

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

        # RPC_URLS=primary,backup enables automatic failover between nodes.
        rpc_urls = tuple(
            _normalize_rpc_url(url)
            for url in _opt(
                "RPC_URLS",
                _opt("RPC_URL", "https://rpc.mainnet.chain.robinhood.com"),
            ).split(",")
            if url.strip()
        )
        if not rpc_urls:
            raise ValueError("RPC_URL/RPC_URLS must contain at least one endpoint")

        pending_detection = _parse_bool(
            _opt("PENDING_DETECTION", "false"), False
        )
        pending_ws_url = _opt("PENDING_WS_URL", "")
        if pending_detection and not pending_ws_url:
            raise ValueError(
                "PENDING_DETECTION=true requires PENDING_WS_URL=wss://..."
            )
        if pending_ws_url and not pending_ws_url.startswith(("ws://", "wss://")):
            raise ValueError("PENDING_WS_URL must start with ws:// or wss://")
        pending_subscription = _opt("PENDING_SUBSCRIPTION", "auto").lower()
        if pending_subscription not in {"auto", "alchemy", "full"}:
            raise ValueError(
                "PENDING_SUBSCRIPTION must be auto, alchemy, or full"
            )

        return cls(
            telegram_bot_token=_req("TELEGRAM_BOT_TOKEN"),
            telegram_owner_id=owner_id,
            rpc_url=rpc_urls[0],
            rpc_urls=rpc_urls,
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
            max_catchup_blocks=int(_opt("MAX_CATCHUP_BLOCKS", "100")),
            # Your plan's requests/second cap, used for "almost full" warnings.
            rpc_rate_limit=float(_opt("RPC_RATE_LIMIT", "25")),
            rpc_warn_percent=float(_opt("RPC_WARN_PERCENT", "80")),
            rpc_slow_ms=float(_opt("RPC_SLOW_MS", "1500")),
            # true = spread requests across every RPC_URLS entry instead of
            # keeping the extras purely as backups.
            rpc_load_balance=_parse_bool(_opt("RPC_LOAD_BALANCE", "true"), True),
            # After this many seconds on a backup, probe the primary and return.
            rpc_failback_sec=float(_opt("RPC_FAILBACK_SEC", "30")),
            pending_detection=pending_detection,
            pending_ws_url=pending_ws_url,
            pending_subscription=pending_subscription,
            wallet_state_refresh_sec=max(
                1.0, float(_opt("WALLET_STATE_REFRESH_SEC", "5"))
            ),
            wallet_state_ttl_sec=max(
                2.0, float(_opt("WALLET_STATE_TTL_SEC", "15"))
            ),
        )
