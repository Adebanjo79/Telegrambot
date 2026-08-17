from decimal import Decimal
from unittest.mock import MagicMock
import time

from eth_account import Account
from web3 import Web3

from bot.config import Settings
from bot.mint_copy import (
    SEADROP,
    SEADROP_MINT_PUBLIC,
    MintCopyService,
    normalize_seadrop_mint_public,
    rewrite_calldata_for_my_wallet,
    seadrop_mint_public_quantity,
    with_seadrop_mint_public_quantity,
)
from bot.models import MintCandidate


def _settings(**overrides) -> Settings:
    pk = "0x" + "ab" * 32
    base = dict(
        telegram_bot_token="x",
        telegram_owner_id=1,
        rpc_url="https://rpc.mainnet.chain.robinhood.com",
        rpc_urls=("https://rpc.mainnet.chain.robinhood.com",),
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
        rpc_rate_limit=25.0,
        rpc_warn_percent=80.0,
        rpc_slow_ms=1500.0,
        rpc_load_balance=False,
        rpc_failback_sec=30.0,
    )
    base.update(overrides)
    return Settings(**base)


def _seadrop_public_candidate(target: str, qty: int = 1) -> MintCandidate:
    data = (
        SEADROP_MINT_PUBLIC
        + "000000000000000000000000cc69a57113ab3b9822a3b202d704611971ece5c7"
        + "0000000000000000000000000000a26b00c1f0df003000390027140000faa719"
        + "000000000000000000000000"
        + target[2:].lower()
        + f"{qty:064x}"
    )
    return MintCandidate(
        source_tx_hash="0x" + "aa" * 32,
        contract_address=Web3.to_checksum_address(SEADROP),
        input_data=data,
        value_wei=0,
        value_eth=Decimal(0),
        block_number=1,
        method_hint="SeaDrop mintPublic",
        target_wallet=target,
    )


def test_seadrop_mint_public_rewrites_minter_to_zero():
    target = "0xA0c9EA7Dcd2a50FC7aD758B96356e99f04b9861d"
    mine = "0x545C60Ee00fE80E2d00C9Ef7E682A68F2F94D4A5"
    # Real shape from Robinhood Chain SeaDrop mintPublic tx
    raw = (
        SEADROP_MINT_PUBLIC
        + "000000000000000000000000cc69a57113ab3b9822a3b202d704611971ece5c7"
        + "0000000000000000000000000000a26b00c1f0df003000390027140000faa719"
        + "000000000000000000000000a0c9ea7dcd2a50fc7ad758b96356e99f04b9861d"
        + "0000000000000000000000000000000000000000000000000000000000000005"
    )
    out, note = rewrite_calldata_for_my_wallet(raw, target, mine, SEADROP)
    assert out.startswith(SEADROP_MINT_PUBLIC)
    # third word must be zero
    body = out[10:]
    assert body[64 * 2 : 64 * 3] == "0" * 64
    # quantity unchanged
    assert body[64 * 3 :] == "0" * 63 + "5"
    assert "SeaDrop" in note


def test_seadrop_strips_trailing_junk_and_qty_retry_stays_valid():
    target = "0xA0c9EA7Dcd2a50FC7aD758B96356e99f04b9861d"
    mine = "0x545C60Ee00fE80E2d00C9Ef7E682A68F2F94D4A5"
    raw = (
        SEADROP_MINT_PUBLIC
        + "000000000000000000000000b68ea16af1356d88395242502d95bc097b9f517c"
        + "0000000000000000000000000000a26b00c1f0df003000390027140000faa719"
        + "000000000000000000000000a0c9ea7dcd2a50fc7ad758b96356e99f04b9861d"
        + "0000000000000000000000000000000000000000000000000000000000000003"
        + "3d958fe2"  # trailing junk seen on Robinhood txs
    )
    out, _ = rewrite_calldata_for_my_wallet(raw, target, mine, SEADROP)
    assert out == normalize_seadrop_mint_public(out)
    assert seadrop_mint_public_quantity(out) == 3
    qty1 = with_seadrop_mint_public_quantity(out, 1)
    assert seadrop_mint_public_quantity(qty1) == 1
    assert len(qty1) == 10 + 64 * 4


def test_generic_address_rewrite():
    target = "0x" + "11" * 20
    mine = "0x" + "22" * 20
    raw = "0xa0712d68" + "0" * 24 + "11" * 20 + "0" * 64
    out, note = rewrite_calldata_for_my_wallet(raw, target, mine, "0x" + "33" * 20)
    assert "22" * 20 in out
    assert "11" * 20 not in out
    assert "replaced" in note


