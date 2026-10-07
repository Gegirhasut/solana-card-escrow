from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from solders.keypair import Keypair

DEV_WEBHOOK_SECRET = "dev-only-secret-change-me"  # noqa: S105 (rejected outside chain=memory)
MIN_WEBHOOK_SECRET_LEN = 32


class Settings(BaseSettings):
    """All configuration comes from the environment (prefix ESCROW_)."""

    model_config = SettingsConfigDict(env_prefix="ESCROW_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://escrow:escrow@localhost:5433/escrow"
    redis_url: str = "redis://localhost:6391/0"

    # Webhook authentication
    webhook_secret: SecretStr = SecretStr(DEV_WEBHOOK_SECRET)
    webhook_tolerance_s: int = 300

    # The issuer stands in (declines) if we have not answered within this budget.
    auth_timeout_budget_ms: int = 1500

    # Amounts: issuer sends minor units (cents); the mint has `mint_decimals`.
    currency: str = "USD"
    currency_exponent: int = 2
    mint_decimals: int = Field(default=6, ge=0)

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

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.mint_decimals < self.currency_exponent:
            raise ValueError("mint_decimals must be >= currency_exponent")
        if self.chain == "rpc":
            # Real money paths: refuse to run with a guessable secret or no mint.
            secret = self.webhook_secret.get_secret_value()
            if secret == DEV_WEBHOOK_SECRET or len(secret) < MIN_WEBHOOK_SECRET_LEN:
                raise ValueError(
                    f"ESCROW_WEBHOOK_SECRET must be set to a random value of at least "
                    f"{MIN_WEBHOOK_SECRET_LEN} characters (e.g. `openssl rand -hex 32`)"
                )
            if not self.mint:
                raise ValueError("ESCROW_MINT is required with chain=rpc")
        return self


def load_keypair(path: Path) -> Keypair:
    raw = json.loads(path.expanduser().read_text())
    return Keypair.from_bytes(bytes(raw))


@lru_cache
def get_settings() -> Settings:
    return Settings()
