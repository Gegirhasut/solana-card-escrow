"""In-memory implementation of the card_escrow program rules.

Used by unit tests and by `ESCROW_CHAIN=memory`. It enforces the same
invariants as the on-chain program (PDA-init idempotency, held_total,
daily/velocity windows, capture/release/expire state machine) so service-level
tests exercise realistic failure paths without a validator. It does not model
transport effects (signature deduplication, landed-but-unconfirmed
transactions); those paths are covered by gateway unit tests and the
integration tests.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from solders.pubkey import Pubkey

from escrow_backend.decision import SECONDS_PER_DAY, VaultSnapshot
from escrow_backend.solana.gateway import ACCOUNT_ALREADY_IN_USE, ChainError, TxResult
from escrow_backend.solana.program import (
    Config,
    EscrowProgram,
    Hold,
    HoldStatus,
    UserVault,
)


@dataclass
class _Vault:
    state: UserVault
    balance: int


@dataclass
class InMemoryChain:
    program: EscrowProgram
    operator: Pubkey = field(default_factory=Pubkey.new_unique)
    ttl: int = 7 * SECONDS_PER_DAY
    paused: bool = False
    clock: Callable[[], int] = field(default=lambda: int(time.time()))
    latency_s: float = 0.0
    settlement_balance: int = 0
    vaults: dict[Pubkey, _Vault] = field(default_factory=dict)
    holds: dict[Pubkey, Hold] = field(default_factory=dict)
    refunds: dict[Pubkey, int] = field(default_factory=dict)  # refund PDA -> amount
    sent: list[tuple[str, Pubkey, bytes, int]] = field(default_factory=list)
    fail_next: list[ChainError] = field(default_factory=list)
    _slot: itertools.count[int] = field(default_factory=lambda: itertools.count(1000))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---------------------------------------------------------- test setup

    def open_vault(
        self,
        owner: Pubkey,
        balance: int,
        *,
        daily_limit: int = 10**15,
        velocity_max_auths: int = 1_000,
        velocity_window_seconds: int = 3_600,
    ) -> None:
        now = self.clock()
        self.vaults[owner] = _Vault(
            UserVault(
                owner=owner,
                held_total=0,
                daily_limit=daily_limit,
                daily_spent=0,
                day_start_ts=now,
                velocity_max_auths=velocity_max_auths,
                velocity_window_seconds=velocity_window_seconds,
                window_start_ts=now,
                window_count=0,
                bump=255,
            ),
            balance,
        )

    def withdraw(self, owner: Pubkey, amount: int) -> None:
        v = self.vaults[owner]
        if amount > v.balance - v.state.held_total:
            raise ChainError("withdraw", name="InsufficientAvailableBalance")
        v.balance -= amount

    def hold_of(self, owner: Pubkey, auth_id: bytes) -> Hold | None:
        return self.holds.get(self._hold_key(owner, auth_id))

    def _hold_key(self, owner: Pubkey, auth_id: bytes) -> Pubkey:
        return self.program.pdas.hold(self.program.pdas.vault(owner), auth_id)

    # ------------------------------------------------------- gateway API

    async def close(self) -> None:
        return None

    async def config(self) -> Config | None:
        return Config(
            admin=Pubkey.default(),
            operator=self.operator,
            mint=self.program.mint,
            settlement_token_account=Pubkey.default(),
            settlement_authority=Pubkey.default(),
            paused=self.paused,
            default_hold_ttl_seconds=self.ttl,
            bump=255,
        )

    async def snapshot(self, owner: Pubkey) -> VaultSnapshot:
        await self._io()
        v = self.vaults.get(owner)
        return VaultSnapshot(
            slot=next(self._slot),
            chain_ts=self.clock(),
            paused=self.paused,
            vault=v.state if v else None,
            balance=v.balance if v else 0,
        )

    async def get_holds(self, holds: list[Pubkey]) -> dict[Pubkey, Hold | None]:
        await self._io()
        return {pk: self.holds.get(pk) for pk in holds}

    async def accounts_exist(self, keys: list[Pubkey]) -> dict[Pubkey, bool]:
        await self._io()
        return {k: k in self.holds or k in self.refunds for k in keys}

    async def get_refund_amounts(self, keys: list[Pubkey]) -> dict[Pubkey, int | None]:
        await self._io()
        return {k: self.refunds.get(k) for k in keys}

    async def authorize(self, owner: Pubkey, auth_id: bytes, amount: int) -> TxResult:
        async with self._tx():
            if self.paused:
                raise ChainError("paused", name="Paused")
            key = self._hold_key(owner, auth_id)
            if key in self.holds:
                raise ChainError("hold exists", name=ACCOUNT_ALREADY_IN_USE)
            v = self._vault(owner)
            s = v.state
            now = self.clock()
            if amount <= 0:
                raise ChainError("zero", name="ZeroAmount")
            if amount > v.balance - s.held_total:
                raise ChainError("insufficient", name="InsufficientAvailableBalance")
            day_start, spent = (
                (now, 0)
                if now - s.day_start_ts >= SECONDS_PER_DAY
                else (
                    s.day_start_ts,
                    s.daily_spent,
                )
            )
            win_start, count = (
                (now, 0)
                if now - s.window_start_ts >= s.velocity_window_seconds
                else (s.window_start_ts, s.window_count)
            )
            if spent + amount > s.daily_limit:
                raise ChainError("daily", name="DailyLimitExceeded")
            if count + 1 > s.velocity_max_auths:
                raise ChainError("velocity", name="VelocityLimitExceeded")
            v.state = replace(
                s,
                held_total=s.held_total + amount,
                daily_spent=spent + amount,
                day_start_ts=day_start,
                window_start_ts=win_start,
                window_count=count + 1,
            )
            self.holds[key] = Hold(
                vault=self.program.pdas.vault(owner),
                auth_id=auth_id,
                amount=amount,
                captured_amount=0,
                status=HoldStatus.PENDING,
                created_ts=now,
                expires_ts=now + self.ttl,
                rent_payer=self.operator,
                bump=255,
            )
            return self._ok("authorize", owner, auth_id, amount)

    async def capture(self, owner: Pubkey, auth_id: bytes, amount: int, memo: str = "") -> TxResult:
        async with self._tx():
            if self.paused:
                raise ChainError("paused", name="Paused")
            key, h = self._pending(owner, auth_id)
            if self.clock() >= h.expires_ts:
                raise ChainError("expired", name="HoldExpired")
            if amount <= 0:
                raise ChainError("zero", name="ZeroAmount")
            if amount > h.amount:
                raise ChainError("exceeds", name="CaptureExceedsHold")
            self._settle(owner, h, unused=h.amount - amount)
            self.vaults[owner].balance -= amount
            self.settlement_balance += amount
            self.holds[key] = replace(h, status=HoldStatus.CAPTURED, captured_amount=amount)
            return self._ok("capture", owner, auth_id, amount)

    async def release(self, owner: Pubkey, auth_id: bytes) -> TxResult:
        async with self._tx():
            key, h = self._pending(owner, auth_id)
            self._settle(owner, h, unused=h.amount)
            self.holds[key] = replace(h, status=HoldStatus.RELEASED)
            return self._ok("release", owner, auth_id, h.amount)

    async def expire(self, owner: Pubkey, auth_id: bytes) -> TxResult:
        async with self._tx():
            key, h = self._pending(owner, auth_id)
            if self.clock() < h.expires_ts:
                raise ChainError("not expired", name="HoldNotYetExpired")
            self._settle(owner, h, unused=h.amount)
            self.holds[key] = replace(h, status=HoldStatus.EXPIRED)
            return self._ok("expire", owner, auth_id, h.amount)

    async def refund(self, owner: Pubkey, refund_id: bytes, amount: int) -> TxResult:
        async with self._tx():
            vault = self.program.pdas.vault(owner)
            key = self.program.pdas.refund(vault, refund_id)
            if key in self.refunds:
                raise ChainError("refund exists", name=ACCOUNT_ALREADY_IN_USE)
            v = self._vault(owner)
            if amount <= 0:
                raise ChainError("zero", name="ZeroAmount")
            if amount > self.settlement_balance:
                raise ChainError("settlement balance too low", name=None)
            self.settlement_balance -= amount
            v.balance += amount
            self.refunds[key] = amount
            return self._ok("refund", owner, refund_id, amount)

    # ----------------------------------------------------------- helpers

    async def _io(self) -> None:
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _tx(self) -> _TxContext:
        return _TxContext(self)

    def _vault(self, owner: Pubkey) -> _Vault:
        v = self.vaults.get(owner)
        if v is None:
            raise ChainError("vault account not initialized", name="AccountNotInitialized")
        return v

    def _pending(self, owner: Pubkey, auth_id: bytes) -> tuple[Pubkey, Hold]:
        key = self._hold_key(owner, auth_id)
        h = self.holds.get(key)
        if h is None:
            raise ChainError("hold account not initialized", name="AccountNotInitialized")
        if h.status != HoldStatus.PENDING:
            raise ChainError("hold not pending", name="HoldNotPending")
        return key, h

    def _settle(self, owner: Pubkey, h: Hold, unused: int) -> None:
        v = self.vaults[owner]
        s = v.state
        spent = s.daily_spent - unused if h.created_ts >= s.day_start_ts else s.daily_spent
        v.state = replace(s, held_total=s.held_total - h.amount, daily_spent=spent)

    def _ok(self, kind: str, owner: Pubkey, ident: bytes, amount: int) -> TxResult:
        self.sent.append((kind, owner, ident, amount))
        slot = next(self._slot)
        return TxResult(signature=f"fake-{kind}-{slot}", slot=slot)


class _TxContext:
    def __init__(self, chain: InMemoryChain) -> None:
        self.chain = chain

    async def __aenter__(self) -> None:
        await self.chain._io()
        await self.chain._lock.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self.chain._lock.release()
