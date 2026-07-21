from bot.mint_copy import SEADROP, SEADROP_MINT_PUBLIC, rewrite_calldata_for_my_wallet


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


def test_generic_address_rewrite():
    target = "0x" + "11" * 20
    mine = "0x" + "22" * 20
    raw = "0xa0712d68" + "0" * 24 + "11" * 20 + "0" * 64
    out, note = rewrite_calldata_for_my_wallet(raw, target, mine, "0x" + "33" * 20)
    assert "22" * 20 in out
    assert "11" * 20 not in out
    assert "replaced" in note
