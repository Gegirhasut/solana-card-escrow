"""Background jobs.

* `retry_operations`  - re-drives clearing/reversal/refund rows left in
  retry or with an expired lease.
* `finalize_stale`    - declines authorizations that never got a decision
  (the deciding request crashed or lost the DB after sending), so their
  holds are compensated.
* `compensate`        - releases holds that exist on-chain for declined
  authorizations (authorize tx landed after a timeout or crash).
* `expire_holds`      - calls the permissionless `expire_hold` for approved
  holds past their TTL and syncs the authorization state.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import structlog
from solders.pubkey import Pubkey
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from escrow_backend.db import transaction
from escrow_backend.decision import Decision, DeclineReason
from escrow_backend.ids import auth_id_bytes
from escrow_backend.models import Authorization, AuthState, Compensation
from escrow_backend.services.authorizations import render_response
from escrow_backend.services.operations import OperationService
from escrow_backend.solana.gateway import ChainError, ChainGateway
from escrow_backend.solana.program import HoldStatus

log = structlog.get_logger(__name__)

# An authorize tx can still land until its blockhash expires (~60-90 s).
TX_LANDING_WINDOW = timedelta(seconds=120)

_FINAL_STATE = {
    HoldStatus.CAPTURED: AuthState.CAPTURED,
    HoldStatus.RELEASED: AuthState.RELEASED,
    HoldStatus.EXPIRED: AuthState.EXPIRED,
}


class Worker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        chain: ChainGateway,
        operations: OperationService,
    ) -> None:
        self.sm = sessionmaker
        self.chain = chain
        self.ops = operations

    async def run_once(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        jobs = {
            "operations": self.retry_operations,
            "finalized": self.finalize_stale,
            "compensated": self.compensate,
            "expired": self.expire_holds,
        }
        for name, job in jobs.items():
            # Isolated: an RPC outage in one job must not starve the others.
            try:
                stats[name] = await job()
            except Exception as e:
                log.exception("worker.job_failed", job=name, error=str(e))
                stats[name] = 0
        return stats

    async def run_forever(self, interval_s: float) -> None:
        while True:
            try:
                stats = await self.run_once()
                if any(stats.values()):
                    log.info("worker.tick", **stats)
            except Exception as e:  # keep the loop alive; each job is idempotent
                log.exception("worker.error", error=str(e))
            await asyncio.sleep(interval_s)

    async def retry_operations(self) -> int:
        n = 0
        for kind, external_id in await self.ops.pending_ids():
            try:
                await self.ops.process(kind, external_id)
            except Exception as e:
                log.exception("worker.op_failed", kind=kind, id=external_id, error=str(e))
            n += 1
        return n

    async def finalize_stale(self) -> int:
        """Timeout-declines authorizations still undecided after their deadline.

        Approvals are only written while `clock_timestamp() <= deadline_at`, so
        once the deadline has passed nobody can approve any more and this
        decline is the final answer. Attempted ones get flagged for
        compensation, which releases a hold that may have landed.
        """
        async with transaction(self.sm) as s:
            rows = (
                await s.execute(
                    select(Authorization.id, Authorization.auth_id)
                    .where(
                        Authorization.decision.is_(None),
                        Authorization.deadline_at < func.now(),
                    )
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row_id, auth_id in rows:
                await s.execute(
                    update(Authorization)
                    .where(Authorization.id == row_id, Authorization.decision.is_(None))
                    .values(
                        decision="declined",
                        decline_reason=DeclineReason.TIMEOUT.value,
                        decided_at=func.now(),
                        response_body=render_response(
                            auth_id, Decision.decline(DeclineReason.TIMEOUT)
                        ),
                        state=AuthState.DECLINED,
                        compensation=case(
                            (
                                Authorization.authorize_attempted_at.is_not(None),
                                Compensation.PENDING,
                            ),
                            else_=None,
                        ),
                    )
                )
        return len(rows)

    def _hold_key(self, owner: str, auth_id: str) -> Pubkey:
        pdas = self.chain.program.pdas
        return pdas.hold(pdas.vault(Pubkey.from_string(owner)), auth_id_bytes(auth_id))

    async def compensate(self) -> int:
        async with self.sm() as s:
            rows = (
                await s.scalars(
                    select(Authorization)
                    .where(Authorization.compensation == Compensation.PENDING)
                    .limit(100)
                )
            ).all()
        if not rows:
            return 0
        holds = await self.chain.get_holds(
            [self._hold_key(r.owner_pubkey, r.auth_id) for r in rows if r.owner_pubkey]
        )
        done = 0
        for r in rows:
            if r.owner_pubkey is None:
                await self._set_compensation(r.id, Compensation.NOT_NEEDED)
                continue
            hold = holds.get(self._hold_key(r.owner_pubkey, r.auth_id))
            if hold is None:
                async with self.sm() as s:
                    landed_window_over = await s.scalar(
                        select(
                            func.now() > Authorization.authorize_attempted_at + TX_LANDING_WINDOW
                        ).where(Authorization.id == r.id)
                    )
                if landed_window_over:
                    await self._set_compensation(r.id, Compensation.NOT_NEEDED)
                    done += 1
                continue
            if hold.status == HoldStatus.PENDING:
                try:
                    await self.chain.release(
                        Pubkey.from_string(r.owner_pubkey), auth_id_bytes(r.auth_id)
                    )
                except ChainError as e:
                    if e.name != "HoldNotPending":
                        log.warning("worker.compensation_failed", auth_id=r.auth_id, error=str(e))
                        continue
                log.warning("worker.orphan_hold_released", auth_id=r.auth_id)
            await self._set_compensation(r.id, Compensation.RELEASED)
            done += 1
        return done

    async def _set_compensation(self, row_id: int, value: Compensation) -> None:
        async with transaction(self.sm) as s:
            await s.execute(
                update(Authorization).where(Authorization.id == row_id).values(compensation=value)
            )

    async def expire_holds(self) -> int:
        async with self.sm() as s:
            rows = (
                await s.scalars(
                    select(Authorization)
                    .where(Authorization.state == AuthState.APPROVED)
                    .order_by(Authorization.id)
                    .limit(200)
                )
            ).all()
        if not rows:
            return 0
        keys = {r.auth_id: self._hold_key(r.owner_pubkey or "", r.auth_id) for r in rows}
        holds = await self.chain.get_holds(list(keys.values()))
        snap_ts: int | None = None
        changed = 0
        for r in rows:
            hold = holds.get(keys[r.auth_id])
            if hold is None:
                continue
            if hold.status in _FINAL_STATE:
                # Finalized elsewhere (e.g. someone else called expire_hold).
                await self._set_state(r.id, _FINAL_STATE[hold.status], hold.captured_amount)
                changed += 1
                continue
            if snap_ts is None:
                snap_ts = (
                    await self.chain.snapshot(Pubkey.from_string(r.owner_pubkey or ""))
                ).chain_ts
            if snap_ts >= hold.expires_ts:
                try:
                    await self.chain.expire(Pubkey.from_string(r.owner_pubkey or ""), hold.auth_id)
                except ChainError as e:
                    log.warning("worker.expire_failed", auth_id=r.auth_id, error=e.name or str(e))
                    continue
                await self._set_state(r.id, AuthState.EXPIRED, 0)
                log.info("worker.hold_expired", auth_id=r.auth_id)
                changed += 1
        return changed

    async def _set_state(self, row_id: int, state: AuthState, captured: int) -> None:
        async with transaction(self.sm) as s:
            await s.execute(
                update(Authorization)
                .where(Authorization.id == row_id)
                .values(state=state, captured_amount=captured)
            )
