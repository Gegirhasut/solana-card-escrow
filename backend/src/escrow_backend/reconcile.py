"""Three-way reconciliation: on-chain holds/refunds, backend DB, issuer ledger.

The issuer ledger is a JSON-lines file written by the mock issuer (in a real
integration this would be the issuer's settlement/clearing report). Each line:

  {"type": "authorization", "auth_id": ..., "card_id": ..., "amount": ..., "decision": ...}
  {"type": "clearing", "auth_id": ..., "clearing_id": ..., "amount": ..., "status": ...}
  {"type": "reversal", "auth_id": ..., "reversal_id": ..., "status": ...}
  {"type": "refund", "refund_id": ..., "card_id": ..., "amount": ..., "status": ...}
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from solders.pubkey import Pubkey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from escrow_backend.ids import auth_id_bytes, refund_id_bytes
from escrow_backend.models import (
    Authorization,
    AuthState,
    Card,
    Compensation,
    IssuerOperation,
    OpKind,
    OpStatus,
)
from escrow_backend.solana.gateway import ChainGateway
from escrow_backend.solana.program import Hold, HoldStatus

_CHAIN_TO_STATE = {
    HoldStatus.PENDING: AuthState.APPROVED,
    HoldStatus.CAPTURED: AuthState.CAPTURED,
    HoldStatus.RELEASED: AuthState.RELEASED,
    HoldStatus.EXPIRED: AuthState.EXPIRED,
}


@dataclass(frozen=True)
class Mismatch:
    key: str
    kind: str
    detail: str


@dataclass
class IssuerView:
    decision: str | None = None
    amount: int | None = None
    cleared: int = 0
    reversed: bool = False


@dataclass
class Report:
    checked_authorizations: int = 0
    checked_refunds: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def add(self, key: str, kind: str, detail: str) -> None:
        self.mismatches.append(Mismatch(key, kind, detail))

    def to_json(self) -> dict[str, Any]:
        return {
            "checked_authorizations": self.checked_authorizations,
            "checked_refunds": self.checked_refunds,
            "mismatch_count": len(self.mismatches),
            "mismatches": [asdict(m) for m in self.mismatches],
        }

    def render(self) -> str:
        lines = [
            f"authorizations checked: {self.checked_authorizations}",
            f"refunds checked:        {self.checked_refunds}",
            f"mismatches:             {len(self.mismatches)}",
        ]
        if self.mismatches:
            w = max(len(m.key) for m in self.mismatches)
            k = max(len(m.kind) for m in self.mismatches)
            lines.append("")
            lines.extend(f"  {m.key:<{w}}  {m.kind:<{k}}  {m.detail}" for m in self.mismatches)
        return "\n".join(lines)


def load_issuer_ledger(
    path: Path | None,
) -> tuple[dict[str, IssuerView], dict[str, dict[str, Any]]]:
    auths: dict[str, IssuerView] = defaultdict(IssuerView)
    refunds: dict[str, dict[str, Any]] = {}
    if path is None or not path.exists():
        return auths, refunds
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        t = e["type"]
        if t == "authorization":
            v = auths[e["auth_id"]]
            v.decision, v.amount = e["decision"], e["amount"]
        elif t == "clearing" and e.get("status") == "succeeded":
            auths[e["auth_id"]].cleared += int(e["amount"])
        elif t == "reversal" and e.get("status") == "succeeded":
            auths[e["auth_id"]].reversed = True
        elif t == "refund":
            refunds[e["refund_id"]] = e
    return auths, refunds


async def reconcile(
    sm: async_sessionmaker[AsyncSession], chain: ChainGateway, ledger_path: Path | None
) -> Report:
    report = Report()
    issuer_auths, issuer_refunds = load_issuer_ledger(ledger_path)

    async with sm() as s:
        db_auths = {a.auth_id: a for a in (await s.scalars(select(Authorization))).all()}
        refund_ops = {
            o.external_id: o
            for o in (
                await s.scalars(
                    select(IssuerOperation).where(IssuerOperation.kind == OpKind.REFUND)
                )
            ).all()
        }
        cards = {c.card_id: c.owner_pubkey for c in (await s.scalars(select(Card))).all()}

    pdas = chain.program.pdas
    hold_keys: dict[str, Pubkey] = {}
    for a in db_auths.values():
        if a.owner_pubkey:
            hold_keys[a.auth_id] = pdas.hold(
                pdas.vault(Pubkey.from_string(a.owner_pubkey)), auth_id_bytes(a.auth_id)
            )
    holds = await chain.get_holds(list(hold_keys.values()))

    for auth_id in sorted(set(db_auths) | set(issuer_auths)):
        report.checked_authorizations += 1
        db = db_auths.get(auth_id)
        iss = issuer_auths.get(auth_id)
        hold = holds.get(hold_keys[auth_id]) if auth_id in hold_keys else None
        if db is None:
            report.add(
                auth_id, "missing_in_backend", f"issuer decision={iss.decision if iss else None}"
            )
            continue
        _check_auth(report, db, hold, iss)

    refund_keys: dict[str, Pubkey] = {}
    for rid in set(refund_ops) | set(issuer_refunds):
        op = refund_ops.get(rid)
        card_id = op.card_id if op else issuer_refunds[rid].get("card_id")
        owner = cards.get(card_id or "")
        if owner:
            refund_keys[rid] = pdas.refund(
                pdas.vault(Pubkey.from_string(owner)), refund_id_bytes(rid)
            )
    exists = await chain.accounts_exist(list(refund_keys.values()))
    for rid in sorted(set(refund_ops) | set(issuer_refunds)):
        report.checked_refunds += 1
        op = refund_ops.get(rid)
        iss_r = issuer_refunds.get(rid)
        on_chain = exists.get(refund_keys[rid], False) if rid in refund_keys else False
        db_ok = op is not None and op.status == OpStatus.SUCCEEDED
        iss_ok = iss_r is not None and iss_r.get("status") == "succeeded"
        if db_ok != on_chain:
            report.add(
                rid, "refund_chain_mismatch", f"db={op.status if op else None} on_chain={on_chain}"
            )
        if iss_r is not None and iss_ok != db_ok:
            report.add(
                rid,
                "refund_issuer_mismatch",
                f"issuer={iss_r.get('status')} db={op.status if op else None}",
            )
    return report


def _check_auth(
    report: Report, db: Authorization, hold: Hold | None, iss: IssuerView | None
) -> None:
    key = db.auth_id
    if iss is not None and iss.decision is not None and iss.decision != db.decision:
        report.add(key, "decision_mismatch", f"issuer={iss.decision} backend={db.decision}")

    if db.decision == "approved":
        if hold is None:
            report.add(key, "approved_without_hold", "no hold account on-chain")
            return
        if hold.amount != db.amount:
            report.add(key, "hold_amount_mismatch", f"chain={hold.amount} backend={db.amount}")
        chain_state = _CHAIN_TO_STATE[hold.status]
        if chain_state != db.state:
            report.add(key, "state_mismatch", f"chain={chain_state} backend={db.state}")
        if hold.captured_amount != db.captured_amount:
            report.add(
                key,
                "captured_amount_mismatch",
                f"chain={hold.captured_amount} backend={db.captured_amount}",
            )
        if iss is not None:
            if iss.cleared != hold.captured_amount:
                report.add(
                    key,
                    "issuer_clearing_mismatch",
                    f"issuer_cleared={iss.cleared} chain_captured={hold.captured_amount}",
                )
            if iss.reversed and hold.status not in (HoldStatus.RELEASED, HoldStatus.EXPIRED):
                report.add(key, "issuer_reversal_not_applied", f"chain={hold.status.name}")
    else:
        if hold is not None and hold.status == HoldStatus.PENDING:
            pending = db.compensation == Compensation.PENDING
            report.add(
                key,
                "orphan_hold" if not pending else "orphan_hold_compensation_pending",
                f"declined ({db.decline_reason}) but hold is pending on-chain",
            )
        if iss is not None and iss.cleared:
            report.add(key, "cleared_declined_authorization", f"issuer cleared {iss.cleared}")
