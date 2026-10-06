"""Hand-written client for the card_escrow Anchor program.

Kept dependency-free (no anchorpy): instruction data is the 8-byte Anchor
discriminator followed by Borsh-encoded args; account order mirrors the
`#[derive(Accounts)]` structs in programs/card_escrow/src/instructions.
"""

from __future__ import annotations

import enum
import hashlib
import struct
from dataclasses import dataclass

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
BPF_LOADER_UPGRADEABLE_ID = Pubkey.from_string("BPFLoaderUpgradeab1e11111111111111111111111")
SYSVAR_CLOCK_ID = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")

CONFIG_SEED = b"config"
VAULT_SEED = b"vault"
HOLD_SEED = b"hold"
REFUND_SEED = b"refund"

# Custom error codes start at 6000 in declaration order of `EscrowError`.
ESCROW_ERRORS = [
    "Unauthorized",
    "Paused",
    "ZeroAmount",
    "InsufficientAvailableBalance",
    "DailyLimitExceeded",
    "VelocityLimitExceeded",
    "HoldNotPending",
    "CaptureExceedsHold",
    "HoldExpired",
    "HoldNotYetExpired",
    "HoldNotFinal",
    "MathOverflow",
    "InvalidHoldTtl",
    "InvalidVelocityWindow",
    "UnsupportedMintExtension",
    "SettlementOwnerMismatch",
    "InvalidSettlementAccount",
    "InvalidMint",
    "InvalidProgramData",
    "NotUpgradeAuthority",
    "HeldExceedsBalance",
]


def error_name(code: int) -> str | None:
    idx = code - 6000
    return ESCROW_ERRORS[idx] if 0 <= idx < len(ESCROW_ERRORS) else None


def _disc(namespace: str, name: str) -> bytes:
    return hashlib.sha256(f"{namespace}:{name}".encode()).digest()[:8]


def ix_disc(name: str) -> bytes:
    return _disc("global", name)


def account_disc(name: str) -> bytes:
    return _disc("account", name)


# --------------------------------------------------------------------- PDAs


@dataclass(frozen=True)
class Pdas:
    program_id: Pubkey

    def config(self) -> Pubkey:
        return Pubkey.find_program_address([CONFIG_SEED], self.program_id)[0]

    def vault(self, owner: Pubkey) -> Pubkey:
        return Pubkey.find_program_address([VAULT_SEED, bytes(owner)], self.program_id)[0]

    def hold(self, vault: Pubkey, auth_id: bytes) -> Pubkey:
        return Pubkey.find_program_address([HOLD_SEED, bytes(vault), auth_id], self.program_id)[0]

    def refund(self, vault: Pubkey, refund_id: bytes) -> Pubkey:
        return Pubkey.find_program_address([REFUND_SEED, bytes(vault), refund_id], self.program_id)[
            0
        ]

    def program_data(self) -> Pubkey:
        return Pubkey.find_program_address([bytes(self.program_id)], BPF_LOADER_UPGRADEABLE_ID)[0]


def ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM_ID) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)], ASSOCIATED_TOKEN_PROGRAM_ID
    )[0]


# ----------------------------------------------------------------- accounts


class HoldStatus(enum.IntEnum):
    PENDING = 0
    CAPTURED = 1
    RELEASED = 2
    EXPIRED = 3


class _Reader:
    def __init__(self, data: bytes, disc: bytes) -> None:
        if data[:8] != disc:
            raise ValueError("account discriminator mismatch")
        self.data = data
        self.off = 8

    def take(self, fmt: str) -> int:
        (v,) = struct.unpack_from("<" + fmt, self.data, self.off)
        self.off += struct.calcsize(fmt)
        return int(v)

    def pubkey(self) -> Pubkey:
        pk = Pubkey.from_bytes(self.data[self.off : self.off + 32])
        self.off += 32
        return pk

    def raw(self, n: int) -> bytes:
        b = self.data[self.off : self.off + n]
        self.off += n
        return bytes(b)


@dataclass(frozen=True)
class Config:
    admin: Pubkey
    operator: Pubkey
    mint: Pubkey
    settlement_token_account: Pubkey
    settlement_authority: Pubkey
    paused: bool
    default_hold_ttl_seconds: int
    bump: int

    @classmethod
    def decode(cls, data: bytes) -> Config:
        r = _Reader(data, account_disc("Config"))
        return cls(
            admin=r.pubkey(),
            operator=r.pubkey(),
            mint=r.pubkey(),
            settlement_token_account=r.pubkey(),
            settlement_authority=r.pubkey(),
            paused=bool(r.take("B")),
            default_hold_ttl_seconds=r.take("q"),
            bump=r.take("B"),
        )


