"""Operational commands: `escrow-backend <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import uvicorn
from solders.pubkey import Pubkey
from sqlalchemy.dialects.postgresql import insert

from escrow_backend.api.app import build_chain, create_app
from escrow_backend.db import make_engine, make_sessionmaker, transaction
from escrow_backend.logging import configure_logging
from escrow_backend.models import Card, CardStatus
from escrow_backend.reconcile import reconcile
from escrow_backend.services.operations import OperationService
from escrow_backend.services.worker import Worker
from escrow_backend.settings import get_settings


async def _register_card(card_id: str, owner: str, status: str) -> None:
    Pubkey.from_string(owner)  # reject a malformed owner before it reaches the DB
    s = get_settings()
    engine = make_engine(s.database_url)
    try:
        async with transaction(make_sessionmaker(engine)) as session:
            await session.execute(
                insert(Card)
                .values(card_id=card_id, owner_pubkey=owner, status=status)
                .on_conflict_do_update(
                    index_elements=["card_id"], set_={"owner_pubkey": owner, "status": status}
                )
            )
    finally:
        await engine.dispose()


async def _reconcile(ledger: Path | None, out: Path | None) -> int:
    s = get_settings()
    engine = make_engine(s.database_url)
    chain = build_chain(s)
    try:
        report = await reconcile(make_sessionmaker(engine), chain, ledger)
    finally:
        await chain.close()
        await engine.dispose()
    print(report.render())
    if out:
        await asyncio.to_thread(out.write_text, json.dumps(report.to_json(), indent=2))
    return 0 if report.ok else 1


async def _worker(once: bool) -> None:
    s = get_settings()
    configure_logging()
    engine = make_engine(s.database_url)
    sm = make_sessionmaker(engine)
    chain = build_chain(s)
    worker = Worker(sm, chain, OperationService(sm, chain))
    try:
        if once:
            print(json.dumps(await worker.run_once()))
        else:
            await worker.run_forever(s.worker_interval_s)
    finally:
        await chain.close()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["bootstrap"]:
        from escrow_backend import bootstrap

        return bootstrap.main(argv[1:])
    if argv[:1] == ["mock-issuer"]:
        from escrow_backend.mock_issuer.__main__ import main as mock_main

        return mock_main(argv[1:])

    p = argparse.ArgumentParser(prog="escrow-backend")
    sub_help = "commands: serve, worker, register-card, reconcile, bootstrap, mock-issuer"
    p.description = sub_help
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the webhook API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-worker", action="store_true")

    card = sub.add_parser("register-card", help="map an issuer card id to a vault owner")
    card.add_argument("card_id")
    card.add_argument("owner_pubkey")
    card.add_argument("--status", default=CardStatus.ACTIVE, choices=[c.value for c in CardStatus])

    rec = sub.add_parser("reconcile", help="compare chain, DB and issuer ledger")
    rec.add_argument("--ledger", type=Path)
    rec.add_argument("--out", type=Path)

    wk = sub.add_parser("worker", help="run background jobs")
    wk.add_argument("--once", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        app = create_app(run_worker=not args.no_worker)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0
    if args.cmd == "register-card":
        asyncio.run(_register_card(args.card_id, args.owner_pubkey, args.status))
        return 0
    if args.cmd == "reconcile":
        return asyncio.run(_reconcile(args.ledger, args.out))
    if args.cmd == "worker":
        asyncio.run(_worker(args.once))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
