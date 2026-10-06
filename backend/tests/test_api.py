"""HTTP layer: signature enforcement, payload validation, unit conversion."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from escrow_backend.api.app import create_app
from escrow_backend.db import make_sessionmaker, transaction
from escrow_backend.models import Card
from escrow_backend.settings import Settings
from escrow_backend.solana.fake import InMemoryChain
from escrow_backend.solana.program import EscrowProgram
from escrow_backend.webhook_auth import SIGNATURE_HEADER, sign

from .conftest import CARD, OWNER, USD

SECRET = b"test-secret"


@pytest.fixture
async def client(
    engine: AsyncEngine, program: EscrowProgram, test_settings: Callable[..., Settings]
) -> AsyncIterator[tuple[httpx.AsyncClient, InMemoryChain]]:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE authorizations, issuer_operations, cards"))
    async with transaction(make_sessionmaker(engine)) as s:
        s.add(Card(card_id=CARD, owner_pubkey=str(OWNER), status="active"))
    chain = InMemoryChain(program=program, settlement_balance=10**12)
    chain.open_vault(OWNER, 1_000 * USD)
    app = create_app(test_settings(auth_timeout_budget_ms=10_000), chain=chain, run_worker=False)
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c, chain


async def post(
    c: httpx.AsyncClient, path: str, payload: Any, secret: bytes = SECRET
) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await c.post(path, content=body, headers={SIGNATURE_HEADER: sign(secret, body)})


AUTH = {
    "auth_id": "auth-1",
    "card_id": CARD,
    "amount": 1250,
    "currency": "USD",
    "merchant": {"mcc": "5411"},
}


async def test_healthz(client: tuple[httpx.AsyncClient, InMemoryChain]) -> None:
    c, _ = client
    assert (await c.get("/healthz")).json() == {"ok": True}


async def test_authorization_converts_cents_to_base_units(
    client: tuple[httpx.AsyncClient, InMemoryChain],
) -> None:
    c, chain = client
    r = await post(c, "/webhooks/authorization", AUTH)
    assert r.status_code == 200 and r.json()["decision"] == "approved"
    assert chain.vaults[OWNER].state.held_total == 12_500_000  # $12.50 at 6 decimals


async def test_full_flow_over_http(client: tuple[httpx.AsyncClient, InMemoryChain]) -> None:
    c, chain = client
    assert (await post(c, "/webhooks/authorization", AUTH)).json()["decision"] == "approved"
    r = await post(
        c, "/webhooks/clearing", {"clearing_id": "clr-1", "auth_id": "auth-1", "amount": 1000}
    )
    assert r.json()["status"] == "succeeded"
    r = await post(
        c,
        "/webhooks/refund",
        {"refund_id": "ref-1", "card_id": CARD, "amount": 300, "auth_id": "auth-1"},
    )
    assert r.json()["status"] == "succeeded"
    auth2 = {**AUTH, "auth_id": "auth-2"}
    assert (await post(c, "/webhooks/authorization", auth2)).json()["decision"] == "approved"
    r = await post(c, "/webhooks/reversal", {"reversal_id": "rev-1", "auth_id": "auth-2"})
    assert r.json()["status"] == "succeeded"
    assert chain.vaults[OWNER].balance == 1_000 * USD - 10 * USD + 3 * USD
    assert chain.vaults[OWNER].state.held_total == 0


async def test_duplicate_http_responses_are_byte_identical(
    client: tuple[httpx.AsyncClient, InMemoryChain],
) -> None:
    c, _ = client
    responses = await asyncio.gather(*[post(c, "/webhooks/authorization", AUTH) for _ in range(5)])
    assert len({r.content for r in responses}) == 1


@pytest.mark.parametrize(
    "path",
    ["/webhooks/authorization", "/webhooks/clearing", "/webhooks/reversal", "/webhooks/refund"],
)
async def test_every_webhook_requires_valid_signature(
    client: tuple[httpx.AsyncClient, InMemoryChain], path: str
) -> None:
    c, chain = client
    body = json.dumps(AUTH).encode()
    for headers in ({}, {SIGNATURE_HEADER: "t=1,v1=00"}, {SIGNATURE_HEADER: sign(b"wrong", body)}):
        r = await c.post(path, content=body, headers=headers)
        assert r.status_code == 401
    # Valid signature over a different body.
    r = await c.post(path, content=body + b" ", headers={SIGNATURE_HEADER: sign(SECRET, body)})
    assert r.status_code == 401
    assert chain.sent == []


@pytest.mark.parametrize(
    "payload",
    [
        {**AUTH, "amount": -1},
        {**AUTH, "auth_id": ""},
        {**AUTH, "auth_id": "has space"},
        {**AUTH, "unexpected": 1},
        {k: v for k, v in AUTH.items() if k != "card_id"},
        "not an object",
    ],
)
async def test_invalid_payloads_rejected(
    client: tuple[httpx.AsyncClient, InMemoryChain], payload: Any
) -> None:
    c, chain = client
    r = await post(c, "/webhooks/authorization", payload)
    assert r.status_code == 400
    assert chain.sent == []
