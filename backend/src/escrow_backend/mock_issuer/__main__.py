"""Mock card issuer: fires realistic webhook flows at the backend.

    mock-issuer --base-url http://127.0.0.1:8000 --card card_main --limited-card card_low \
        --ledger issuer-ledger.jsonl

Behaves like an issuer processor would: signs every webhook, treats any
non-200 / timeout on an authorization as a decline (stand-in), and re-sends
clearing/reversal/refund webhooks until it receives a final 200 answer.
Everything the issuer *believes* is appended to a JSON-lines ledger, which the
reconciliation job compares against the backend DB and the chain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from escrow_backend.webhook_auth import SIGNATURE_HEADER, sign


@dataclass
class FlowResult:
    name: str
    ok: bool
    detail: str


@dataclass
class Issuer:
    base_url: str
    secret: bytes
    ledger_path: Path
    auth_timeout_s: float
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    client: httpx.AsyncClient = field(init=False)

    def __post_init__(self) -> None:
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=30)

    def ident(self, kind: str, name: str) -> str:
        return f"{self.run_id}-{name}-{kind}"

    def record(self, event: dict[str, Any]) -> None:
        with self.ledger_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    async def send(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        secret: bytes | None = None,
        request_timeout: float | None = None,
    ) -> httpx.Response:
        body = json.dumps(payload).encode()
        headers = {
            SIGNATURE_HEADER: sign(secret or self.secret, body),
            "content-type": "application/json",
        }
        timeout = httpx.USE_CLIENT_DEFAULT if request_timeout is None else request_timeout
        return await self.client.post(path, content=body, headers=headers, timeout=timeout)

    # ------------------------------------------------------------ webhooks

    async def authorize(
        self, auth_id: str, card: str, cents: int, *, record: bool = True
    ) -> dict[str, Any]:
        payload = {
            "auth_id": auth_id,
            "card_id": card,
            "amount": cents,
            "currency": "USD",
            "merchant": {"name": "Mock Coffee", "mcc": "5814", "country": "US"},
        }
        try:
            r = await self.send(
                "/webhooks/authorization", payload, request_timeout=self.auth_timeout_s
            )
            body = (
                r.json()
                if r.status_code == 200
                else {"decision": "declined", "reason": f"http_{r.status_code}"}
            )
        except httpx.TimeoutException:
            body = {"decision": "declined", "reason": "issuer_stand_in_timeout"}
        if record:
            self.record(
                {
                    "type": "authorization",
                    "auth_id": auth_id,
                    "card_id": card,
                    "amount": cents * 10_000,
                    "decision": body["decision"],
                }
            )
        return body

    async def _final(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Re-sends a webhook until the backend gives a final (200) answer."""
        for attempt in range(60):
            r = await self.send(path, payload)
            if r.status_code == 200:
                return dict(r.json())
            await asyncio.sleep(min(0.5 * (attempt + 1), 5))
        raise RuntimeError(f"no final answer for {path} {payload}")

    async def clear(self, clearing_id: str, auth_id: str, cents: int) -> dict[str, Any]:
        body = await self._final(
            "/webhooks/clearing", {"clearing_id": clearing_id, "auth_id": auth_id, "amount": cents}
        )
        self.record(
            {
                "type": "clearing",
                "auth_id": auth_id,
                "clearing_id": clearing_id,
                "amount": cents * 10_000,
                "status": body["status"],
            }
        )
        return body

    async def reverse(self, reversal_id: str, auth_id: str) -> dict[str, Any]:
        body = await self._final(
            "/webhooks/reversal", {"reversal_id": reversal_id, "auth_id": auth_id}
        )
        self.record(
            {
                "type": "reversal",
                "auth_id": auth_id,
                "reversal_id": reversal_id,
                "status": body["status"],
            }
        )
        return body

    async def refund(
        self, refund_id: str, card: str, cents: int, auth_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"refund_id": refund_id, "card_id": card, "amount": cents}
        if auth_id:
            payload["auth_id"] = auth_id
        body = await self._final("/webhooks/refund", payload)
        self.record(
            {
                "type": "refund",
                "refund_id": refund_id,
                "card_id": card,
                "amount": cents * 10_000,
                "status": body["status"],
            }
        )
        return body