@dataclass(frozen=True)
class UserVault:
    owner: Pubkey
    held_total: int
    daily_limit: int
    daily_spent: int
    day_start_ts: int
    velocity_max_auths: int
    velocity_window_seconds: int
    window_start_ts: int
    window_count: int
    bump: int

    @classmethod
    def decode(cls, data: bytes) -> UserVault:
        r = _Reader(data, account_disc("UserVault"))
        return cls(
            owner=r.pubkey(),
            held_total=r.take("Q"),
            daily_limit=r.take("Q"),
            daily_spent=r.take("Q"),
            day_start_ts=r.take("q"),
            velocity_max_auths=r.take("I"),
            velocity_window_seconds=r.take("q"),
            window_start_ts=r.take("q"),
            window_count=r.take("I"),
            bump=r.take("B"),
        )


@dataclass(frozen=True)
class Hold:
    vault: Pubkey
    auth_id: bytes
    amount: int
    captured_amount: int
    status: HoldStatus
    created_ts: int
    expires_ts: int
    rent_payer: Pubkey
    bump: int

    @classmethod
    def decode(cls, data: bytes) -> Hold:
        r = _Reader(data, account_disc("Hold"))
        return cls(
            vault=r.pubkey(),
            auth_id=r.raw(32),
            amount=r.take("Q"),
            captured_amount=r.take("Q"),
            status=HoldStatus(r.take("B")),
            created_ts=r.take("q"),
            expires_ts=r.take("q"),
            rent_payer=r.pubkey(),
            bump=r.take("B"),
        )


def decode_token_amount(data: bytes) -> int:
    """SPL Token / Token-2022 account: amount is a u64 at offset 64."""
    (amount,) = struct.unpack_from("<Q", data, 64)
    return int(amount)


def decode_clock_unix_timestamp(data: bytes) -> int:
    """Clock sysvar: slot u64, epoch_start_timestamp i64, epoch u64,
    leader_schedule_epoch u64, unix_timestamp i64."""
    (ts,) = struct.unpack_from("<q", data, 32)
    return int(ts)


# ------------------------------------------------------------- instructions


def _opt_pubkey(pk: Pubkey | None) -> bytes:
    return b"\x00" if pk is None else b"\x01" + bytes(pk)


def _opt_i64(v: int | None) -> bytes:
    return b"\x00" if v is None else b"\x01" + struct.pack("<q", v)


def _limits(daily_limit: int, velocity_max_auths: int, velocity_window_seconds: int) -> bytes:
    return struct.pack("<QIq", daily_limit, velocity_max_auths, velocity_window_seconds)


def _m(pk: Pubkey, signer: bool = False, writable: bool = False) -> AccountMeta:
    return AccountMeta(pubkey=pk, is_signer=signer, is_writable=writable)