def test_window_skip_message_closed_and_future():
    closed = MintCopyService._window_skip_message(
        {"start_time": 1, "end_time": 100}, now=150
    )
    assert closed is not None
    assert "closed" in closed.lower()
    assert "0x13da22f2" in closed

    future = MintCopyService._window_skip_message(
        {"start_time": 200, "end_time": 300}, now=150
    )
    assert future is not None
    assert "not started" in future.lower()

    open_win = MintCopyService._window_skip_message(
        {"start_time": 100, "end_time": 300}, now=150
    )
    assert open_win is None


def test_closed_public_drop_skips_before_sim():
    pk1 = "0x" + "ab" * 32
    settings = _settings(
        dry_run=False,
        private_keys=(pk1,),
        my_wallets=(Account.from_key(pk1).address,),
        private_key=pk1,
        my_wallet=Account.from_key(pk1).address,
    )
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    w3.to_wei.return_value = 50_000_000
    # mintPrice=0, start=1, end=2 (already closed)
    now = int(time.time())
    end = now - 30
    start = end - 60
    raw = (
        f"{0:064x}"
        + f"{start:064x}"
        + f"{end:064x}"
        + f"{1:064x}"
        + f"{0:064x}"
        + f"{0:064x}"
    )
    w3.eth.call.return_value = bytes.fromhex(raw)

    service = MintCopyService(settings, w3)
    candidate = _seadrop_public_candidate(settings.target_wallets[0], qty=1)
    results = service.try_copy_all(candidate)
    assert len(results) == 1
    assert results[0][1] is False
    assert "closed" in results[0][2].lower()
    w3.eth.send_raw_transaction.assert_not_called()


def test_free_seadrop_uses_fast_path_single_simulation():
    pk1 = "0x" + "ab" * 32
    pk2 = "0x" + "cd" * 32
    pk3 = "0x" + "ef" * 32
    addrs = tuple(Account.from_key(k).address for k in (pk1, pk2, pk3))
    settings = _settings(
        dry_run=True,
        private_keys=(pk1, pk2, pk3),
        my_wallets=addrs,
        private_key=pk1,
        my_wallet=addrs[0],
    )
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    w3.to_wei.return_value = 50_000_000
    now = int(time.time())
    # Open free window
    raw = (
        f"{0:064x}"
        + f"{now - 10:064x}"
        + f"{now + 3600:064x}"
        + f"{5:064x}"
        + f"{0:064x}"
        + f"{0:064x}"
    )
    w3.eth.call.return_value = bytes.fromhex(raw)

    service = MintCopyService(settings, w3)
    candidate = _seadrop_public_candidate(settings.target_wallets[0], qty=1)
    results = service.try_copy_all(candidate)
    assert len(results) == 3
    assert all(ok for _, ok, msg, _ in results)
    assert all("fast SeaDrop blast" in msg for _, _, msg, _ in results)


def test_free_seadrop_fast_path_live_blasts_all_wallets():
    pk1 = "0x" + "ab" * 32
    pk2 = "0x" + "cd" * 32
    addrs = tuple(Account.from_key(k).address for k in (pk1, pk2))
    settings = _settings(
        dry_run=False,
        private_keys=(pk1, pk2),
        my_wallets=addrs,
        private_key=pk1,
        my_wallet=addrs[0],
    )
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    now = int(time.time())
    raw = (
        f"{0:064x}"
        + f"{now - 10:064x}"
        + f"{now + 3600:064x}"
        + f"{5:064x}"
        + f"{0:064x}"
        + f"{0:064x}"
    )
    w3.eth.call.return_value = bytes.fromhex(raw)
    w3.to_wei.return_value = 50_000_000
    w3.eth.get_balance.return_value = 10**18
    w3.eth.get_transaction_count.side_effect = [0, 0]
    sent = []

    class _Hash:
        def __init__(self, n: int) -> None:
            self._n = n

        def hex(self) -> str:
            return f"{self._n:064x}"

    def _send(raw_tx):
        sent.append(raw_tx)
        return _Hash(len(sent))

    w3.eth.send_raw_transaction.side_effect = _send
    service = MintCopyService(settings, w3)
    candidate = _seadrop_public_candidate(settings.target_wallets[0], qty=1)
    results = service.try_copy_all(candidate)
    assert len(results) == 2
    assert all(ok for _, ok, _, _ in results)
    assert all(h is not None for *_, h in results)
    assert len(sent) == 2
    assert all("[fast]" in msg for _, _, msg, _ in results)
