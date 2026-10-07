"""Integration tests against a real cluster (localnet by default).

    scripts/localnet.sh start
    escrow-backend bootstrap --cluster localnet --out /tmp/localnet.json
    ESCROW_IT_BOOTSTRAP=/tmp/localnet.json pytest -m integration

Exercises `RpcGateway` end to end: snapshots, authorize/capture/release/refund,
on-chain idempotency and error mapping.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from escrow_backend.ids import auth_id_bytes, refund_id_bytes
from escrow_backend.settings import load_keypair
from escrow_backend.solana.gateway import ACCOUNT_ALREADY_IN_USE, ChainError, RpcGateway
from escrow_backend.solana.program import EscrowProgram, HoldStatus

pytestmark = pytest.mark.integration

BOOTSTRAP = os.environ.get("ESCROW_IT_BOOTSTRAP")
RPC_URL = os.environ.get("ESCROW_IT_RPC_URL", "http://127.0.0.1:8899")
KEYS = Path("~/.config/solana/card-escrow").expanduser()
USD = 1_000_000


@pytest.fixture
async def gw() -> AsyncIterator[tuple[RpcGateway, Pubkey]]:
    if not BOOTSTRAP:
        pytest.skip("ESCROW_IT_BOOTSTRAP not set")
    info = json.loads(await asyncio.to_thread(Path(BOOTSTRAP).read_text))
    program = EscrowProgram(
        Pubkey.from_string("8PyM1gDSssAqmn1qNPcwQ2y6nxFjUGwhPp81obhmAwpK"),
        Pubkey.from_string(info["ESCROW_MINT"]),
    )
    g = RpcGateway(
        RPC_URL,
        program,
        load_keypair(KEYS / "operator.json"),
        load_keypair(KEYS / "settlement-authority.json"),
    )
    yield g, Pubkey.from_string(info["CARD:card_alice"])
    await g.close()


def uid() -> str:
    return uuid.uuid4().hex


async def test_snapshot_reads_confirmed_state(gw: tuple[RpcGateway, Pubkey]) -> None:
    g, alice = gw
    snap = await g.snapshot(alice)
    assert snap.vault is not None and snap.vault.owner == alice
    assert snap.balance >= snap.vault.held_total
    assert snap.slot > 0 and snap.chain_ts > 1_700_000_000
    assert not snap.paused
    cfg = await g.config()
    assert cfg is not None and cfg.operator == g.operator.pubkey()


async def test_authorize_capture_flow(gw: tuple[RpcGateway, Pubkey]) -> None:
    g, alice = gw
    a = uid()
    before = await g.snapshot(alice)
    tx = await g.authorize(alice, auth_id_bytes(a), 3 * USD)
    assert tx.slot and tx.signature
    after = await g.snapshot(alice)
    assert after.vault.held_total == before.vault.held_total + 3 * USD  # type: ignore[union-attr]

    # A byte-identical resend (cached blockhash) confirms the original transaction.
    again = await g.authorize(alice, auth_id_bytes(a), 3 * USD)
    assert again.signature == tx.signature

    # A different transaction for the same auth_id hits the Hold PDA `init`.
    with pytest.raises(ChainError) as dup:
        await g.authorize(alice, auth_id_bytes(a), 3 * USD + 1)
    assert dup.value.name == ACCOUNT_ALREADY_IN_USE

    with pytest.raises(ChainError) as over:
        await g.capture(alice, auth_id_bytes(a), 4 * USD)
    assert over.value.name == "CaptureExceedsHold"

    await g.capture(alice, auth_id_bytes(a), 2 * USD)
    key = g.program.pdas.hold(g.program.pdas.vault(alice), auth_id_bytes(a))
    hold = (await g.get_holds([key]))[key]
    assert hold is not None
    assert (hold.status, hold.captured_amount) == (HoldStatus.CAPTURED, 2 * USD)

    with pytest.raises(ChainError) as again:
        await g.release(alice, auth_id_bytes(a))
    assert again.value.name == "HoldNotPending"


async def test_release_and_refund(gw: tuple[RpcGateway, Pubkey]) -> None:
    g, alice = gw
    a = uid()
    await g.authorize(alice, auth_id_bytes(a), USD)
    await g.release(alice, auth_id_bytes(a))
    before = (await g.snapshot(alice)).balance
    r = uid()
    await g.refund(alice, refund_id_bytes(r), USD // 2)
    assert (await g.snapshot(alice)).balance == before + USD // 2
    with pytest.raises(ChainError) as dup:
        await g.refund(alice, refund_id_bytes(r), USD // 2 + 1)
    assert dup.value.name == ACCOUNT_ALREADY_IN_USE
    key = g.program.pdas.refund(g.program.pdas.vault(alice), refund_id_bytes(r))
    assert (await g.accounts_exist([key]))[key]


async def test_insufficient_funds_maps_to_program_error(gw: tuple[RpcGateway, Pubkey]) -> None:
    g, alice = gw
    with pytest.raises(ChainError) as e:
        await g.authorize(alice, auth_id_bytes(uid()), 10**15)
    assert e.value.name == "InsufficientAvailableBalance"
    assert not e.value.transient


async def test_expire_before_ttl_rejected(gw: tuple[RpcGateway, Pubkey]) -> None:
    g, alice = gw
    a = uid()
    await g.authorize(alice, auth_id_bytes(a), USD)
    with pytest.raises(ChainError) as e:
        await g.expire(alice, auth_id_bytes(a))
    assert e.value.name == "HoldNotYetExpired"
    await g.release(alice, auth_id_bytes(a))


async def test_unreachable_rpc_is_transient(gw: tuple[RpcGateway, Pubkey]) -> None:
    g, alice = gw
    bad = RpcGateway("http://127.0.0.1:1", g.program, g.operator, None)
    with pytest.raises(ChainError) as e:
        await bad.snapshot(alice)
    assert e.value.transient
    with pytest.raises(ChainError) as e2:
        await bad.release(alice, auth_id_bytes(uid()))
    assert e2.value.transient
    await bad.close()
