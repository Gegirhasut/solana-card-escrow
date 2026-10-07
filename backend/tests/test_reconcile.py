"""Three-way reconciliation between chain, DB and issuer ledger."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import update

from escrow_backend.db import transaction
from escrow_backend.ids import auth_id_bytes
from escrow_backend.models import Authorization, OpKind
from escrow_backend.reconcile import reconcile
from escrow_backend.services.operations import OpRequest

from .conftest import CARD, OWNER, USD, Ctx


def write_ledger(path: Path, events: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


async def scenario(ctx: Ctx) -> list[dict[str, object]]:
    """A consistent history: approve+clear, approve+reverse, decline, refund."""
    await ctx.authorize("a1", 100 * USD)
    await ctx.ops.handle(OpRequest(OpKind.CLEARING, "c1", "a1", None, 60 * USD, {}))
    await ctx.authorize("a2", 10 * USD)
    await ctx.ops.handle(OpRequest(OpKind.REVERSAL, "r1", "a2", None, None, {}))
    await ctx.authorize("a3", 9_999 * USD)
    await ctx.ops.handle(OpRequest(OpKind.REFUND, "rf1", None, CARD, 5 * USD, {}))
    return [
        {"type": "authorization", "auth_id": "a1", "amount": 100 * USD, "decision": "approved"},
        {
            "type": "clearing",
            "auth_id": "a1",
            "clearing_id": "c1",
            "amount": 60 * USD,
            "status": "succeeded",
        },
        {"type": "authorization", "auth_id": "a2", "amount": 10 * USD, "decision": "approved"},
        {"type": "reversal", "auth_id": "a2", "reversal_id": "r1", "status": "succeeded"},
        {"type": "authorization", "auth_id": "a3", "amount": 9_999 * USD, "decision": "declined"},
        {
            "type": "refund",
            "refund_id": "rf1",
            "card_id": CARD,
            "amount": 5 * USD,
            "status": "succeeded",
        },
    ]


async def test_consistent_history_reconciles(ctx: Ctx, tmp_path: Path) -> None:
    ledger = write_ledger(tmp_path / "ledger.jsonl", await scenario(ctx))
    report = await reconcile(ctx.sm, ctx.chain, ledger)
    assert report.ok, report.render()
    assert (report.checked_authorizations, report.checked_refunds) == (3, 1)
    assert "mismatches:             0" in report.render()


async def test_redelivered_clearing_counts_once(ctx: Ctx, tmp_path: Path) -> None:
    events = await scenario(ctx)
    events.insert(2, events[1])  # the issuer logged a second delivery of c1
    report = await reconcile(ctx.sm, ctx.chain, write_ledger(tmp_path / "l.jsonl", events))
    assert report.ok, report.render()


async def test_detects_every_mismatch_kind(ctx: Ctx, tmp_path: Path) -> None:
    events = await scenario(ctx)
    events[1]["amount"] = 61 * USD  # issuer thinks it cleared more
    events.append(
        {"type": "authorization", "auth_id": "ghost", "amount": 1, "decision": "approved"}
    )
    events.append(
        {
            "type": "refund",
            "refund_id": "rf-missing",
            "card_id": CARD,
            "amount": 1,
            "status": "succeeded",
        }
    )
    events.append({"type": "authorization", "auth_id": "a4", "amount": USD, "decision": "declined"})
    ledger = write_ledger(tmp_path / "ledger.jsonl", events)

    await ctx.authorize("a4", USD)  # approved by us, issuer recorded a decline
    # Orphan: declined in DB but a pending hold exists on-chain.
    await ctx.chain.authorize(OWNER, auth_id_bytes("a3"), USD)
    # DB drift on a2.
    async with transaction(ctx.sm) as s:
        await s.execute(
            update(Authorization).where(Authorization.auth_id == "a2").values(state="approved")
        )
    # An approved auth whose hold vanished.
    await ctx.authorize("a5", USD)
    ctx.chain.holds.pop(ctx.chain._hold_key(OWNER, auth_id_bytes("a5")))

    report = await reconcile(ctx.sm, ctx.chain, ledger)
    kinds = {(m.key, m.kind) for m in report.mismatches}
    assert ("a1", "issuer_clearing_mismatch") in kinds
    assert ("ghost", "missing_in_backend") in kinds
    assert ("rf-missing", "refund_issuer_mismatch") in kinds
    assert ("a4", "decision_mismatch") in kinds
    assert ("a3", "orphan_hold") in kinds
    assert ("a2", "state_mismatch") in kinds
    assert ("a5", "approved_without_hold") in kinds
    assert not report.ok
    assert json.loads(json.dumps(report.to_json()))["mismatch_count"] == len(report.mismatches)


async def test_amount_mismatches_and_unapplied_reversal(ctx: Ctx, tmp_path: Path) -> None:
    await ctx.authorize("a1", 10 * USD)
    async with transaction(ctx.sm) as s:
        await s.execute(update(Authorization).values(amount=11 * USD, captured_amount=1))
    ledger = write_ledger(
        tmp_path / "l.jsonl",
        [
            {"type": "authorization", "auth_id": "a1", "amount": 10 * USD, "decision": "approved"},
            {"type": "reversal", "auth_id": "a1", "reversal_id": "r1", "status": "succeeded"},
        ],
    )
    kinds = {m.kind for m in (await reconcile(ctx.sm, ctx.chain, ledger)).mismatches}
    assert {
        "hold_amount_mismatch",
        "captured_amount_mismatch",
        "issuer_reversal_not_applied",
    } <= kinds


async def test_cleared_declined_and_refund_chain_mismatch(ctx: Ctx, tmp_path: Path) -> None:
    await ctx.authorize("d1", 99_999 * USD)  # declined
    await ctx.ops.handle(OpRequest(OpKind.REFUND, "rf1", None, CARD, USD, {}))
    ctx.chain.refunds.clear()  # refund PDA "disappeared"
    ledger = write_ledger(
        tmp_path / "l.jsonl",
        [
            {"type": "authorization", "auth_id": "d1", "amount": 1, "decision": "declined"},
            {
                "type": "clearing",
                "auth_id": "d1",
                "clearing_id": "c",
                "amount": 5,
                "status": "succeeded",
            },
        ],
    )
    kinds = {(m.key, m.kind) for m in (await reconcile(ctx.sm, ctx.chain, ledger)).mismatches}
    assert ("d1", "cleared_declined_authorization") in kinds
    assert ("rf1", "refund_chain_mismatch") in kinds


async def test_works_without_ledger(ctx: Ctx) -> None:
    await scenario(ctx)
    report = await reconcile(ctx.sm, ctx.chain, None)
    assert report.ok