@dataclass(frozen=True)
class EscrowProgram:
    """Instruction builders bound to one deployment (program id + mint)."""

    program_id: Pubkey
    mint: Pubkey
    token_program: Pubkey = TOKEN_PROGRAM_ID

    @property
    def pdas(self) -> Pdas:
        return Pdas(self.program_id)

    def vault_ata(self, owner: Pubkey) -> Pubkey:
        return ata(self.pdas.vault(owner), self.mint, self.token_program)

    def _ix(self, name: str, args: bytes, metas: list[AccountMeta]) -> Instruction:
        return Instruction(self.program_id, ix_disc(name) + args, metas)

    # admin
    def initialize_config(
        self,
        admin: Pubkey,
        settlement_token_account: Pubkey,
        operator: Pubkey,
        settlement_authority: Pubkey,
        ttl: int,
    ) -> Instruction:
        args = bytes(operator) + bytes(settlement_authority) + struct.pack("<q", ttl)
        return self._ix(
            "initialize_config",
            args,
            [
                _m(admin, True, True),
                _m(self.pdas.config(), writable=True),
                _m(self.mint),
                _m(settlement_token_account),
                _m(self.program_id),
                _m(self.pdas.program_data()),
                _m(self.token_program),
                _m(SYSTEM_PROGRAM_ID),
            ],
        )

    def set_paused(self, admin: Pubkey, paused: bool) -> Instruction:
        return self._ix(
            "set_paused",
            bytes([int(paused)]),
            [_m(admin, True), _m(self.pdas.config(), writable=True)],
        )

    def update_config(
        self,
        admin: Pubkey,
        settlement_token_account: Pubkey,
        *,
        new_admin: Pubkey | None = None,
        operator: Pubkey | None = None,
        settlement_authority: Pubkey | None = None,
        ttl: int | None = None,
    ) -> Instruction:
        args = (
            _opt_pubkey(new_admin)
            + _opt_pubkey(operator)
            + _opt_pubkey(settlement_authority)
            + _opt_i64(ttl)
        )
        return self._ix(
            "update_config",
            args,
            [
                _m(admin, True),
                _m(self.pdas.config(), writable=True),
                _m(self.mint),
                _m(settlement_token_account),
                _m(self.token_program),
            ],
        )

    # vault owner
    def open_vault(
        self, owner: Pubkey, daily_limit: int, velocity_max_auths: int, velocity_window: int
    ) -> Instruction:
        vault = self.pdas.vault(owner)
        return self._ix(
            "open_vault",
            _limits(daily_limit, velocity_max_auths, velocity_window),
            [
                _m(owner, True, True),
                _m(self.pdas.config()),
                _m(self.mint),
                _m(vault, writable=True),
                _m(self.vault_ata(owner), writable=True),
                _m(self.token_program),
                _m(ASSOCIATED_TOKEN_PROGRAM_ID),
                _m(SYSTEM_PROGRAM_ID),
            ],
        )

    def deposit(self, owner: Pubkey, owner_token_account: Pubkey, amount: int) -> Instruction:
        return self._ix(
            "deposit",
            struct.pack("<Q", amount),
            [
                _m(owner, True),
                _m(self.pdas.config()),
                _m(self.mint),
                _m(self.pdas.vault(owner)),
                _m(self.vault_ata(owner), writable=True),
                _m(owner_token_account, writable=True),
                _m(self.token_program),
            ],
        )

    def withdraw(self, owner: Pubkey, destination: Pubkey, amount: int) -> Instruction:
        return self._ix(
            "withdraw",
            struct.pack("<Q", amount),
            [
                _m(owner, True),
                _m(self.pdas.config()),
                _m(self.mint),
                _m(self.pdas.vault(owner)),
                _m(self.vault_ata(owner), writable=True),
                _m(destination, writable=True),
                _m(self.token_program),
            ],
        )

    def set_limits(
        self, owner: Pubkey, daily_limit: int, velocity_max_auths: int, velocity_window: int
    ) -> Instruction:
        return self._ix(
            "set_limits",
            _limits(daily_limit, velocity_max_auths, velocity_window),
            [_m(owner, True), _m(self.pdas.vault(owner), writable=True)],
        )

    # operator
    def authorize(
        self, operator: Pubkey, payer: Pubkey, owner: Pubkey, auth_id: bytes, amount: int
    ) -> Instruction:
        vault = self.pdas.vault(owner)
        return self._ix(
            "authorize",
            auth_id + struct.pack("<Q", amount),
            [
                _m(operator, True),
                _m(payer, True, True),
                _m(self.pdas.config()),
                _m(self.mint),
                _m(vault, writable=True),
                _m(self.vault_ata(owner)),
                _m(self.pdas.hold(vault, auth_id), writable=True),
                _m(self.token_program),
                _m(SYSTEM_PROGRAM_ID),
            ],
        )

    def capture(
        self,
        operator: Pubkey,
        owner: Pubkey,
        auth_id: bytes,
        settlement_token_account: Pubkey,
        amount: int,
    ) -> Instruction:
        vault = self.pdas.vault(owner)
        return self._ix(
            "capture",
            struct.pack("<Q", amount),
            [
                _m(operator, True),
                _m(self.pdas.config()),
                _m(self.mint),
                _m(vault, writable=True),
                _m(self.pdas.hold(vault, auth_id), writable=True),
                _m(self.vault_ata(owner), writable=True),
                _m(settlement_token_account, writable=True),
                _m(self.token_program),
            ],
        )

    def release(self, operator: Pubkey, owner: Pubkey, auth_id: bytes) -> Instruction:
        vault = self.pdas.vault(owner)
        return self._ix(
            "release",
            b"",
            [
                _m(operator, True),
                _m(self.pdas.config()),
                _m(vault, writable=True),
                _m(self.pdas.hold(vault, auth_id), writable=True),
            ],
        )

    def expire_hold(self, owner: Pubkey, auth_id: bytes) -> Instruction:
        vault = self.pdas.vault(owner)
        return self._ix(
            "expire_hold",
            b"",
            [_m(vault, writable=True), _m(self.pdas.hold(vault, auth_id), writable=True)],
        )

    def close_hold(self, operator: Pubkey, hold: Pubkey, rent_payer: Pubkey) -> Instruction:
        return self._ix(
            "close_hold",
            b"",
            [
                _m(operator, True),
                _m(self.pdas.config()),
                _m(hold, writable=True),
                _m(rent_payer, writable=True),
            ],
        )

    # settlement authority
    def refund(
        self,
        settlement_authority: Pubkey,
        payer: Pubkey,
        owner: Pubkey,
        settlement_token_account: Pubkey,
        refund_id: bytes,
        amount: int,
    ) -> Instruction:
        vault = self.pdas.vault(owner)
        return self._ix(
            "refund",
            refund_id + struct.pack("<Q", amount),
            [
                _m(settlement_authority, True),
                _m(payer, True, True),
                _m(self.pdas.config()),
                _m(self.mint),
                _m(vault),
                _m(self.vault_ata(owner), writable=True),
                _m(settlement_token_account, writable=True),
                _m(self.pdas.refund(vault, refund_id), writable=True),
                _m(self.token_program),
                _m(SYSTEM_PROGRAM_ID),
            ],
        )
