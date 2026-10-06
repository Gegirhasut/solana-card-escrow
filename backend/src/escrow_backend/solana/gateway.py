"""Chain access behind a small async interface.

`RpcGateway` talks to a real cluster; `InMemoryChain` (fake.py) implements the
same interface with the program's rules for fast, deterministic tests.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Protocol

import structlog
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import RPCException
from solana.rpc.models import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

from escrow_backend.decision import VaultSnapshot
from escrow_backend.solana.program import (
    SYSVAR_CLOCK_ID,
    Config,
    EscrowProgram,
    Hold,
    UserVault,
    decode_clock_unix_timestamp,
    decode_token_amount,
    error_name,
)

log = structlog.get_logger(__name__)

ACCOUNT_ALREADY_IN_USE = "AccountAlreadyInUse"


class ChainError(Exception):
    """A transaction or RPC failure.

    `name` is the program error name when the program rejected the instruction,
    `ACCOUNT_ALREADY_IN_USE` for a duplicate PDA `init`, otherwise None.
    `transient` failures (RPC down, blockhash expired, timeout) may be retried.
    """

    def __init__(self, message: str, *, name: str | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.name = name
        self.transient = transient


@dataclass(frozen=True)
class TxResult:
    signature: str
    slot: int | None = None


class ChainGateway(Protocol):
    program: EscrowProgram

    async def snapshot(self, owner: Pubkey) -> VaultSnapshot: ...
    async def config(self) -> Config | None: ...
    async def authorize(self, owner: Pubkey, auth_id: bytes, amount: int) -> TxResult: ...
    async def capture(self, owner: Pubkey, auth_id: bytes, amount: int) -> TxResult: ...
    async def release(self, owner: Pubkey, auth_id: bytes) -> TxResult: ...
    async def expire(self, owner: Pubkey, auth_id: bytes) -> TxResult: ...
    async def refund(self, owner: Pubkey, refund_id: bytes, amount: int) -> TxResult: ...
    async def get_holds(self, holds: list[Pubkey]) -> dict[Pubkey, Hold | None]: ...
    async def accounts_exist(self, keys: list[Pubkey]) -> dict[Pubkey, bool]: ...
    async def close(self) -> None: ...


_ERR_NUMBER = re.compile(r"Error Number: (\d+)")
_CUSTOM_HEX = re.compile(r"custom program error: 0x([0-9a-fA-F]+)")
_CUSTOM_DEC = re.compile(r"Custom\((\d+)\)")


def parse_program_error(text: str) -> str | None:
    """Extracts the escrow error name (or a system error) from logs/errors."""
    if m := _ERR_NUMBER.search(text):
        return error_name(int(m.group(1)))
    if "already in use" in text:
        return ACCOUNT_ALREADY_IN_USE
    if m := _CUSTOM_HEX.search(text):
        code = int(m.group(1), 16)
        return ACCOUNT_ALREADY_IN_USE if code == 0 else error_name(code)
    if m := _CUSTOM_DEC.search(text):
        code = int(m.group(1))
        return ACCOUNT_ALREADY_IN_USE if code == 0 else error_name(code)
    return None


class RpcGateway:
    def __init__(
        self,
        rpc_url: str,
        program: EscrowProgram,
        operator: Keypair,
        settlement_authority: Keypair | None,
        *,
        confirm_timeout_s: float = 30.0,
        compute_unit_price: int = 0,
        compute_unit_limit: int = 0,
    ) -> None:
        self.client = AsyncClient(rpc_url, commitment=Confirmed, timeout=10)
        self.program = program
        self.operator = operator
        self.settlement_authority = settlement_authority
        self.confirm_timeout_s = confirm_timeout_s
        self.compute_unit_price = compute_unit_price
        self.compute_unit_limit = compute_unit_limit
        self._blockhash: tuple[Hash, float] | None = None
        self._settlement_ata: Pubkey | None = None

    async def close(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------- reads

    async def config(self) -> Config | None:
        resp = await self.client.get_account_info(self.program.pdas.config(), Confirmed)
        return Config.decode(bytes(resp.value.data)) if resp.value else None

    async def snapshot(self, owner: Pubkey) -> VaultSnapshot:
        keys = [
            self.program.pdas.config(),
            self.program.pdas.vault(owner),
            self.program.vault_ata(owner),
            SYSVAR_CLOCK_ID,
        ]
        try:
            resp = await self.client.get_multiple_accounts(keys, Confirmed)
        except Exception as e:  # network / RPC errors
            raise ChainError(f"snapshot failed: {e}", transient=True) from e
        cfg_acc, vault_acc, ata_acc, clock_acc = resp.value
        config = Config.decode(bytes(cfg_acc.data)) if cfg_acc else None
        vault = UserVault.decode(bytes(vault_acc.data)) if vault_acc else None
        balance = decode_token_amount(bytes(ata_acc.data)) if ata_acc else 0
        chain_ts = (
            decode_clock_unix_timestamp(bytes(clock_acc.data)) if clock_acc else int(time.time())
        )
        return VaultSnapshot(
            slot=resp.context.slot,
            chain_ts=chain_ts,
            paused=True if config is None else config.paused,
            vault=vault,
            balance=balance,
        )

    async def get_holds(self, holds: list[Pubkey]) -> dict[Pubkey, Hold | None]:
        out: dict[Pubkey, Hold | None] = {}
        for i in range(0, len(holds), 100):
            chunk = holds[i : i + 100]
            resp = await self.client.get_multiple_accounts(chunk, Confirmed)
            for pk, acc in zip(chunk, resp.value, strict=True):
                out[pk] = Hold.decode(bytes(acc.data)) if acc else None
        return out

    async def accounts_exist(self, keys: list[Pubkey]) -> dict[Pubkey, bool]:
        out: dict[Pubkey, bool] = {}
        for i in range(0, len(keys), 100):
            chunk = keys[i : i + 100]
            resp = await self.client.get_multiple_accounts(chunk, Confirmed)
            for pk, acc in zip(chunk, resp.value, strict=True):
                out[pk] = acc is not None
        return out

    # ------------------------------------------------------------- writes

    async def _settlement(self) -> Pubkey:
        if self._settlement_ata is None:
            cfg = await self.config()
            if cfg is None:
                raise ChainError("config not initialized")
            self._settlement_ata = cfg.settlement_token_account
        return self._settlement_ata

    async def authorize(self, owner: Pubkey, auth_id: bytes, amount: int) -> TxResult:
        op = self.operator.pubkey()
        ix = self.program.authorize(op, op, owner, auth_id, amount)
        return await self._send([ix], [self.operator])

    async def capture(self, owner: Pubkey, auth_id: bytes, amount: int) -> TxResult:
        ix = self.program.capture(
            self.operator.pubkey(), owner, auth_id, await self._settlement(), amount
        )
        return await self._send([ix], [self.operator])

    async def release(self, owner: Pubkey, auth_id: bytes) -> TxResult:
        ix = self.program.release(self.operator.pubkey(), owner, auth_id)
        return await self._send([ix], [self.operator])

    async def expire(self, owner: Pubkey, auth_id: bytes) -> TxResult:
        return await self._send([self.program.expire_hold(owner, auth_id)], [self.operator])

    async def refund(self, owner: Pubkey, refund_id: bytes, amount: int) -> TxResult:
        if self.settlement_authority is None:
            raise ChainError("settlement authority key not configured")
        sa = self.settlement_authority.pubkey()
        ix = self.program.refund(
            sa, self.operator.pubkey(), owner, await self._settlement(), refund_id, amount
        )
        return await self._send([ix], [self.operator, self.settlement_authority])

    async def _recent_blockhash(self) -> Hash:
        # Blockhashes are valid for ~60s; reuse one for a few seconds to save a round trip.
        now = time.monotonic()
        if self._blockhash is None or now - self._blockhash[1] > 5:
            resp = await self.client.get_latest_blockhash(Confirmed)
            self._blockhash = (resp.value.blockhash, now)
        return self._blockhash[0]

    async def _send(self, ixs: list[Instruction], signers: list[Keypair]) -> TxResult:
        budget: list[Instruction] = []
        if self.compute_unit_limit:
            budget.append(set_compute_unit_limit(self.compute_unit_limit))
        if self.compute_unit_price:
            budget.append(set_compute_unit_price(self.compute_unit_price))
        try:
            blockhash = await self._recent_blockhash()
            tx = Transaction.new_signed_with_payer(
                budget + ixs, signers[0].pubkey(), signers, blockhash
            )
            resp = await self.client.send_transaction(
                tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
            )
        except RPCException as e:
            text = str(e)
            name = parse_program_error(text)
            if "Blockhash not found" in text:
                self._blockhash = None
                raise ChainError(text, transient=True) from e
            raise ChainError(text, name=name, transient=name is None) from e
        except Exception as e:
            raise ChainError(f"send failed: {e}", transient=True) from e
        sig = resp.value
        slot = await self._confirm(sig)
        return TxResult(str(sig), slot)

    async def _confirm(self, sig: Signature) -> int:
        deadline = time.monotonic() + self.confirm_timeout_s
        while time.monotonic() < deadline:
            resp = await self.client.get_signature_statuses([sig])
            status = resp.value[0]
            if status is not None:
                if status.err is not None:
                    name = parse_program_error(str(status.err))
                    raise ChainError(f"transaction failed: {status.err}", name=name)
                if status.confirmation_status is not None and str(
                    status.confirmation_status
                ).lower().endswith(("confirmed", "finalized")):
                    return int(status.slot)
            await asyncio.sleep(0.15)
        raise ChainError(f"confirmation timeout for {sig}", transient=True)
