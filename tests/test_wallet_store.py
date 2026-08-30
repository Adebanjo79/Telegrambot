from pathlib import Path

from eth_account import Account

from bot.wallet_store import load_extra_keys, merge_keys, save_keys


def test_wallet_store_roundtrip(tmp_path: Path):
    store = tmp_path / "mint_wallets.json"
    pk1 = "0x" + "11" * 32
    pk2 = "0x" + "22" * 32
    save_keys([pk1, pk2], store)
    loaded = load_extra_keys(store)
    assert len(loaded) == 2
    addrs = {Account.from_key(k).address.lower() for k in loaded}
    assert Account.from_key(pk1).address.lower() in addrs
    assert Account.from_key(pk2).address.lower() in addrs


def test_merge_keys_dedupes(tmp_path: Path):
    store = tmp_path / "mint_wallets.json"
    pk = "0x" + "ab" * 32
    save_keys([pk], store)
    merged = merge_keys((pk,), store)
    assert len(merged) == 1
