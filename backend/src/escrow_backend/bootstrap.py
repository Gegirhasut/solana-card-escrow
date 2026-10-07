"""Bootstraps a deployment for demos: test mint, config, funded demo vaults, cards.

    escrow-backend bootstrap --cluster devnet --rpc-url https://api.devnet.solana.com

All keypairs are read from / written to ~/.config/solana/card-escrow (outside the
repository). On mainnet use `--mint` with the real USDC mint; the script then
never mints tokens and skips the demo users unless `--users` is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.models import TxOpts
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountParams, TransferParams, create_account, transfer
from solders.transaction import Transaction

from escrow_backend.solana.program import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    EscrowProgram,
    ata,
)

KEYS = Path(os.environ.get("CARD_ESCROW_KEYS", "~/.config/solana/card-escrow")).expanduser()
DECIMALS = 6
UNIT = 10**DECIMALS
MINT_SIZE = 82
RPC_ATTEMPTS = 8
T = TypeVar("T")


@dataclass(frozen=True)
class DemoUser:
    name: str
    deposit: int
    daily_limit: int
    velocity_max_auths: int
    velocity_window: int


DEMO_USERS = [
    DemoUser("alice", 1_000 * UNIT, 500 * UNIT, 30, 3_600),
    DemoUser("bob", 50 * UNIT, 10 * UNIT, 5, 3_600),
]


def load_or_create(path: Path) -> Keypair:
    if path.exists():
        return Keypair.from_bytes(bytes(json.loads(path.read_text())))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    kp = Keypair()
    # Created 0600 from the start: never world-readable, even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(list(bytes(kp))))
    return kp


# ------------------------------------------------------- SPL token helpers


def ix_initialize_mint2(mint: Pubkey, authority: Pubkey) -> Instruction:
    data = bytes([20, DECIMALS]) + bytes(authority) + b"\x00"  # no freeze authority
    return Instruction(TOKEN_PROGRAM_ID, data, [AccountMeta(mint, False, True)])


def ix_create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM_ID,
        bytes([1]),
        [
            AccountMeta(payer, True, True),
            AccountMeta(ata(owner, mint), False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(TOKEN_PROGRAM_ID, False, False),
        ],
    )


def ix_mint_to_checked(mint: Pubkey, dest: Pubkey, authority: Pubkey, amount: int) -> Instruction:
    data = bytes([14]) + struct.pack("<QB", amount, DECIMALS)
    return Instruction(
        TOKEN_PROGRAM_ID,
        data,
        [
            AccountMeta(mint, False, True),
            AccountMeta(dest, False, True),
            AccountMeta(authority, True, False),
        ],
    )


class Bootstrapper:
    def __init__(self, client: AsyncClient, deployer: Keypair) -> None:
        self.client = client
        self.deployer = deployer

    async def rpc(self, call: Callable[[], Awaitable[T]]) -> T:
        """Retries transport errors (public RPCs answer 429 under load)."""
        for attempt in range(RPC_ATTEMPTS):
            try:
                return await call()
            except SolanaRpcException:
                if attempt == RPC_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(min(2**attempt, 15))
        raise AssertionError("unreachable")

    async def send(self, ixs: list[Instruction], signers: list[Keypair]) -> str:
        bh = (await self.rpc(lambda: self.client.get_latest_blockhash(Confirmed))).value.blockhash
        tx = Transaction.new_signed_with_payer(ixs, signers[0].pubkey(), signers, bh)
        # Resending the same signed tx is safe: the cluster deduplicates by signature.
        sig = (
            await self.rpc(
                lambda: self.client.send_transaction(
                    tx, opts=TxOpts(preflight_commitment=Confirmed)
                )
            )
        ).value
        await self.rpc(lambda: self.client.confirm_transaction(sig, Confirmed, sleep_seconds=2))
        return str(sig)

    async def exists(self, pk: Pubkey) -> bool:
        return (
            await self.rpc(lambda: self.client.get_account_info(pk, Confirmed))
        ).value is not None

    async def ensure_sol(self, pk: Pubkey, lamports: int) -> None:
        bal = (await self.rpc(lambda: self.client.get_balance(pk, Confirmed))).value
        if bal < lamports:
            await self.send(
                [
                    transfer(
                        TransferParams(
                            from_pubkey=self.deployer.pubkey(),
                            to_pubkey=pk,
                            lamports=lamports - bal,
                        )
                    )
                ],
                [self.deployer],
            )

    async def create_mint(self, mint: Keypair) -> None:
        if await self.exists(mint.pubkey()):
            return
        rent = (
            await self.rpc(lambda: self.client.get_minimum_balance_for_rent_exemption(MINT_SIZE))
        ).value
        await self.send(
            [
                create_account(
                    CreateAccountParams(
                        from_pubkey=self.deployer.pubkey(),
                        to_pubkey=mint.pubkey(),
                        lamports=rent,
                        space=MINT_SIZE,
                        owner=TOKEN_PROGRAM_ID,
                    )
                ),
                ix_initialize_mint2(mint.pubkey(), self.deployer.pubkey()),
            ],
            [self.deployer, mint],
        )


async def bootstrap(args: argparse.Namespace) -> dict[str, str]:
    deployer = load_or_create(KEYS / "deployer.json")
    operator = load_or_create(KEYS / "operator.json")
    settlement = load_or_create(KEYS / "settlement-authority.json")
    cluster_dir = KEYS / args.cluster
    client = AsyncClient(args.rpc_url, commitment=Confirmed, timeout=30)
    b = Bootstrapper(client, deployer)
    try:
        test_mint = args.mint is None
        if test_mint:
            mint_kp = load_or_create(cluster_dir / "test-mint.json")
            await b.create_mint(mint_kp)
            mint = mint_kp.pubkey()
        else:
            mint = Pubkey.from_string(args.mint)
        program = EscrowProgram(Pubkey.from_string(args.program_id), mint)
        print(f"mint: {mint} ({'test mint' if test_mint else 'external'})")

        await b.ensure_sol(operator.pubkey(), int(args.operator_sol * 1e9))
        settlement_ata = ata(settlement.pubkey(), mint)
        await b.send(
            [ix_create_ata_idempotent(deployer.pubkey(), settlement.pubkey(), mint)], [deployer]
        )
        if test_mint:
            await b.send(
                [ix_mint_to_checked(mint, settlement_ata, deployer.pubkey(), 1_000_000 * UNIT)],
                [deployer],
            )

        if not await b.exists(program.pdas.config()):
            sig = await b.send(
                [
                    program.initialize_config(
                        deployer.pubkey(),
                        settlement_ata,
                        operator.pubkey(),
                        settlement.pubkey(),
                        args.ttl,
                    )
                ],
                [deployer],
            )
            print(f"config initialized: {program.pdas.config()} ({sig})")
        else:
            print(f"config exists: {program.pdas.config()}")

        cards: dict[str, str] = {}
        users = DEMO_USERS if (test_mint if args.users is None else args.users) else []
        for u in users:
            kp = load_or_create(cluster_dir / "users" / f"{u.name}.json")
            owner = kp.pubkey()
            await b.ensure_sol(owner, int(0.02 * 1e9))
            wallet = ata(owner, mint)
            ixs = [ix_create_ata_idempotent(deployer.pubkey(), owner, mint)]
            if test_mint:
                ixs.append(ix_mint_to_checked(mint, wallet, deployer.pubkey(), u.deposit))
            await b.send(ixs, [deployer])
            if not await b.exists(program.pdas.vault(owner)):
                await b.send(
                    [
                        program.open_vault(
                            owner, u.daily_limit, u.velocity_max_auths, u.velocity_window
                        ),
                        program.deposit(owner, wallet, u.deposit),
                    ],
                    [kp],
                )
            cards[f"card_{u.name}"] = str(owner)
            print(f"user {u.name}: owner={owner} vault={program.pdas.vault(owner)}")
        return {"ESCROW_MINT": str(mint), **{f"CARD:{k}": v for k, v in cards.items()}}
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="escrow-backend bootstrap")
    p.add_argument("--cluster", default="localnet")
    p.add_argument("--rpc-url", default="http://127.0.0.1:8899")
    p.add_argument("--program-id", default="8PyM1gDSssAqmn1qNPcwQ2y6nxFjUGwhPp81obhmAwpK")
    p.add_argument("--mint", help="existing mint (e.g. USDC); omit to create a test mint")
    p.add_argument("--ttl", type=int, default=7 * 86_400, help="default hold TTL (s)")
    p.add_argument("--operator-sol", type=float, default=0.5)
    p.add_argument(
        "--users",
        action=argparse.BooleanOptionalAction,
        help="create demo users (default: only with a test mint)",
    )
    p.add_argument("--out", type=Path, help="write resulting values as JSON")
    args = p.parse_args(argv)
    result = asyncio.run(bootstrap(args))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0