Flow = Callable[[Issuer, str, str], Awaitable[FlowResult]]
FLOWS: dict[str, Flow] = {}


def flow(fn: Flow) -> Flow:
    FLOWS[fn.__name__] = fn
    return fn


def check(name: str, cond: bool, detail: str) -> FlowResult:
    return FlowResult(name, cond, detail)


@flow
async def full_capture(i: Issuer, card: str, _: str) -> FlowResult:
    a = i.ident("auth", "full")
    d = await i.authorize(a, card, 2_000)
    c = await i.clear(i.ident("clr", "full"), a, 2_000)
    return check(
        "full_capture",
        d["decision"] == "approved" and c["status"] == "succeeded",
        f"auth={d['decision']} clearing={c['status']}",
    )


@flow
async def partial_capture(i: Issuer, card: str, _: str) -> FlowResult:
    a = i.ident("auth", "partial")
    d = await i.authorize(a, card, 5_000)
    c = await i.clear(i.ident("clr", "partial"), a, 3_560)
    return check(
        "partial_capture",
        d["decision"] == "approved" and c.get("captured_amount") == 35_600_000,
        f"auth={d['decision']} captured={c.get('captured_amount')}",
    )


@flow
async def reversal(i: Issuer, card: str, _: str) -> FlowResult:
    a = i.ident("auth", "rev")
    d = await i.authorize(a, card, 1_500)
    r = await i.reverse(i.ident("rev", "rev"), a)
    return check(
        "reversal",
        d["decision"] == "approved" and r["status"] == "succeeded",
        f"auth={d['decision']} reversal={r['status']}",
    )


@flow
async def refund_after_clearing(i: Issuer, card: str, _: str) -> FlowResult:
    a = i.ident("auth", "refund")
    d = await i.authorize(a, card, 3_000)
    c = await i.clear(i.ident("clr", "refund"), a, 3_000)
    rid = i.ident("ref", "refund")
    r1, r2 = await asyncio.gather(i.refund(rid, card, 1_000, a), i.refund(rid, card, 1_000, a))
    same = json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    return check(
        "refund_after_clearing (+duplicate refund)",
        d["decision"] == "approved"
        and c["status"] == "succeeded"
        and r1["status"] == "succeeded"
        and same,
        f"refund={r1['status']} duplicate_identical={same}",
    )


@flow
async def duplicate_sequential(i: Issuer, card: str, _: str) -> FlowResult:
    a = i.ident("auth", "dupseq")
    answers = [await i.authorize(a, card, 700, record=n == 0) for n in range(3)]
    same = len({json.dumps(x, sort_keys=True) for x in answers}) == 1
    await i.reverse(i.ident("rev", "dupseq"), a)
    return check(
        "duplicate_sequential_auth",
        same and answers[0]["decision"] == "approved",
        f"3 deliveries -> identical={same} ({answers[0]['decision']})",
    )


@flow
async def duplicate_concurrent(i: Issuer, card: str, _: str) -> FlowResult:
    a = i.ident("auth", "dupconc")
    answers = await asyncio.gather(*[i.authorize(a, card, 900, record=False) for _ in range(8)])
    i.record(
        {
            "type": "authorization",
            "auth_id": a,
            "card_id": card,
            "amount": 900 * 10_000,
            "decision": answers[0]["decision"],
        }
    )
    same = len({json.dumps(x, sort_keys=True) for x in answers}) == 1
    c1, c2 = await asyncio.gather(
        i.clear(i.ident("clr", "dupconc"), a, 900), i.clear(i.ident("clr", "dupconc"), a, 900)
    )
    return check(
        "duplicate_concurrent_auth+clearing",
        same and answers[0]["decision"] == "approved" and c1 == c2 and c1["status"] == "succeeded",
        f"8 concurrent -> identical={same}; 2 concurrent clearings identical={c1 == c2}",
    )


