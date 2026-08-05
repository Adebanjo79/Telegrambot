"""Unit tests that do not require live Telegram or a funded wallet."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from web3 import Web3

from bot.models import MintCandidate
from bot.watcher import (
    MINT_SELECTORS,
    TRANSFER_TOPIC,
    WalletWatcher,
    ZERO_ADDR,
    _normalize_hex,
    _topic_address,
)
from bot.config import Settings
from bot.mint_copy import MintCopyService


def _settings(**overrides) -> Settings:
    pk = "0x" + "ab" * 32
    base = dict(
        telegram_bot_token="x",
        telegram_owner_id=1,
        rpc_url="https://rpc.mainnet.chain.robinhood.com",
        chain_id=4663,
        explorer_url="https://robinhoodchain.blockscout.com",
        target_wallets=(Web3.to_checksum_address("0x" + "11" * 20),),
        private_keys=(pk,),
        my_wallets=(Web3.to_checksum_address("0x" + "22" * 20),),
        private_key=pk,
        my_wallet=Web3.to_checksum_address("0x" + "22" * 20),
        free_mints_only=True,
        dry_run=True,
        poll_interval_sec=1.0,
        gas_limit=500000,
        max_catchup_blocks=25,
    )
    base.update(overrides)
    return Settings(**base)


def test_normalize_hex_and_topic_address():
    assert _normalize_hex(b"\x01\x02") == "0x0102"
    assert _topic_address("0x" + "0" * 24 + "11" * 20) == "0x" + "11" * 20


def test_mint_selectors_do_not_include_approvals():
    # setApprovalForAll must never be treated as a mint
    assert "0xa22cb465" not in MINT_SELECTORS
    assert "0xa0712d68" in MINT_SELECTORS


def test_watcher_ignores_non_target_and_non_mint():
    settings = _settings()
    w3 = MagicMock()
    watcher = WalletWatcher(settings, w3)

    foreign = {
        "from": "0x" + "33" * 20,
        "to": "0x" + "44" * 20,
        "input": "0xa0712d68" + "0" * 64,
        "value": 0,
        "hash": "0x" + "aa" * 32,
    }
    assert watcher._inspect_tx(foreign, 1) is None

    non_mint = {
        "from": settings.target_wallets[0],
        "to": "0x" + "44" * 20,
        "input": "0xa22cb465" + "0" * 64,  # setApprovalForAll
        "value": 0,
        "hash": "0x" + "bb" * 32,
    }
    w3.eth.get_transaction_receipt.return_value = {"logs": []}
    assert watcher._inspect_tx(non_mint, 2) is None


def test_watcher_detects_selector_mint():
    settings = _settings()
    w3 = MagicMock()
    watcher = WalletWatcher(settings, w3)
    w3.eth.get_transaction_receipt.return_value = {"logs": []}

    tx = {
        "from": settings.target_wallets[0],
        "to": "0x" + "44" * 20,
        "input": "0xa0712d68" + "0" * 64,
        "value": 0,
        "hash": "0x" + "cc" * 32,
    }
    candidate = watcher._inspect_tx(tx, 99)
    assert candidate is not None
    assert candidate.method_hint == "mint(uint256)"
    assert candidate.block_number == 99


def test_watcher_detects_transfer_from_zero():
    settings = _settings()
    w3 = MagicMock()
    watcher = WalletWatcher(settings, w3)

    from_topic = "0x" + "0" * 24 + ZERO_ADDR[2:]
    to_topic = "0x" + "0" * 24 + "11" * 20
    w3.eth.get_transaction_receipt.return_value = {
        "logs": [
            {
                "topics": [
                    TRANSFER_TOPIC,
                    from_topic,
                    to_topic,
                    "0x" + "0" * 63 + "1",
                ]
            }
        ]
    }

    tx = {
        "from": settings.target_wallets[0],
        "to": "0x" + "55" * 20,
        "input": "0xdeadbeef" + "0" * 64,  # unknown selector, mint via receipt
        "value": 0,
        "hash": "0x" + "dd" * 32,
    }
    candidate = watcher._inspect_tx(tx, 100)
    assert candidate is not None
    assert "Transfer from 0x0" in candidate.method_hint


def test_mint_copy_dry_run_success():
    settings = _settings(dry_run=True)
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    w3.eth.call.return_value = b""
    w3.to_wei.return_value = 50_000_000
    service = MintCopyService(settings, w3)

    candidate = MintCandidate(
        source_tx_hash="0x" + "ee" * 32,
        contract_address=Web3.to_checksum_address("0x" + "66" * 20),
        input_data="0xa0712d68" + "0" * 64,
        value_wei=0,
        value_eth=Decimal(0),
        block_number=1,
        method_hint="mint(uint256)",
        target_wallet=settings.target_wallets[0],
    )
    ok, message, tx_hash = service.try_copy(candidate)
    assert ok is True
    assert "DRY_RUN" in message
    assert tx_hash is None
    assert service.already_copied(candidate.source_tx_hash)


def test_try_copy_all_runs_for_each_wallet():
    pk1 = "0x" + "ab" * 32
    pk2 = "0x" + "cd" * 32
    from eth_account import Account

    settings = _settings(
        dry_run=True,
        private_keys=(pk1, pk2),
        my_wallets=(Account.from_key(pk1).address, Account.from_key(pk2).address),
        private_key=pk1,
        my_wallet=Account.from_key(pk1).address,
    )
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    w3.eth.call.return_value = b""
    w3.to_wei.return_value = 50_000_000
    service = MintCopyService(settings, w3)
    candidate = MintCandidate(
        source_tx_hash="0x" + "11" * 32,
        contract_address=Web3.to_checksum_address("0x" + "66" * 20),
        input_data="0xa0712d68" + "0" * 64,
        value_wei=0,
        value_eth=Decimal(0),
        block_number=1,
        method_hint="mint(uint256)",
        target_wallet=settings.target_wallets[0],
    )
    results = service.try_copy_all(candidate)
    assert len(results) == 2
    assert all(ok for _, ok, _, _ in results)
