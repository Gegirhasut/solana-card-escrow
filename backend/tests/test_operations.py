"""Clearing / reversal / refund: idempotency, error mapping and retries."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from solders.pubkey import Pubkey
from sqlalchemy import select

from escrow_backend.ids import auth_id_bytes
from escrow_backend.models import Authorization, IssuerOperation, OpKind
from escrow_backend.services.operations import OpRequest, OpResult
from escrow_backend.solana.gateway import ChainError, TxResult
from escrow_backend.solana.program import HoldStatus

from .conftest import CARD, OWNER, USD, Ctx


def clearing(cid: str, auth_id: str, amount: int) -> OpRequest:
    return OpRequest(OpKind.CLEARING, cid, auth_id, None, amount, {"clearing_id": cid})


def reversal(rid: str, auth_id: str) -> OpRequest:
    return OpRequest(OpKind.REVERSAL, rid, auth_id, None, None, {"reversal_id": rid})


def refund(rid: str, amount: int, card: str = CARD) -> OpRequest:
    return OpRequest(OpKind.REFUND, rid, None, card, amount, {"refund_id": rid})


def body(r: OpResult) -> dict[str, Any]:
    return json.loads(r.body)


async def auth_row(ctx: Ctx, auth_id: str) -> Authorization:
    async with ctx.sm() as s:
        r = await s.scalar(select(Authorization).where(Authorization.auth_id == auth_id))
    assert r is not None
    return r


def hold_status(ctx: Ctx, auth_id: str) -> HoldStatus:
    h = ctx.chain.hold_of(OWNER, auth_id_bytes(auth_id))
    assert h is not None
    return h.status


# -------------------------------------------------------------------- clearing


async def test_full_and_partial_clearing(ctx: Ctx) -> None:
    await ctx.authorize("a1", 100 * USD)
    await ctx.authorize("a2", 100 * USD)
    r1 = await ctx.ops.handle(clearing("c1", "a1", 100 * USD))
    r2 = await ctx.ops.handle(clearing("c2", "a2", 60 * USD))
    assert (r1.status_code, body(r1)["status"]) == (200, "succeeded")
    assert body(r2)["captured_amount"] == 60 * USD
    assert ctx.chain.settlement_balance == 1_000_000 * USD + 160 * USD
    assert ctx.chain.vaults[OWNER].state.held_total == 0
    a2 = await auth_row(ctx, "a2")
    assert (a2.state, a2.captured_amount) == ("captured", 60 * USD)


async def test_duplicate_clearing_replays_stored_response(ctx: Ctx) -> None:
    await ctx.authorize("a1", 100 * USD)
    first = await ctx.ops.handle(clearing("c1", "a1", 40 * USD))
    again = await ctx.ops.handle(clearing("c1", "a1", 40 * USD))
    assert first.body == again.body
    captures = [x for x in ctx.chain.sent if x[0] == "capture"]
    assert len(captures) == 1


async def test_concurrent_duplicate_clearings_capture_once(ctx: Ctx) -> None:
    await ctx.authorize("a1", 100 * USD)
    ctx.chain.latency_s = 0.05
    results = await asyncio.gather(
        *[ctx.ops.handle(clearing("c1", "a1", 50 * USD)) for _ in range(6)]
    )
    assert len([x for x in ctx.chain.sent if x[0] == "capture"]) == 1
    finals = {r.body for r in results if r.status_code == 200}
    assert len(finals) == 1
    assert all(body(r)["status"] in ("succeeded", "processing") for r in results)
    # After the dust settles every replay is the final answer.
    assert (await ctx.ops.handle(clearing("c1", "a1", 50 * USD))).body in finals


async def test_clearing_rejections(ctx: Ctx) -> None:
    await ctx.authorize("ok", 10 * USD)
    await ctx.authorize("declined", 5_000 * USD)
    cases = [
        (clearing("x1", "nope", USD), "unknown_authorization"),
        (clearing("x2", "declined", USD), "authorization_not_approved"),
        (clearing("x3", "ok", 11 * USD), "capture_exceeds_authorization"),
    ]
    for req, error in cases:
        r = await ctx.ops.handle(req)
        assert (body(r)["status"], body(r)["error"]) == ("failed", error)


async def test_second_clearing_on_same_auth_fails(ctx: Ctx) -> None:
    await ctx.authorize("a1", 10 * USD)
    await ctx.ops.handle(clearing("c1", "a1", 5 * USD))
    r = await ctx.ops.handle(clearing("c2", "a1", 5 * USD))
    assert body(r)["error"] == "hold_captured"


async def test_clearing_after_expiry_fails(ctx: Ctx) -> None:
    await ctx.authorize("a1", 10 * USD)
    ctx.clock.now += ctx.chain.ttl
    r = await ctx.ops.handle(clearing("c1", "a1", 5 * USD))
    assert body(r)["error"] == "hold_expired"


async def test_clearing_retry_after_landed_capture_is_idempotent(ctx: Ctx) -> None:
    """The capture landed but we never learned it (e.g. confirmation timeout)."""
    await ctx.authorize("a1", 10 * USD)
    real = ctx.chain.capture
    calls = 0

    async def capture(owner: Pubkey, auth_id: bytes, amount: int) -> TxResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            await real(owner, auth_id, amount)
            raise ChainError("confirmation timeout", transient=True)
        return await real(owner, auth_id, amount)

    ctx.chain.capture = capture  # type: ignore[method-assign]
    first = await ctx.ops.handle(clearing("c1", "a1", 7 * USD))
    assert (first.status_code, body(first)["status"]) == (202, "processing")
    assert await ctx.worker.retry_operations() == 1
    final = await ctx.ops.handle(clearing("c1", "a1", 7 * USD))
    assert (body(final)["status"], body(final)["captured_amount"]) == ("succeeded", 7 * USD)
    assert ctx.chain.settlement_balance == 1_000_000 * USD + 7 * USD
    assert (await auth_row(ctx, "a1")).state == "captured"


async def test_permanent_chain_error_fails_operation(ctx: Ctx) -> None:
    await ctx.authorize("a1", 10 * USD)

    async def capture(*a: Any) -> TxResult:
        raise ChainError("paused", name="Paused")

    ctx.chain.capture = capture  # type: ignore[method-assign]
    r = await ctx.ops.handle(clearing("c1", "a1", USD))
    assert (body(r)["status"], body(r)["error"]) == ("failed", "Paused")


async def test_retry_gives_up_after_max_attempts(ctx: Ctx) -> None:
    from escrow_backend.services import operations

    await ctx.authorize("a1", 10 * USD)

    async def capture(*a: Any) -> TxResult:
        raise ChainError("rpc down", transient=True)

    ctx.chain.capture = capture  # type: ignore[method-assign]
    assert (await ctx.ops.handle(clearing("c1", "a1", USD))).status_code == 202
    for _ in range(operations.MAX_ATTEMPTS):
        await ctx.worker.retry_operations()
    r = await ctx.ops.handle(clearing("c1", "a1", USD))
    assert (body(r)["status"], body(r)["error"]) == ("failed", "rpc down")


# -------------------------------------------------------------------- reversal


async def test_reversal_releases_and_is_idempotent(ctx: Ctx) -> None:
    await ctx.authorize("a1", 30 * USD)
    r = await ctx.ops.handle(reversal("r1", "a1"))
    assert body(r)["status"] == "succeeded"
    assert hold_status(ctx, "a1") == HoldStatus.RELEASED
    assert (await ctx.ops.handle(reversal("r1", "a1"))).body == r.body
    # A second, different reversal id for an already released hold also succeeds.
    r2 = await ctx.ops.handle(reversal("r2", "a1"))
    assert body(r2)["status"] == "succeeded" and body(r2)["signature"] is None
    assert (await auth_row(ctx, "a1")).state == "released"


async def test_reversal_after_expiry_and_capture(ctx: Ctx) -> None:
    await ctx.authorize("exp", 10 * USD)
    await ctx.authorize("cap", 10 * USD)
    await ctx.ops.handle(clearing("c1", "cap", 10 * USD))
    ctx.clock.now += ctx.chain.ttl
    await ctx.chain.expire(OWNER, auth_id_bytes("exp"))
    r = await ctx.ops.handle(reversal("r1", "exp"))
    assert body(r)["status"] == "succeeded"
    assert (await auth_row(ctx, "exp")).state == "expired"
    r = await ctx.ops.handle(reversal("r2", "cap"))
    assert (body(r)["status"], body(r)["error"]) == ("failed", "hold_already_captured")


# ---------------------------------------------------------------------- refund


async def test_refund_and_duplicate(ctx: Ctx) -> None:
    before = ctx.chain.vaults[OWNER].balance
    r = await ctx.ops.handle(refund("rf1", 25 * USD))
    assert body(r)["status"] == "succeeded"
    assert ctx.chain.vaults[OWNER].balance == before + 25 * USD
    assert (await ctx.ops.handle(refund("rf1", 25 * USD))).body == r.body
    assert ctx.chain.vaults[OWNER].balance == before + 25 * USD


async def test_refund_already_on_chain_is_success(ctx: Ctx) -> None:
    from escrow_backend.ids import refund_id_bytes

    await ctx.chain.refund(OWNER, refund_id_bytes("rf1"), 5 * USD)  # landed earlier
    r = await ctx.ops.handle(refund("rf1", 5 * USD))
    assert (body(r)["status"], body(r)["already_refunded"]) == ("succeeded", True)
    assert ctx.chain.vaults[OWNER].balance == 1_005 * USD


async def test_refund_validation(ctx: Ctx) -> None:
    r = await ctx.ops.handle(refund("rf1", 5 * USD, card="ghost"))
    assert body(r)["error"] == "card_not_found"
    r = await ctx.ops.handle(refund("rf2", 0))
    assert body(r)["error"] == "invalid_amount"
    r = await ctx.ops.handle(clearing("c0", "whatever", 0))
    assert body(r)["error"] in ("unknown_authorization", "invalid_amount")


async def test_stale_lease_is_reclaimed(ctx: Ctx) -> None:
    from datetime import timedelta

    from sqlalchemy import func, update

    from escrow_backend.db import transaction

    await ctx.authorize("a1", 10 * USD)
    # Simulate a worker that crashed mid-processing.
    async with transaction(ctx.sm) as s:
        s.add(
            IssuerOperation(
                kind=OpKind.CLEARING,
                external_id="c1",
                auth_id="a1",
                amount=4 * USD,
                request={},
                status="processing",
                lease_until=func.now() + timedelta(seconds=30),
            )
        )
    r = await ctx.ops.handle(clearing("c1", "a1", 4 * USD))
    assert (r.status_code, body(r)["status"]) == (202, "processing")
    async with transaction(ctx.sm) as s:
        await s.execute(
            update(IssuerOperation).values(lease_until=func.now() - timedelta(seconds=1))
        )
    assert await ctx.worker.retry_operations() == 1
    r = await ctx.ops.handle(clearing("c1", "a1", 4 * USD))
    assert body(r)["status"] == "succeeded"
