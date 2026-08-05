from __future__ import annotations

import json
import logging
from pathlib import Path

from eth_account import Account

from bot.config import _normalize_private_key

log = logging.getLogger(__name__)

DEFAULT_STORE = Path("mint_wallets.json")


def load_extra_keys(path: Path = DEFAULT_STORE) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = data.get("private_keys") or []
        out: list[str] = []
        for raw in keys:
            try:
                out.append(_normalize_private_key(str(raw)))
            except Exception as exc:
                log.warning("Skipping invalid key in %s: %s", path, exc)
        return out
    except Exception as exc:
        log.warning("Failed to read %s: %s", path, exc)
        return []


def save_keys(keys: list[str], path: Path = DEFAULT_STORE) -> None:
    normalized = [_normalize_private_key(k) for k in keys]
    # de-dupe by address
    by_addr: dict[str, str] = {}
    for key in normalized:
        by_addr[Account.from_key(key).address.lower()] = key
    payload = {"private_keys": list(by_addr.values())}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def merge_keys(env_keys: tuple[str, ...], path: Path = DEFAULT_STORE) -> list[str]:
    """Env keys first, then extras from store (unique by address)."""
    merged: dict[str, str] = {}
    for key in list(env_keys) + load_extra_keys(path):
        addr = Account.from_key(key).address.lower()
        merged[addr] = key
    return list(merged.values())
