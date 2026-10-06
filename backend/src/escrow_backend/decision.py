"""Authorization decision logic.

Mirrors the on-chain checks in `UserVault::record_authorization` so the
backend can explain a decline without spending a transaction. The program
remains the final arbiter: an approval is only returned after the authorize
transaction has been confirmed on-chain.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass
from typing import Any

from escrow_backend.solana.program import Config, UserVault

SECONDS_PER_DAY = 86_400


class DeclineReason(enum.StrEnum):
    CARD_NOT_FOUND = "card_not_found"
    CARD_INACTIVE = "card_inactive"
    VAULT_NOT_FOUND = "vault_not_found"
    PAUSED = "paused"
    INVALID_AMOUNT = "invalid_amount"
    CURRENCY_MISMATCH = "currency_mismatch"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    DAILY_LIMIT = "daily_limit_exceeded"
    VELOCITY_LIMIT = "velocity_limit_exceeded"
    CHAIN_REJECTED = "chain_rejected"
    CHAIN_UNAVAILABLE = "chain_unavailable"
    TIMEOUT = "timeout"


# On-chain error name -> decline reason, for preflight/confirmation failures.
CHAIN_ERROR_REASONS: dict[str, DeclineReason] = {
    "Paused": DeclineReason.PAUSED,
    "ZeroAmount": DeclineReason.INVALID_AMOUNT,
    "InsufficientAvailableBalance": DeclineReason.INSUFFICIENT_FUNDS,
    "DailyLimitExceeded": DeclineReason.DAILY_LIMIT,
    "VelocityLimitExceeded": DeclineReason.VELOCITY_LIMIT,
}


@dataclass(frozen=True)
class VaultSnapshot:
    """On-chain state read at `slot` (confirmed commitment)."""

    slot: int
    chain_ts: int
    paused: bool
    vault: UserVault | None
    balance: int

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "slot": self.slot,
            "chain_ts": self.chain_ts,
            "paused": self.paused,
            "balance": self.balance,
            "vault": None,
        }
        if self.vault is not None:
            v = asdict(self.vault)
            v["owner"] = str(self.vault.owner)
            out["vault"] = v
        return out


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: DeclineReason | None = None

    @classmethod
    def approve(cls) -> Decision:
        return cls(True, None)

    @classmethod
    def decline(cls, reason: DeclineReason) -> Decision:
        return cls(False, reason)


def evaluate(snapshot: VaultSnapshot, amount: int) -> Decision:
    """Pre-checks an authorization of `amount` base units against a snapshot."""
    if snapshot.paused:
        return Decision.decline(DeclineReason.PAUSED)
    v = snapshot.vault
    if v is None:
        return Decision.decline(DeclineReason.VAULT_NOT_FOUND)
    if amount <= 0:
        return Decision.decline(DeclineReason.INVALID_AMOUNT)
    available = snapshot.balance - v.held_total
    if amount > available:
        return Decision.decline(DeclineReason.INSUFFICIENT_FUNDS)

    now = snapshot.chain_ts
    daily_spent = 0 if now - v.day_start_ts >= SECONDS_PER_DAY else v.daily_spent
    if daily_spent + amount > v.daily_limit:
        return Decision.decline(DeclineReason.DAILY_LIMIT)
    window_count = 0 if now - v.window_start_ts >= v.velocity_window_seconds else v.window_count
    if window_count + 1 > v.velocity_max_auths:
        return Decision.decline(DeclineReason.VELOCITY_LIMIT)
    return Decision.approve()


def config_paused(config: Config | None) -> bool:
    # A missing config means the deployment is not usable: treat as paused.
    return True if config is None else config.paused