@flow
async def insufficient_funds(i: Issuer, card: str, _: str) -> FlowResult:
    d = await i.authorize(i.ident("auth", "nsf"), card, 50_000_000)
    return check(
        "insufficient_funds_decline",
        d["decision"] == "declined" and d["reason"] == "insufficient_funds",
        f"{d['decision']} ({d.get('reason')})",
    )


@flow
async def limits(i: Issuer, _: str, limited: str) -> FlowResult:
    first = await i.authorize(i.ident("auth", "lim1"), limited, 800)
    second = await i.authorize(i.ident("auth", "lim2"), limited, 800)
    await i.reverse(i.ident("rev", "lim1"), i.ident("auth", "lim1"))
    return check(
        "daily_limit_decline",
        first["decision"] == "approved" and second.get("reason") == "daily_limit_exceeded",
        f"first={first['decision']} second={second['decision']} ({second.get('reason')})",
    )


@flow
async def expiring_hold(i: Issuer, card: str, _: str) -> FlowResult:
    d = await i.authorize(i.ident("auth", "expire"), card, 500)
    return check(
        "hold_left_to_expire",
        d["decision"] == "approved",
        "approved; never cleared — the worker expires it after the TTL",
    )


@flow
async def bad_signature(i: Issuer, card: str, _: str) -> FlowResult:
    r = await i.send(
        "/webhooks/authorization",
        {"auth_id": i.ident("auth", "badsig"), "card_id": card, "amount": 100, "currency": "USD"},
        secret=b"forged",
    )
    return check("forged_signature_rejected", r.status_code == 401, f"http {r.status_code}")


@flow
async def unknown_auth_clearing(i: Issuer, _: str, __: str) -> FlowResult:
    body = await i._final(
        "/webhooks/clearing",
        {
            "clearing_id": i.ident("clr", "ghost"),
            "auth_id": i.ident("auth", "ghost"),
            "amount": 100,
        },
    )
    return check(
        "clearing_for_unknown_auth_fails",
        body["status"] == "failed",
        f"{body['status']} ({body.get('error')})",
    )


async def run(args: argparse.Namespace) -> int:
    issuer = Issuer(args.base_url, args.secret.encode(), Path(args.ledger), args.auth_timeout)
    names = list(FLOWS) if args.flows == ["all"] else args.flows
    results: list[FlowResult] = []
    started = time.monotonic()
    try:
        # Flows touch the same vaults, so they run one after another; the
        # concurrency under test is inside individual flows.
        for name in names:
            try:
                results.append(await FLOWS[name](issuer, args.card, args.limited_card))
            except Exception as e:
                results.append(FlowResult(name, False, f"error: {e}"))
    finally:
        await issuer.client.aclose()
    width = max(len(r.name) for r in results)
    print(
        f"mock issuer run {issuer.run_id} ({time.monotonic() - started:.1f}s), "
        f"ledger: {args.ledger}"
    )
    for r in results:
        print(f"  [{'PASS' if r.ok else 'FAIL'}] {r.name:<{width}}  {r.detail}")
    failed = [r for r in results if not r.ok]
    print(f"{len(results) - len(failed)}/{len(results)} flows passed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mock-issuer")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument(
        "--secret",
        default=os.environ.get("ESCROW_WEBHOOK_SECRET", "dev-only-secret-change-me"),
        help="webhook secret (default: $ESCROW_WEBHOOK_SECRET)",
    )
    p.add_argument("--card", required=True, help="well-funded card id")
    p.add_argument("--limited-card", required=True, help="card with a low daily limit")
    p.add_argument("--ledger", default="issuer-ledger.jsonl")
    p.add_argument("--auth-timeout", type=float, default=5.0, help="issuer stand-in timeout (s)")
    p.add_argument("--flows", nargs="+", default=["all"], choices=["all", *FLOWS])
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
