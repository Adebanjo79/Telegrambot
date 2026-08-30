from __future__ import annotations

from eth_account import Account
from web3 import Web3

from bot.config import Settings
from bot.pending_watcher import PendingWalletWatcher
from bot.watcher import WalletWatcher


def _settings(**overrides) -> Settings:
    pk = "0x" + "ab" * 32
    target = Web3.to_checksum_address("0x" + "11" * 20)
    base = dict(
        telegram_bot_token="x",
        telegram_owner_id=1,
        rpc_url="https://rpc.example",
        rpc_urls=("https://rpc.example",),
        chain_id=4663,
        explorer_url="https://robinhoodchain.blockscout.com",
        target_wallets=(target,),
        private_keys=(pk,),
        my_wallets=(Account.from_key(pk).address,),
        private_key=pk,
        my_wallet=Account.from_key(pk).address,
        free_mints_only=True,
        dry_run=False,
        poll_interval_sec=1.0,
        gas_limit=500000,
        max_catchup_blocks=25,
        rpc_rate_limit=25.0,
        rpc_warn_percent=80.0,
        rpc_slow_ms=1500.0,
        rpc_load_balance=False,
        rpc_failback_sec=30.0,
        pending_detection=True,
        pending_ws_url="wss://robinhood-mainnet.g.alchemy.com/v2/test",
        pending_subscription="auto",
    )
    base.update(overrides)
    return Settings(**base)


def test_alchemy_pending_subscription_filters_target_wallet():
    settings = _settings()
    pending = PendingWalletWatcher(
        settings, WalletWatcher(settings, Web3())
    )
    assert pending.subscription_params() == [
        "alchemy_pendingTransactions",
        {
            "fromAddress": list(settings.target_wallets),
            "hashesOnly": False,
        },
    ]


def test_full_pending_transaction_becomes_candidate_without_receipt():
    from unittest.mock import MagicMock

    settings = _settings()
    w3 = MagicMock()
    watcher = WalletWatcher(settings, w3)
    pending = PendingWalletWatcher(settings, watcher)
    tx = {
        "from": settings.target_wallets[0],
        "to": "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",
        "input": (
            "0x161ac21f"
            + "000000000000000000000000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            + "0000000000000000000000000000a26b00c1f0df003000390027140000faa719"
            + "000000000000000000000000"
            + settings.target_wallets[0][2:].lower()
            + "0000000000000000000000000000000000000000000000000000000000000001"
        ),
        "value": "0x0",
        "hash": "0x" + "dd" * 32,
    }

    candidate = pending.candidate_from_result(tx)
    assert candidate is not None
    assert candidate.block_number == 0
    assert candidate.method_hint == "SeaDrop mintPublic"
    w3.eth.get_transaction_receipt.assert_not_called()

    # Duplicate notifications and hash-only feeds are ignored.
    assert pending.candidate_from_result(tx) is None
    assert pending.candidate_from_result(tx["hash"]) is None
    assert "hashes only" in pending.last_error
