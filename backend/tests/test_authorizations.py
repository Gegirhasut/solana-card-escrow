"""Authorization webhook semantics: decisions, idempotency, concurrency, budget."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

from solders.pubkey import Pubkey
from sqlalchemy import func, select, update

from escrow_backend.db import transaction
from escrow_backend.ids import auth_id_bytes
from escrow_backend.models import Authorization, Card, Compensation
from escrow_backend.services.authorizations import AuthRequest
from escrow_backend.solana.gateway import ChainError, TxResult
from escrow_backend.solana.program import HoldStatus

from .conftest import CARD, OWNER, USD, Ctx


async def row(ctx: Ctx, auth_id: str) -> Authorization:
    async with ctx.sm() as s:
        r = await s.scalar(select(Authorization).where(Authorization.auth_id == auth_id))
    assert r is not None
    return r


def authorize_calls(ctx: Ctx) -> int:
    return sum(1 for kind, *_ in ctx.chain.sent if kind == "authorize")


# ------------------------------------------------------------------ decisions


async def test_approve_places_confirmed_hold(ctx: Ctx) -> None:
    resp = await ctx.authorize("a1", 120 * USD)
    assert resp["decision"] == "approved"
    assert resp["reason"] is None
    hold = ctx.chain.hold_of(OWNER, auth_id_bytes("a1"))
    assert hold is not None and hold.amount == 120 * USD and hold.status == HoldStatus.PENDING
    assert resp["hold_address"] == str(ctx.chain._hold_key(OWNER, auth_id_bytes("a1")))
    r = await row(ctx, "a1")
    assert (r.decision, r.state, r.owner_pubkey) == ("approved", "approved", str(OWNER))
    assert r.snapshot is not None and r.snapshot_slot == resp["snapshot_slot"]
    assert r.snapshot["balance"] == 1_000 * USD
    assert r.authorize_sig == resp["authorize_signature"]
    assert r.compensation is None


async def test_declines_with_reasons(ctx: Ctx) -> None:
    async with transaction(ctx.sm) as s:
        s.add(Card(card_id="frozen", owner_pubkey=str(OWNER), status="frozen"))
        s.add(Card(card_id="novault", owner_pubkey=str(Pubkey.new_unique()), status="active"))
    cases = [
        ("d1", 2_000 * USD, CARD, "USD", "insufficient_funds"),
        ("d2", 501 * USD, CARD, "USD", "daily_limit_exceeded"),
        ("d3", USD, "unknown", "USD", "card_not_found"),
        ("d4", USD, "frozen", "USD", "card_inactive"),
        ("d5", USD, "novault", "USD", "vault_not_found"),
        ("d6", 0, CARD, "USD", "invalid_amount"),
        ("d7", USD, CARD, "EUR", "currency_mismatch"),
    ]
    for auth_id, amount, card, cur, reason in cases:
        body = json.loads(await ctx.auths.handle(AuthRequest(auth_id, card, amount, cur)))
        assert (body["decision"], body["reason"]) == ("declined", reason), auth_id
        r = await row(ctx, auth_id)
        assert r.decline_reason == reason
        # Pre-checks happen before any transaction is attempted.
        assert r.authorize_attempted_at is None and r.compensation is None
    assert authorize_calls(ctx) == 0


async def test_paused_declines(ctx: Ctx) -> None:
    ctx.chain.paused = True
    assert (await ctx.authorize("p1", USD))["reason"] == "paused"


async def test_velocity_limit(ctx: Ctx) -> None:
    for i in range(10):
        assert (await ctx.authorize(f"v{i}", USD))["decision"] == "approved"
    assert (await ctx.authorize("v10", USD))["reason"] == "velocity_limit_exceeded"


async def test_chain_rejection_maps_to_reason_and_flags_compensation(ctx: Ctx) -> None:
    async def authorize(*a: Any) -> TxResult:
        raise ChainError("preflight failed", name="InsufficientAvailableBalance")

    ctx.chain.authorize = authorize  # type: ignore[method-assign]
    resp = await ctx.authorize("r1", USD)
    assert (resp["decision"], resp["reason"]) == ("declined", "insufficient_funds")
    r = await row(ctx, "r1")
    assert r.authorize_attempted_at is not None
    assert r.compensation == Compensation.PENDING


async def test_unknown_chain_error_is_chain_rejected(ctx: Ctx) -> None:
    async def authorize(*a: Any) -> TxResult:
        raise ChainError("boom", name="SomethingElse")

    ctx.chain.authorize = authorize  # type: ignore[method-assign]
    assert (await ctx.authorize("r2", USD))["reason"] == "chain_rejected"


async def test_transient_chain_errors_decline_unavailable(ctx: Ctx) -> None:
    ctx.chain.fail_next.append(ChainError("rpc down", transient=True))
    assert (await ctx.authorize("t1", USD))["reason"] == "chain_unavailable"

    async def authorize(*a: Any) -> TxResult:
        raise ChainError("confirmation timeout", transient=True)

    ctx.chain.authorize = authorize  # type: ignore[method-assign]
    assert (await ctx.authorize("t2", USD))["reason"] == "chain_unavailable"
    assert (await row(ctx, "t2")).compensation == Compensation.PENDING


# ---------------------------------------------------------------- idempotency


async def test_sequential_duplicate_returns_identical_bytes(ctx: Ctx) -> None:
    req = AuthRequest("dup1", CARD, 50 * USD, "USD")
    first = await ctx.auths.handle(req)
    # Change the world: a recomputation would now decline.
    ctx.chain.paused = True
    again = await ctx.auths.handle(req)
    different_payload = await ctx.auths.handle(AuthRequest("dup1", CARD, 999 * USD, "USD"))
    assert first == again == different_payload
    assert json.loads(first)["decision"] == "approved"
    assert authorize_calls(ctx) == 1


async def test_sequential_duplicate_of_decline_stays_declined(ctx: Ctx) -> None:
    req = AuthRequest("dup2", CARD, 5_000 * USD, "USD")
    first = await ctx.auths.handle(req)
    ctx.chain.vaults[OWNER].balance = 10_000 * USD  # would now be approvable
    assert await ctx.auths.handle(req) == first
    assert json.loads(first)["decision"] == "declined"


async def test_concurrent_duplicates_get_one_answer(ctx: Ctx) -> None:
    ctx.chain.latency_s = 0.05
    req = AuthRequest("conc1", CARD, 10 * USD, "USD")
    bodies = await asyncio.gather(*[ctx.auths.handle(req) for _ in range(12)])
    assert len(set(bodies)) == 1
    assert json.loads(bodies[0])["decision"] == "approved"
    assert authorize_calls(ctx) == 1
    assert ctx.chain.vaults[OWNER].state.held_total == 10 * USD


async def test_concurrent_distinct_auths_respect_balance(ctx: Ctx) -> None:
    ctx.chain.latency_s = 0.01
    ctx.chain.vaults[OWNER].state = replace(ctx.chain.vaults[OWNER].state, daily_limit=10**15)
    reqs = [AuthRequest(f"bal{i}", CARD, 300 * USD, "USD") for i in range(6)]
    bodies = [json.loads(b) for b in await asyncio.gather(*[ctx.auths.handle(r) for r in reqs])]
    approved = [b for b in bodies if b["decision"] == "approved"]
    # 1000 USD balance -> at most three 300 USD holds, regardless of interleaving.
    assert len(approved) == 3
    assert ctx.chain.vaults[OWNER].state.held_total == 900 * USD


# ------------------------------------------------------------- timeout budget


async def test_slow_chain_declines_with_timeout(ctx: Ctx) -> None:
    svc = ctx.auth_service(budget_ms=100)
    ctx.chain.latency_s = 0.3
    body = json.loads(await svc.handle(AuthRequest("slow1", CARD, USD, "USD")))
    assert (body["decision"], body["reason"]) == ("declined", "timeout")
    ctx.chain.latency_s = 0
    # A later duplicate gets the same stored timeout decline.
    assert json.loads(await ctx.auths.handle(AuthRequest("slow1", CARD, USD, "USD"))) == body


class LateLandingChain:
    """Authorize lands on-chain but confirmation arrives after the budget."""

    def __init__(self, ctx: Ctx, delay: float) -> None:
        self.inner = ctx.chain
        self.program = ctx.chain.program
        self.delay = delay

    async def snapshot(self, owner: Pubkey) -> Any:
        return await self.inner.snapshot(owner)

    async def authorize(self, owner: Pubkey, auth_id: bytes, amount: int) -> TxResult:
        # Shielded: once broadcast, a transaction lands regardless of our timeout.
        tx = await asyncio.shield(self.inner.authorize(owner, auth_id, amount))
        await asyncio.sleep(self.delay)
        return tx


async def test_late_landing_hold_is_declined_and_compensated(ctx: Ctx) -> None:
    svc = ctx.auth_service(budget_ms=2000, chain=LateLandingChain(ctx, delay=4))
    body = json.loads(await svc.handle(AuthRequest("late1", CARD, 70 * USD, "USD")))
    assert (body["decision"], body["reason"]) == ("declined", "timeout")
    # The hold exists on-chain although the issuer was told "declined".
    assert ctx.chain.hold_of(OWNER, auth_id_bytes("late1")).status == HoldStatus.PENDING
    r = await row(ctx, "late1")
    assert r.compensation == Compensation.PENDING

    assert await ctx.worker.compensate() == 1
    assert ctx.chain.hold_of(OWNER, auth_id_bytes("late1")).status == HoldStatus.RELEASED
    assert ctx.chain.vaults[OWNER].state.held_total == 0
    assert (await row(ctx, "late1")).compensation == Compensation.RELEASED


async def test_approval_after_deadline_loses_cas(ctx: Ctx) -> None:
    """Even without asyncio cancellation, the DB refuses approvals past the deadline."""
    svc = ctx.auth_service(budget_ms=60_000)
    real = ctx.chain.authorize

    async def authorize(owner: Pubkey, auth_id: bytes, amount: int) -> TxResult:
        tx = await real(owner, auth_id, amount)
        async with transaction(ctx.sm) as s:  # simulate the deadline passing meanwhile
            await s.execute(
                update(Authorization)
                .where(Authorization.auth_id == "cas1")
                .values(deadline_at=func.now() - timedelta(seconds=1))
            )
        # ...and a duplicate settling it as a timeout decline.
        await svc._finalize_timeout(AuthRequest("cas1", CARD, USD, "USD"))
        return tx

    ctx.chain.authorize = authorize  # type: ignore[method-assign]
    body = json.loads(await svc.handle(AuthRequest("cas1", CARD, USD, "USD")))
    assert (body["decision"], body["reason"]) == ("declined", "timeout")
    assert (await row(ctx, "cas1")).compensation == Compensation.PENDING


async def test_concurrent_duplicates_with_slow_first_all_decline(ctx: Ctx) -> None:
    svc = ctx.auth_service(budget_ms=200)
    ctx.chain.latency_s = 0.15  # snapshot + authorize ~0.3s > budget
    req = AuthRequest("slowdup", CARD, USD, "USD")
    bodies = await asyncio.gather(*[svc.handle(req) for _ in range(5)])
    assert len(set(bodies)) == 1
    assert json.loads(bodies[0])["reason"] == "timeout"


async def test_crashed_first_request_is_settled_by_duplicate(ctx: Ctx) -> None:
    # A row claimed by a request that died before deciding.
    async with transaction(ctx.sm) as s:
        s.add(
            Authorization(
                auth_id="crash1",
                card_id=CARD,
                amount=USD,
                currency="USD",
                deadline_at=func.now() - timedelta(seconds=5),
                state="pending",
                authorize_attempted_at=func.now(),
            )
        )
    body = json.loads(await ctx.auths.handle(AuthRequest("crash1", CARD, USD, "USD")))
    assert (body["decision"], body["reason"]) == ("declined", "timeout")
    assert (await row(ctx, "crash1")).compensation == Compensation.PENDING


async def test_timeout_before_claim_still_persists_decline(ctx: Ctx) -> None:
    svc = ctx.auth_service(budget_ms=1500)
    stored = await svc._finalize_timeout(AuthRequest("never", CARD, USD, "USD"))
    assert json.loads(stored)["reason"] == "timeout"
    # A retry cannot turn it into an approval.
    assert await ctx.auths.handle(AuthRequest("never", CARD, USD, "USD")) == stored
    assert authorize_calls(ctx) == 0


async def test_works_without_redis(ctx: Ctx) -> None:
    from escrow_backend.services.authorizations import AuthorizationService
    from escrow_backend.services.notify import Notifier

    svc = AuthorizationService(ctx.sm, ctx.chain, Notifier(None), 1500)
    ctx.chain.latency_s = 0.03
    req = AuthRequest("noredis", CARD, USD, "USD")
    bodies = await asyncio.gather(*[svc.handle(req) for _ in range(4)])
    assert len(set(bodies)) == 1


async def test_broken_redis_falls_back_to_polling(ctx: Ctx) -> None:
    from redis.asyncio import Redis

    from escrow_backend.services.authorizations import AuthorizationService
    from escrow_backend.services.notify import Notifier

    broken = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.05)
    svc = AuthorizationService(ctx.sm, ctx.chain, Notifier(broken), 1500)
    ctx.chain.latency_s = 0.03
    req = AuthRequest("badredis", CARD, USD, "USD")
    bodies = await asyncio.gather(*[svc.handle(req) for _ in range(3)])
    assert len(set(bodies)) == 1
    assert json.loads(bodies[0])["decision"] == "approved"
    await broken.aclose()


async def test_hung_redis_does_not_hold_up_the_answer(ctx: Ctx) -> None:
    """A Redis that accepts connections but never answers (network partition)."""
    from escrow_backend.services.authorizations import AuthorizationService
    from escrow_backend.services.notify import Notifier

    class HungPubSub:
        async def subscribe(self, *_: object) -> None:
            await asyncio.sleep(3600)

        async def get_message(self, **_: object) -> None:
            await asyncio.sleep(3600)

        async def unsubscribe(self) -> None:
            await asyncio.sleep(3600)

        async def aclose(self) -> None:
            await asyncio.sleep(3600)

    class HungRedis:
        async def publish(self, *_: object) -> None:
            await asyncio.sleep(3600)

        def pubsub(self) -> HungPubSub:
            return HungPubSub()

    svc = AuthorizationService(ctx.sm, ctx.chain, Notifier(HungRedis()), 3000)  # type: ignore[arg-type]
    req = AuthRequest("hung", CARD, USD, "USD")
    bodies = await asyncio.wait_for(asyncio.gather(*[svc.handle(req) for _ in range(3)]), 10)
    assert len(set(bodies)) == 1
    assert json.loads(bodies[0])["decision"] == "approved"
