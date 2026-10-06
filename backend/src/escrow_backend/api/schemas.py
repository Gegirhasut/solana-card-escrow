from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_ID = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_\-:.]+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthorizationWebhook(_Strict):
    auth_id: str = _ID
    card_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(ge=0, description="Minor currency units (cents)")
    currency: str = Field(min_length=3, max_length=3)
    merchant: dict[str, Any] | None = None


class ClearingWebhook(_Strict):
    clearing_id: str = _ID
    auth_id: str = _ID
    amount: int = Field(gt=0, description="Minor currency units (cents)")


class ReversalWebhook(_Strict):
    reversal_id: str = _ID
    auth_id: str = _ID


class RefundWebhook(_Strict):
    refund_id: str = _ID
    card_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0, description="Minor currency units (cents)")
    auth_id: str | None = None
