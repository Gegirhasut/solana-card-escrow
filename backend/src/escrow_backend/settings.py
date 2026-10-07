from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from solders.keypair import Keypair


class Settings(BaseSettings):
    """All configuration comes from the environment (prefix ESCROW_)."""

    model_config = SettingsConfigDict(env_prefix="ESCROW_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://escrow:escrow@localhost:5433/escrow"
    redis_url: str = "redis://localhost:6391/0"

    # Webhook authentication
    webhook_secret: SecretStr = SecretStr("dev-only-secret-change-me")
    webhook_tolerance_s: int = 300

    # The issuer stands in (declines) if we have not answered within this budget.
    auth_timeout_budget_ms: int = 1500

    # Amounts: issuer sends minor units (cents); the mint has `mint_decimals`.
    currency: str = "USD"
    currency_exponent: int = 2
    mint_decimals: int = 6

    # Chain
    chain: str = Field(default="rpc", pattern="^(rpc|memory)$")
    rpc_url: str = "http://127.0.0.1:8899"
    program_id: str = "8PyM1gDSssAqmn1qNPcwQ2y6nxFjUGwhPp81obhmAwpK"
    mint: str = ""
    token_program: str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105 (program id)
    operator_keypair: Path = Path("~/.config/solana/card-escrow/operator.json")
    settlement_authority_keypair: Path | None = Path(
        "~/.config/solana/card-escrow/settlement-authority.json"
    )
    confirm_timeout_s: float = 30.0
    # Signature-status polling interval; raise it on rate-limited public RPCs.
    confirm_poll_s: float = Field(default=0.15, gt=0)
    compute_unit_price: int = 0
    compute_unit_limit: int = 0

    # Background work
    worker_interval_s: float = 5.0


def load_keypair(path: Path) -> Keypair:
    raw = json.loads(path.expanduser().read_text())
    return Keypair.from_bytes(bytes(raw))


@lru_cache
def get_settings() -> Settings:
    return Settings()
