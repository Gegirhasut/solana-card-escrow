"""FastAPI application: issuer webhooks.

Every webhook body is authenticated with an HMAC signature before it is
parsed. Verification is fail-closed: on any error the request is rejected
with 401, which the issuer treats as a decline for authorizations.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TypeVar

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from solders.pubkey import Pubkey

from escrow_backend.api.schemas import (
    AuthorizationWebhook,
    ClearingWebhook,
    RefundWebhook,
    ReversalWebhook,
)
from escrow_backend.db import make_engine, make_sessionmaker
from escrow_backend.logging import configure_logging
from escrow_backend.models import OpKind
from escrow_backend.services.authorizations import AuthorizationService, AuthRequest
from escrow_backend.services.notify import Notifier
from escrow_backend.services.operations import OperationService, OpRequest
from escrow_backend.services.worker import Worker
from escrow_backend.settings import Settings, get_settings, load_keypair
from escrow_backend.solana.fake import InMemoryChain
from escrow_backend.solana.gateway import ChainGateway, RpcGateway
from escrow_backend.solana.program import EscrowProgram
from escrow_backend.webhook_auth import SIGNATURE_HEADER, SignatureError, verify

log = structlog.get_logger(__name__)
M = TypeVar("M", bound=BaseModel)


@dataclass
class Services:
    settings: Settings
    chain: ChainGateway
    auths: AuthorizationService
    ops: OperationService
    worker: Worker
    units_per_minor: int


def build_chain(settings: Settings) -> ChainGateway:
    program = EscrowProgram(
        program_id=Pubkey.from_string(settings.program_id),
        mint=Pubkey.from_string(settings.mint) if settings.mint else Pubkey.default(),
        token_program=Pubkey.from_string(settings.token_program),
    )
    if settings.chain == "memory":
        return InMemoryChain(program=program)
    sa = (
        load_keypair(settings.settlement_authority_keypair)
        if settings.settlement_authority_keypair
        and settings.settlement_authority_keypair.expanduser().exists()
        else None
    )
    return RpcGateway(
        settings.rpc_url,
        program,
        load_keypair(settings.operator_keypair),
        sa,
        confirm_timeout_s=settings.confirm_timeout_s,
        compute_unit_price=settings.compute_unit_price,
        compute_unit_limit=settings.compute_unit_limit,
    )


def create_app(
    settings: Settings | None = None,
    *,
    chain: ChainGateway | None = None,
    run_worker: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        sm = make_sessionmaker(engine)
        redis = Redis.from_url(settings.redis_url)
        gw = chain or build_chain(settings)
        ops = OperationService(sm, gw)
        app.state.services = Services(
            settings=settings,
            chain=gw,
            auths=AuthorizationService(
                sm, gw, Notifier(redis), settings.auth_timeout_budget_ms, settings.currency
            ),
            ops=ops,
            worker=Worker(sm, gw, ops),
            units_per_minor=10 ** (settings.mint_decimals - settings.currency_exponent),
        )
        task = (
            asyncio.create_task(app.state.services.worker.run_forever(settings.worker_interval_s))
            if run_worker
            else None
        )
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await gw.close()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="card-escrow issuer integration", version="0.1.0", lifespan=lifespan)

    async def authenticated(request: Request, model: type[M]) -> M:
        svc: Services = request.app.state.services
        body = await request.body()
        try:
            verify(
                svc.settings.webhook_secret.get_secret_value().encode(),
                body,
                request.headers.get(SIGNATURE_HEADER),
                svc.settings.webhook_tolerance_s,
            )
        except SignatureError as e:
            log.warning("webhook.rejected", path=request.url.path, reason=str(e))
            raise HTTPException(status_code=401, detail="invalid signature") from e
        try:
            return model.model_validate_json(body)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail="invalid payload") from e

    def json_response(body: str, status: int = 200) -> Response:
        return Response(content=body, status_code=status, media_type="application/json")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/webhooks/authorization")
    async def authorization(request: Request) -> Response:
        p = await authenticated(request, AuthorizationWebhook)
        svc: Services = request.app.state.services
        body = await svc.auths.handle(
            AuthRequest(
                auth_id=p.auth_id,
                card_id=p.card_id,
                amount=p.amount * svc.units_per_minor,
                currency=p.currency,
                merchant=p.merchant,
            )
        )
        return json_response(body)

    async def operation(request: Request, op: OpRequest) -> Response:
        svc: Services = request.app.state.services
        result = await svc.ops.handle(op)
        return json_response(result.body, result.status_code)

    @app.post("/webhooks/clearing")
    async def clearing(request: Request) -> Response:
        p = await authenticated(request, ClearingWebhook)
        units = request.app.state.services.units_per_minor
        return await operation(
            request,
            OpRequest(
                OpKind.CLEARING, p.clearing_id, p.auth_id, None, p.amount * units, p.model_dump()
            ),
        )

    @app.post("/webhooks/reversal")
    async def reversal(request: Request) -> Response:
        p = await authenticated(request, ReversalWebhook)
        return await operation(
            request,
            OpRequest(OpKind.REVERSAL, p.reversal_id, p.auth_id, None, None, p.model_dump()),
        )

    @app.post("/webhooks/refund")
    async def refund(request: Request) -> Response:
        p = await authenticated(request, RefundWebhook)
        units = request.app.state.services.units_per_minor
        return await operation(
            request,
            OpRequest(
                OpKind.REFUND, p.refund_id, p.auth_id, p.card_id, p.amount * units, p.model_dump()
            ),
        )

    return app
