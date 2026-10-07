"""Encoding/decoding of the hand-written program client."""

import hashlib
import json
import struct
from pathlib import Path

import pytest
from solana.rpc.core import RPCException
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from escrow_backend.solana.gateway import (
    ACCOUNT_ALREADY_IN_USE,
    ChainError,
    RpcGateway,
    parse_program_error,
)
from escrow_backend.solana.program import (
    Config,
    EscrowProgram,
    Hold,
    HoldStatus,
    UserVault,
    account_disc,
    decode_clock_unix_timestamp,
    decode_token_amount,
    error_name,
    ix_disc,
)

IDL = Path(__file__).resolve().parents[2] / "idl" / "card_escrow.json"
PK = [Pubkey.new_unique() for _ in range(6)]


def test_discriminators_match_anchor_convention() -> None:
    assert ix_disc("authorize") == hashlib.sha256(b"global:authorize").digest()[:8]
    assert account_disc("Hold") == hashlib.sha256(b"account:Hold").digest()[:8]


@pytest.mark.skipif(not IDL.exists(), reason="IDL not built")
def test_discriminators_and_accounts_match_idl() -> None:
    idl = json.loads(IDL.read_text())
    program = EscrowProgram(Pubkey.from_string(idl["address"]), PK[0])
    for ix in idl["instructions"]:
        assert bytes(ix["discriminator"]) == ix_disc(ix["name"]), ix["name"]
    for acc in idl["accounts"]:
        assert bytes(acc["discriminator"]) == account_disc(acc["name"])
    # Account order of the builders must follow the IDL.
    o, a = PK[1], b"\x01" * 32
    built = {
        "authorize": program.authorize(o, o, o, a, 1),
        "capture": program.capture(o, o, a, PK[2], 1),
        "release": program.release(o, o, a),
        "expire_hold": program.expire_hold(o, a),
        "refund": program.refund(o, o, o, PK[2], a, 1),
        "deposit": program.deposit(o, PK[2], 1),
        "withdraw": program.withdraw(o, PK[2], 1),
        "open_vault": program.open_vault(o, 1, 1, 60),
        "set_limits": program.set_limits(o, 1, 1, 60),
        "set_paused": program.set_paused(o, True),
        "update_config": program.update_config(o, PK[2]),
        "initialize_config": program.initialize_config(o, PK[2], o, o, 60),
        "close_hold": program.close_hold(o, PK[3], o),
    }
    for ix in idl["instructions"]:
        metas = built[ix["name"]].accounts
        assert len(metas) == len(ix["accounts"]), ix["name"]
        for meta, spec in zip(metas, ix["accounts"], strict=True):
            assert meta.is_writable == spec.get("writable", False), (ix["name"], spec["name"])
            assert meta.is_signer == spec.get("signer", False), (ix["name"], spec["name"])
            if "address" in spec:
                assert str(meta.pubkey) == spec["address"], (ix["name"], spec["name"])


def test_decode_accounts() -> None:
    cfg = (
        account_disc("Config")
        + b"".join(bytes(p) for p in PK[:5])
        + struct.pack("<?qB", True, 600, 254)
    )
    c = Config.decode(cfg)
    assert (c.admin, c.paused, c.default_hold_ttl_seconds, c.bump) == (PK[0], True, 600, 254)

    vault = (
        account_disc("UserVault")
        + bytes(PK[0])
        + struct.pack("<QQQqIqqIB", 1, 2, 3, 4, 5, 6, 7, 8, 9)
    )
    v = UserVault.decode(vault)
    assert (v.held_total, v.velocity_max_auths, v.window_count, v.bump) == (1, 5, 8, 9)

    hold = (
        account_disc("Hold")
        + bytes(PK[1])
        + b"\x07" * 32
        + struct.pack("<QQBqq", 100, 40, 1, 10, 20)
        + bytes(PK[2])
        + b"\xff"
    )
    h = Hold.decode(hold)
    assert (h.amount, h.captured_amount, h.status, h.expires_ts, h.rent_payer) == (
        100,
        40,
        HoldStatus.CAPTURED,
        20,
        PK[2],
    )
    with pytest.raises(ValueError):
        Hold.decode(vault)


def test_decode_token_and_clock() -> None:
    assert decode_token_amount(b"\x00" * 64 + struct.pack("<Q", 42) + b"\x00" * 100) == 42
    assert decode_clock_unix_timestamp(struct.pack("<QqQQq", 1, 2, 3, 4, 99)) == 99


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("AnchorError ... Error Code: Paused. Error Number: 6001.", "Paused"),
        ("custom program error: 0x1773", "InsufficientAvailableBalance"),
        ("InstructionError(0, Custom(6006))", "HoldNotPending"),
        ("Allocate: account Address { .. } already in use", ACCOUNT_ALREADY_IN_USE),
        ("custom program error: 0x0", ACCOUNT_ALREADY_IN_USE),
        ("InstructionErrorCustom(Custom(0))", ACCOUNT_ALREADY_IN_USE),
        ("something else", None),
    ],
)
def test_parse_program_error(text: str, name: str | None) -> None:
    assert parse_program_error(text) == name


def test_error_name_bounds() -> None:
    assert error_name(6000) == "Unauthorized"
    assert error_name(5999) is None
    assert error_name(7000) is None


async def test_already_processed_is_transient_and_drops_the_blockhash() -> None:
    """Identical bytes may belong to another operation (same capture amount):
    never report its signature as ours; retry with a fresh blockhash instead."""
    program = EscrowProgram(PK[0], PK[1])
    gw = RpcGateway("http://127.0.0.1:1", program, Keypair(), None)

    async def already_processed(*_: object, **__: object) -> None:
        raise RPCException(
            {"code": -32002, "message": "This transaction has already been processed"}
        )

    gw._blockhash = (Hash.new_unique(), float("inf"))
    gw.client.send_transaction = already_processed  # type: ignore[method-assign]
    with pytest.raises(ChainError) as e:
        await gw.authorize(PK[2], bytes(32), 1)
    assert e.value.transient and e.value.name is None
    assert gw._blockhash is None
    await gw.close()
