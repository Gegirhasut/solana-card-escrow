"""Persistent state of the issuer integration.

Idempotency is enforced by unique constraints: `authorizations.auth_id` and
`(kind, external_id)` on `issuer_operations`. Webhook responses are stored as
text and replayed byte-for-byte to duplicate deliveries.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CardStatus(enum.StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"


class AuthState(enum.StrEnum):
    PENDING = "pending"  # received, no decision yet
    APPROVED = "approved"  # hold confirmed on-chain
    DECLINED = "declined"
    CAPTURED = "captured"
    RELEASED = "released"
    EXPIRED = "expired"


class Compensation(enum.StrEnum):
    """Tracks holds that may exist on-chain for a declined authorization
    (e.g. the authorize tx landed after the issuer's timeout)."""

    PENDING = "pending"
    RELEASED = "released"
    NOT_NEEDED = "not_needed"


class OpKind(enum.StrEnum):
    CLEARING = "clearing"
    REVERSAL = "reversal"
    REFUND = "refund"


class OpStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _now() -> Any:
    return func.now()


class Card(Base):
    __tablename__ = "cards"

    card_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_pubkey: Mapped[str] = mapped_column(String(44), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CardStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class Authorization(Base):
    __tablename__ = "authorizations"
    __table_args__ = (
        UniqueConstraint("auth_id", name="uq_authorizations_auth_id"),
        Index("ix_authorizations_state", "state"),
        Index("ix_authorizations_compensation", "compensation"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    auth_id: Mapped[str] = mapped_column(String(128), nullable=False)
    card_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # token base units
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    merchant: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    decision: Mapped[str | None] = mapped_column(String(16))
    decline_reason: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_body: Mapped[str | None] = mapped_column(Text)

    owner_pubkey: Mapped[str | None] = mapped_column(String(44))
    hold_address: Mapped[str | None] = mapped_column(String(44))
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    snapshot_slot: Mapped[int | None] = mapped_column(BigInteger)
    authorize_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorize_sig: Mapped[str | None] = mapped_column(String(100))
    authorize_slot: Mapped[int | None] = mapped_column(BigInteger)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default=AuthState.PENDING)
    captured_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    compensation: Mapped[str | None] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_now(), onupdate=_now()
    )


class IssuerOperation(Base):
    """Clearing, reversal and refund webhooks: one row per external id."""

    __tablename__ = "issuer_operations"
    __table_args__ = (
        UniqueConstraint("kind", "external_id", name="uq_issuer_operations_kind_external_id"),
        Index("ix_issuer_operations_status", "status"),
        Index("ix_issuer_operations_auth_id", "auth_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    auth_id: Mapped[str | None] = mapped_column(String(128))
    card_id: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[int | None] = mapped_column(BigInteger)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OpStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    tx_sig: Mapped[str | None] = mapped_column(String(100))
    response_body: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_now(), onupdate=_now()
    )
