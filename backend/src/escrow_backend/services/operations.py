"""Clearing (-> capture), reversal (-> release) and refund (-> refund).

Each webhook is keyed by `(kind, external_id)` (unique). Processing:

1. Insert the operation row (`ON CONFLICT DO NOTHING`).
2. Claim it with a lease (`status -> processing`, `lease_until`), so only one
   request or worker executes it at a time; a crashed holder's lease expires.
3. Execute the chain instruction. The program is idempotent on its own
   (a hold can only leave Pending once; a refund_id PDA can only be created
   once), so a retry after an ambiguous failure cannot double-move funds:
   "already done" errors are resolved by reading the on-chain state.
4. Store a final response (succeeded / failed) that duplicates replay verbatim.
   Transient failures go to `retry` for the worker; the issuer gets 202.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from solders.pubkey import Pubkey
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from escrow_backend.db import transaction
from escrow_backend.ids import auth_id_bytes, refund_id_bytes
from escrow_backend.models import (
    Authorization,
    AuthState,
    Card,
    IssuerOperation,
    OpKind,
    OpStatus,
)
from escrow_backend.solana.gateway import ACCOUNT_ALREADY_IN_USE, ChainError, ChainGateway, TxResult
from escrow_backend.solana.program import HoldStatus

log = structlog.get_logger(__name__)

LEASE = timedelta(seconds=30)
MAX_ATTEMPTS = 20


@dataclass(frozen=True)
class OpRequest:
    kind: OpKind
    external_id: str
    auth_id: str | None
    card_id: str | None
    amount: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class OpResult:
    status_code: int  # 200 final, 202 accepted / processing
    body: str


class _Permanent(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _body(req_kind: str, external_id: str, status: str, **extra: Any) -> str:
    return json.dumps(
        {"kind": req_kind, "id": external_id, "status": status, **extra}, separators=(",", ":")
    )


class OperationService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], chain: ChainGateway) -> None:
        self.sm = sessionmaker
        self.chain = chain

    async def handle(self, req: OpRequest) -> OpResult:
        async with transaction(self.sm) as s:
            await s.execute(
                insert(IssuerOperation)
                .values(
                    kind=req.kind,
                    external_id=req.external_id,
                    auth_id=req.auth_id,
                    card_id=req.card_id,
                    amount=req.amount,
                    request=req.raw,
                    status=OpStatus.PENDING,
                )
                .on_conflict_do_nothing(constraint="uq_issuer_operations_kind_external_id")
            )
        return await self.process(req.kind, req.external_id)

    async def process(self, kind: str, external_id: str) -> OpResult:
        op = await self._claim(kind, external_id)
        if op is None:
            return await self._current(kind, external_id)
        try:
            tx, extra = await self._execute(op)
        except _Permanent as e:
            return await self._finish(op, OpStatus.FAILED, None, error=e.reason)
        except ChainError as e:
            if e.transient and op.attempts < MAX_ATTEMPTS:
                return await self._retry(op, str(e))
            return await self._finish(op, OpStatus.FAILED, None, error=e.name or str(e))
        return await self._finish(op, OpStatus.SUCCEEDED, tx, **extra)

    # ------------------------------------------------------------- leasing

    async def _claim(self, kind: str, external_id: str) -> IssuerOperation | None:
        async with transaction(self.sm) as s:
            res = await s.execute(
                update(IssuerOperation)
                .where(
                    IssuerOperation.kind == kind,
                    IssuerOperation.external_id == external_id,
                    or_(
                        IssuerOperation.status.in_([OpStatus.PENDING, OpStatus.RETRY]),
                        (IssuerOperation.status == OpStatus.PROCESSING)
                        & (IssuerOperation.lease_until < func.now()),
                    ),
                )
                .values(
                    status=OpStatus.PROCESSING,
                    lease_until=func.now() + LEASE,
                    attempts=IssuerOperation.attempts + 1,
                )
                .returning(IssuerOperation)
            )
            return res.scalar_one_or_none()

    async def _current(self, kind: str, external_id: str) -> OpResult:
        async with self.sm() as s:
            op = await s.scalar(
                select(IssuerOperation).where(
                    IssuerOperation.kind == kind, IssuerOperation.external_id == external_id
                )
            )
        assert op is not None
        if op.status in (OpStatus.SUCCEEDED, OpStatus.FAILED) and op.response_body:
            return OpResult(200, op.response_body)
        return OpResult(202, _body(kind, external_id, "processing"))

    async def _retry(self, op: IssuerOperation, error: str) -> OpResult:
        log.warning("op.retry", kind=op.kind, id=op.external_id, error=error)
        async with transaction(self.sm) as s:
            await s.execute(
                update(IssuerOperation)
                .where(IssuerOperation.id == op.id)
                .values(status=OpStatus.RETRY, last_error=error, lease_until=None)
            )
        return OpResult(202, _body(op.kind, op.external_id, "processing"))

    async def _finish(
        self,
        op: IssuerOperation,
        status: OpStatus,
        tx: TxResult | None,
        *,
        error: str | None = None,
        **extra: Any,
    ) -> OpResult:
        body = _body(
            op.kind,
            op.external_id,
            status.value,
            error=error,
            signature=tx.signature if tx else None,
            **extra,
        )
        async with transaction(self.sm) as s:
            await s.execute(
                update(IssuerOperation)
                .where(IssuerOperation.id == op.id)
                .values(
                    status=status,
                    response_body=body,
                    last_error=error,
                    tx_sig=tx.signature if tx else None,
                    lease_until=None,
                )
            )
            if status == OpStatus.SUCCEEDED:
                await self._apply_to_authorization(s, op, extra)
        log.info("op.finished", kind=op.kind, id=op.external_id, status=status, error=error)
        return OpResult(200, body)

    async def _apply_to_authorization(
        self, s: AsyncSession, op: IssuerOperation, extra: dict[str, Any]
    ) -> None:
        if op.auth_id is None or op.kind == OpKind.REFUND:
            return
        values: dict[str, Any]
        if op.kind == OpKind.CLEARING:
            values = {"state": AuthState.CAPTURED, "captured_amount": extra["captured_amount"]}
        else:
            values = {"state": extra.get("hold_state", AuthState.RELEASED)}
        await s.execute(
            update(Authorization).where(Authorization.auth_id == op.auth_id).values(**values)
        )

    # ----------------------------------------------------------- execution

    async def _execute(self, op: IssuerOperation) -> tuple[TxResult | None, dict[str, Any]]:
        if op.kind == OpKind.REFUND:
            return await self._refund(op)
        auth = await self._approved_auth(op)
        owner = Pubkey.from_string(auth.owner_pubkey or "")
        aid = auth_id_bytes(auth.auth_id)
        if op.kind == OpKind.CLEARING:
            return await self._capture(op, owner, aid, auth)
        return await self._release(owner, aid)

    async def _approved_auth(self, op: IssuerOperation) -> Authorization:
        async with self.sm() as s:
            auth = await s.scalar(select(Authorization).where(Authorization.auth_id == op.auth_id))
        if auth is None:
            raise _Permanent("unknown_authorization")
        if auth.decision != "approved" or auth.owner_pubkey is None:
            raise _Permanent("authorization_not_approved")
        return auth

    async def _hold_status(self, owner: Pubkey, aid: bytes) -> tuple[HoldStatus | None, int]:
        pdas = self.chain.program.pdas
        key = pdas.hold(pdas.vault(owner), aid)
        hold = (await self.chain.get_holds([key]))[key]
        return (hold.status, hold.captured_amount) if hold else (None, 0)

    async def _capture(
        self, op: IssuerOperation, owner: Pubkey, aid: bytes, auth: Authorization
    ) -> tuple[TxResult | None, dict[str, Any]]:
        amount = op.amount or 0
        if amount <= 0:
            raise _Permanent("invalid_amount")
        if amount > auth.amount:
            raise _Permanent("capture_exceeds_authorization")
        try:
            tx = await self.chain.capture(owner, aid, amount)
        except ChainError as e:
            if e.name == "HoldNotPending":
                status, captured = await self._hold_status(owner, aid)
                if (
                    status == HoldStatus.CAPTURED
                    and captured == amount
                    and op.attempts > 1
                    and not await self._other_clearing_succeeded(op)
                ):
                    # Our own earlier attempt landed but its outcome was lost
                    # (e.g. confirmation timeout): report success idempotently.
                    return None, {"captured_amount": captured}
                raise _Permanent(f"hold_{status.name.lower() if status else 'missing'}") from e
            if e.name == "HoldExpired":
                raise _Permanent("hold_expired") from e
            raise
        return tx, {"captured_amount": amount}

    async def _other_clearing_succeeded(self, op: IssuerOperation) -> bool:
        async with self.sm() as s:
            other = await s.scalar(
                select(IssuerOperation.id).where(
                    IssuerOperation.kind == OpKind.CLEARING,
                    IssuerOperation.auth_id == op.auth_id,
                    IssuerOperation.external_id != op.external_id,
                    IssuerOperation.status == OpStatus.SUCCEEDED,
                )
            )
        return other is not None

    async def _release(self, owner: Pubkey, aid: bytes) -> tuple[TxResult | None, dict[str, Any]]:
        try:
            tx = await self.chain.release(owner, aid)
        except ChainError as e:
            if e.name == "HoldNotPending":
                status, _ = await self._hold_status(owner, aid)
                if status == HoldStatus.RELEASED:
                    return None, {"hold_state": AuthState.RELEASED}
                if status == HoldStatus.EXPIRED:
                    return None, {"hold_state": AuthState.EXPIRED}
                raise _Permanent("hold_already_captured") from e
            raise
        return tx, {"hold_state": AuthState.RELEASED}

    async def _refund(self, op: IssuerOperation) -> tuple[TxResult | None, dict[str, Any]]:
        amount = op.amount or 0
        if amount <= 0:
            raise _Permanent("invalid_amount")
        async with self.sm() as s:
            card = await s.get(Card, op.card_id) if op.card_id else None
        if card is None:
            raise _Permanent("card_not_found")
        owner = Pubkey.from_string(card.owner_pubkey)
        try:
            tx = await self.chain.refund(owner, refund_id_bytes(op.external_id), amount)
        except ChainError as e:
            if e.name == ACCOUNT_ALREADY_IN_USE:
                return None, {"already_refunded": True}
            raise
        return tx, {}

    # ---------------------------------------------------------------- worker

    async def pending_ids(self, limit: int = 100) -> list[tuple[str, str]]:
        async with self.sm() as s:
            rows = await s.execute(
                select(IssuerOperation.kind, IssuerOperation.external_id)
                .where(
                    or_(
                        IssuerOperation.status.in_([OpStatus.RETRY, OpStatus.PENDING]),
                        (IssuerOperation.status == OpStatus.PROCESSING)
                        & (IssuerOperation.lease_until < func.now()),
                    )
                )
                .order_by(IssuerOperation.id)
                .limit(limit)
            )
            return [(k, e) for k, e in rows.all()]
