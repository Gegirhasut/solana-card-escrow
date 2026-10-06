"""Authorization webhook handling.

Guarantees
----------
* One answer per auth_id. The `authorizations.auth_id` unique constraint picks
  exactly one *first* request; the decision is then written with a single
  compare-and-set (`WHERE decision IS NULL`). Every response — first,
  sequential duplicate, concurrent duplicate — is the stored response text.
* Approve only with a confirmed hold. The authorize transaction must be
  confirmed on-chain before an approval is written, so an approval always
  has funds reserved by the program.
* Fail-closed within the issuer's budget. Requests that cannot finish within
  `auth_timeout_budget_ms` write (or read) a `timeout` decline. Approvals are
  additionally guarded by `clock_timestamp() <= deadline_at` in the database.
* No stuck funds. If an authorize transaction was attempted but the final
  decision is a decline (timeout, crash, race), the row is flagged for
  compensation and the worker releases any hold that landed.

The first request does *not* keep a DB transaction open across RPC calls:
holding row locks across network I/O would make duplicate requests wait on
locks they cannot time out of consistently. Instead: short transaction to
claim the auth_id, chain work, short CAS transaction to publish the decision.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from solders.pubkey import Pubkey
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from escrow_backend.db import transaction
from escrow_backend.decision import (
    CHAIN_ERROR_REASONS,
    Decision,
    DeclineReason,
    VaultSnapshot,
    evaluate,
)
from escrow_backend.ids import auth_id_bytes
from escrow_backend.models import Authorization, AuthState, Card, CardStatus, Compensation
from escrow_backend.services.notify import Notifier
from escrow_backend.solana.gateway import ChainError, ChainGateway, TxResult

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AuthRequest:
    auth_id: str
    card_id: str
    amount: int  # token base units
    currency: str
    merchant: dict[str, Any] | None = None


def render_response(
    auth_id: str,
    decision: Decision,
    *,
    hold_address: str | None = None,
    snapshot_slot: int | None = None,
    authorize_sig: str | None = None,
) -> str:
    body = {
        "auth_id": auth_id,
        "decision": "approved" if decision.approved else "declined",
        "reason": decision.reason.value if decision.reason else None,
        "hold_address": hold_address,
        "snapshot_slot": snapshot_slot,
        "authorize_signature": authorize_sig,
    }
    return json.dumps(body, separators=(",", ":"))


class AuthorizationService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        chain: ChainGateway,
        notifier: Notifier,
        budget_ms: int,
        currency: str = "USD",
    ) -> None:
        self.sm = sessionmaker
        self.chain = chain
        self.notifier = notifier
        self.budget_s = budget_ms / 1000
        self.currency = currency

    async def handle(self, req: AuthRequest) -> str:
        """Returns the response body for this auth_id (always the stored one)."""
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.budget_s):
                return await self._handle(req)
        except TimeoutError:
            log.warning("auth.timeout", auth_id=req.auth_id, elapsed_ms=_ms(started))
            # Shielded: the timeout decline must be persisted even if the
            # client disconnects, or a later duplicate could see no decision.
            return await asyncio.shield(self._finalize_timeout(req))
        finally:
            log.info("auth.handled", auth_id=req.auth_id, elapsed_ms=_ms(started))

    # ----------------------------------------------------------------- flow

    async def _handle(self, req: AuthRequest) -> str:
        if not await self._claim(req):
            return await self._await_decision(req)

        decision, snapshot, owner, tx = await self._decide(req)
        response = render_response(
            req.auth_id,
            decision,
            hold_address=str(self._hold(owner, req.auth_id)) if tx else None,
            snapshot_slot=snapshot.slot if snapshot else None,
            authorize_sig=tx.signature if tx else None,
        )
        stored = await self._publish(req.auth_id, decision, response, snapshot, owner, tx)
        await self.notifier.publish(req.auth_id)
        return stored

    async def _claim(self, req: AuthRequest) -> bool:
        """Inserts the auth row; True only for the first request for this auth_id."""
        async with transaction(self.sm) as s:
            stmt = (
                insert(Authorization)
                .values(
                    auth_id=req.auth_id,
                    card_id=req.card_id,
                    amount=req.amount,
                    currency=req.currency,
                    merchant=req.merchant,
                    deadline_at=func.now() + timedelta(seconds=self.budget_s),
                    state=AuthState.PENDING,
                )
                .on_conflict_do_nothing(index_elements=["auth_id"])
                .returning(Authorization.id)
            )
            inserted = (await s.execute(stmt)).scalar_one_or_none()
        if inserted is None:
            log.info("auth.duplicate", auth_id=req.auth_id)
        return inserted is not None

    async def _decide(
        self, req: AuthRequest
    ) -> tuple[Decision, VaultSnapshot | None, Pubkey | None, TxResult | None]:
        if req.amount <= 0:
            return Decision.decline(DeclineReason.INVALID_AMOUNT), None, None, None
        if req.currency != self.currency:
            return Decision.decline(DeclineReason.CURRENCY_MISMATCH), None, None, None

        async with self.sm() as s:
            card = await s.get(Card, req.card_id)
        if card is None:
            return Decision.decline(DeclineReason.CARD_NOT_FOUND), None, None, None
        if card.status != CardStatus.ACTIVE:
            return Decision.decline(DeclineReason.CARD_INACTIVE), None, None, None
        owner = Pubkey.from_string(card.owner_pubkey)

        try:
            snapshot = await self.chain.snapshot(owner)
        except ChainError as e:
            log.warning("auth.snapshot_failed", auth_id=req.auth_id, error=str(e))
            return Decision.decline(DeclineReason.CHAIN_UNAVAILABLE), None, owner, None
        await self._store_snapshot(req.auth_id, owner, snapshot)

        decision = evaluate(snapshot, req.amount)
        if not decision.approved:
            return decision, snapshot, owner, None

        await self._mark_attempt(req.auth_id)
        try:
            tx = await self.chain.authorize(owner, auth_id_bytes(req.auth_id), req.amount)
        except ChainError as e:
            log.warning("auth.chain_rejected", auth_id=req.auth_id, error=e.name or str(e))
            if e.transient:
                return Decision.decline(DeclineReason.CHAIN_UNAVAILABLE), snapshot, owner, None
            reason = CHAIN_ERROR_REASONS.get(e.name or "", DeclineReason.CHAIN_REJECTED)
            return Decision.decline(reason), snapshot, owner, None
        return Decision.approve(), snapshot, owner, tx

    # -------------------------------------------------------------- storage

    def _hold(self, owner: Pubkey | None, auth_id: str) -> Pubkey | None:
        if owner is None:
            return None
        pdas = self.chain.program.pdas
        return pdas.hold(pdas.vault(owner), auth_id_bytes(auth_id))

    async def _store_snapshot(self, auth_id: str, owner: Pubkey, snap: VaultSnapshot) -> None:
        async with transaction(self.sm) as s:
            await s.execute(
                update(Authorization)
                .where(Authorization.auth_id == auth_id)
                .values(
                    owner_pubkey=str(owner),
                    snapshot=snap.to_json(),
                    snapshot_slot=snap.slot,
                    hold_address=str(self._hold(owner, auth_id)),
                )
            )

    async def _mark_attempt(self, auth_id: str) -> None:
        # Recorded *before* sending, so a crash or timeout mid-send leaves a
        # trace the compensation worker can act on.
        async with transaction(self.sm) as s:
            await s.execute(
                update(Authorization)
                .where(Authorization.auth_id == auth_id)
                .values(authorize_attempted_at=func.now())
            )

    async def _publish(
        self,
        auth_id: str,
        decision: Decision,
        response: str,
        snapshot: VaultSnapshot | None,
        owner: Pubkey | None,
        tx: TxResult | None,
    ) -> str:
        """Compare-and-set of the final decision; returns the stored response."""
        async with transaction(self.sm) as s:
            conds = [Authorization.auth_id == auth_id, Authorization.decision.is_(None)]
            if decision.approved:
                conds.append(func.clock_timestamp() <= Authorization.deadline_at)
            res = await s.execute(
                update(Authorization)
                .where(*conds)
                .values(
                    decision="approved" if decision.approved else "declined",
                    decline_reason=decision.reason.value if decision.reason else None,
                    decided_at=func.now(),
                    response_body=response,
                    state=AuthState.APPROVED if decision.approved else AuthState.DECLINED,
                    authorize_sig=tx.signature if tx else None,
                    authorize_slot=tx.slot if tx else None,
                )
                .returning(Authorization.response_body)
            )
            won = res.scalar_one_or_none()
            if won is not None:
                log.info(
                    "auth.decided",
                    auth_id=auth_id,
                    decision="approved" if decision.approved else "declined",
                    reason=decision.reason,
                    slot=snapshot.slot if snapshot else None,
                )
                if not decision.approved:
                    await self._flag_compensation(s, auth_id)
                return won

            # Lost the race (deadline passed and a timeout decline was written).
            stored = await s.scalar(
                select(Authorization.response_body).where(Authorization.auth_id == auth_id)
            )
            if tx is not None:
                log.warning("auth.orphan_hold", auth_id=auth_id, sig=tx.signature)
            await self._flag_compensation(s, auth_id)
            assert stored is not None
            return stored

    async def _flag_compensation(self, s: AsyncSession, auth_id: str) -> None:
        await s.execute(
            update(Authorization)
            .where(
                Authorization.auth_id == auth_id,
                Authorization.authorize_attempted_at.is_not(None),
                Authorization.decision == "declined",
                Authorization.compensation.is_(None),
            )
            .values(compensation=Compensation.PENDING)
        )

    async def _await_decision(self, req: AuthRequest) -> str:
        """Duplicate request: wait for the first request's stored response."""
        auth_id = req.auth_id
        async with self.notifier.subscription(auth_id) as wait:
            while True:
                async with self.sm() as s:
                    row = (
                        await s.execute(
                            select(
                                Authorization.response_body,
                                Authorization.deadline_at < func.clock_timestamp(),
                            ).where(Authorization.auth_id == auth_id)
                        )
                    ).one()
                body, past_deadline = row
                if body is not None:
                    return str(body)
                if past_deadline:
                    # The first request missed its own deadline (slow chain or
                    # crash): settle the decision as a timeout decline.
                    return await self._finalize_timeout(req)
                await wait()

    async def _finalize_timeout(self, req: AuthRequest) -> str:
        auth_id = req.auth_id
        response = render_response(auth_id, Decision.decline(DeclineReason.TIMEOUT))
        async with transaction(self.sm) as s:
            # The timeout may have fired before our claim was committed: make
            # sure a row with the decline exists so no later request can approve.
            await s.execute(
                insert(Authorization)
                .values(
                    auth_id=auth_id,
                    card_id=req.card_id,
                    amount=req.amount,
                    currency=req.currency,
                    merchant=req.merchant,
                    deadline_at=func.now(),
                    state=AuthState.DECLINED,
                    decision="declined",
                    decline_reason=DeclineReason.TIMEOUT.value,
                    decided_at=func.now(),
                    response_body=response,
                )
                .on_conflict_do_nothing(index_elements=["auth_id"])
            )
            await s.execute(
                update(Authorization)
                .where(Authorization.auth_id == auth_id, Authorization.decision.is_(None))
                .values(
                    decision="declined",
                    decline_reason=DeclineReason.TIMEOUT.value,
                    decided_at=func.now(),
                    response_body=response,
                    state=AuthState.DECLINED,
                )
            )
            await self._flag_compensation(s, auth_id)
            stored = await s.scalar(
                select(Authorization.response_body).where(Authorization.auth_id == auth_id)
            )
        await self.notifier.publish(auth_id)
        assert stored is not None
        return stored


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
