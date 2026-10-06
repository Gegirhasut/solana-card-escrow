"""Test fixtures: a real Postgres + Redis (docker compose) and the in-memory chain.

Run `docker compose up -d` in backend/ first. Tests use a dedicated
`escrow_test` database that is migrated once per session and truncated
between tests.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from redis.asyncio import Redis
from solders.pubkey import Pubkey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from escrow_backend.db import make_engine, make_sessionmaker, transaction
from escrow_backend.models import Card
from escrow_backend.services.authorizations import AuthorizationService, AuthRequest
from escrow_backend.services.notify import Notifier
from escrow_backend.services.operations import OperationService
from escrow_backend.services.worker import Worker
from escrow_backend.settings import Settings
from escrow_backend.solana.fake import InMemoryChain
from escrow_backend.solana.program import EscrowProgram

PG_ADMIN_DSN = os.environ.get(
    "ESCROW_TEST_PG_ADMIN_DSN", "postgresql://escrow:escrow@localhost:5433/escrow"
)
TEST_DB = "escrow_test"
TEST_DB_URL = f"postgresql+asyncpg://escrow:escrow@localhost:5433/{TEST_DB}"
REDIS_URL = os.environ.get("ESCROW_TEST_REDIS_URL", "redis://localhost:6391/15")
BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))

USD = 1_000_000  # one dollar in token base units (6 decimals)
OWNER = Pubkey.from_string("9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin")
CARD = "card_0001"


@dataclass
class Clock:
    now: int = 1_760_000_000

    def __call__(self) -> int:
        return self.now


@dataclass
class Ctx:
    sm: async_sessionmaker[AsyncSession]
    chain: InMemoryChain
    clock: Clock
    auths: AuthorizationService
    ops: OperationService
    worker: Worker
    redis: Redis
    extra: dict[str, object] = field(default_factory=dict)

    def auth_service(
        self, budget_ms: int = 1500, chain: object | None = None
    ) -> AuthorizationService:
        return AuthorizationService(
            self.sm,
            chain or self.chain,
            Notifier(self.redis),
            budget_ms,  # type: ignore[arg-type]
        )

    async def authorize(self, auth_id: str, amount: int, card: str = CARD) -> dict[str, object]:
        import json

        return json.loads(await self.auths.handle(AuthRequest(auth_id, card, amount, "USD")))


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    conn = await asyncpg.connect(PG_ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()
    cfg = AlembicConfig(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "migrations"))
    cfg.attributes["database_url"] = TEST_DB_URL
    await asyncio.to_thread(command.upgrade, cfg, "head")
    eng = make_engine(TEST_DB_URL)

    async def ping() -> None:
        async with eng.connect() as c:
            await c.execute(text("SELECT 1"))
            await asyncio.sleep(0.05)

    # Warm the pool: connection setup is slow on a loaded CI box and would
    # otherwise eat into the authorization budget of concurrency tests.
    await asyncio.gather(*[ping() for _ in range(25)])
    yield eng
    await eng.dispose()


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    r = Redis.from_url(REDIS_URL)
    yield r
    await r.aclose()


@pytest.fixture
def program() -> EscrowProgram:
    return EscrowProgram(
        program_id=Pubkey.from_string("8PyM1gDSssAqmn1qNPcwQ2y6nxFjUGwhPp81obhmAwpK"),
        mint=Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
    )


@pytest.fixture
async def ctx(engine: AsyncEngine, redis: Redis, program: EscrowProgram) -> Ctx:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE authorizations, issuer_operations, cards"))
    sm = make_sessionmaker(engine)
    clock = Clock()
    chain = InMemoryChain(program=program, clock=clock, settlement_balance=1_000_000 * USD)
    chain.open_vault(OWNER, 1_000 * USD, daily_limit=500 * USD, velocity_max_auths=10)
    async with transaction(sm) as s:
        s.add(Card(card_id=CARD, owner_pubkey=str(OWNER), status="active"))
    ops = OperationService(sm, chain)
    return Ctx(
        sm=sm,
        chain=chain,
        clock=clock,
        # Generous budget: tests of the budget itself build their own service.
        auths=AuthorizationService(sm, chain, Notifier(redis), 10_000),
        ops=ops,
        worker=Worker(sm, chain, ops),
        redis=redis,
    )


@pytest.fixture
def test_settings() -> Callable[..., Settings]:
    def make(**kw: object) -> Settings:
        base: dict[str, object] = {
            "database_url": TEST_DB_URL,
            "redis_url": REDIS_URL,
            "webhook_secret": "test-secret",
            "chain": "memory",
            "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        }
        base.update(kw)
        return Settings(**base)  # type: ignore[arg-type]

    return make
