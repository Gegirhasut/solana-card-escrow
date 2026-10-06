"""Background jobs: expiry crank, compensation and state sync."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update

from escrow_backend.db import transaction
from escrow_backend.ids import auth_id_bytes
from escrow_backend.models import Authorization, Compensation
from escrow_backend.solana.program import HoldStatus

from .conftest import OWNER, USD, Ctx


async def state(ctx: Ctx, auth_id: str) -> Authorization:
    async with ctx.sm() as s:
        r = await s.scalar(select(Authorization).where(Authorization.auth_id == auth_id))
    assert r is not None
    return r


async def test_expire_holds_after_ttl(ctx: Ctx) -> None:
    await ctx.authorize("a1", 10 * USD)
    await ctx.authorize("a2", 10 * USD)
    assert await ctx.worker.expire_holds() == 0  # not yet expired
    ctx.clock.now += ctx.chain.ttl
    assert await ctx.worker.expire_holds() == 2
    assert ctx.chain.hold_of(OWNER, auth_id_bytes("a1")).status == HoldStatus.EXPIRED
    assert (await state(ctx, "a1")).state == "expired"
    assert ctx.chain.vaults[OWNER].state.held_total == 0
    assert await ctx.worker.expire_holds() == 0


async def test_state_sync_when_finalized_elsewhere(ctx: Ctx) -> None:
    await ctx.authorize("a1", 10 * USD)
    await ctx.chain.capture(OWNER, auth_id_bytes("a1"), 4 * USD)  # e.g. an ops script
    assert await ctx.worker.expire_holds() == 1
    r = await state(ctx, "a1")
    assert (r.state, r.captured_amount) == ("captured", 4 * USD)


async def test_compensation_not_needed_after_landing_window(ctx: Ctx) -> None:
    async with transaction(ctx.sm) as s:
        s.add(
            Authorization(
                auth_id="ghost",
                card_id="card_0001",
                amount=USD,
                currency="USD",
                deadline_at=func.now(),
                decision="declined",
                decline_reason="timeout",
                state="declined",
                owner_pubkey=str(OWNER),
                authorize_attempted_at=func.now(),
                compensation=Compensation.PENDING,
            )
        )
    # Within the landing window we keep waiting: the tx might still land.
    assert await ctx.worker.compensate() == 0
    assert (await state(ctx, "ghost")).compensation == Compensation.PENDING
    async with transaction(ctx.sm) as s:
        await s.execute(
            update(Authorization).values(authorize_attempted_at=func.now() - timedelta(minutes=5))
        )
    assert await ctx.worker.compensate() == 1
    assert (await state(ctx, "ghost")).compensation == Compensation.NOT_NEEDED


async def test_compensation_without_owner_and_already_final(ctx: Ctx) -> None:
    async with transaction(ctx.sm) as s:
        s.add(
            Authorization(
                auth_id="noowner",
                card_id="x",
                amount=USD,
                currency="USD",
                deadline_at=func.now(),
                decision="declined",
                state="declined",
                authorize_attempted_at=func.now(),
                compensation=Compensation.PENDING,
            )
        )
    # A hold that landed and was already released by someone else.
    await ctx.chain.authorize(OWNER, auth_id_bytes("released"), USD)
    await ctx.chain.release(OWNER, auth_id_bytes("released"))
    async with transaction(ctx.sm) as s:
        s.add(
            Authorization(
                auth_id="released",
                card_id="card_0001",
                amount=USD,
                currency="USD",
                deadline_at=func.now(),
                decision="declined",
                state="declined",
                owner_pubkey=str(OWNER),
                authorize_attempted_at=func.now(),
                compensation=Compensation.PENDING,
            )
        )
    await ctx.worker.compensate()
    assert (await state(ctx, "noowner")).compensation == Compensation.NOT_NEEDED
    assert (await state(ctx, "released")).compensation == Compensation.RELEASED


async def test_run_once_reports_stats(ctx: Ctx) -> None:
    stats = await ctx.worker.run_once()
    assert stats == {"operations": 0, "compensated": 0, "expired": 0}
