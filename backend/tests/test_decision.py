from dataclasses import replace

from solders.pubkey import Pubkey

from escrow_backend.decision import (
    SECONDS_PER_DAY,
    DeclineReason,
    VaultSnapshot,
    config_paused,
    evaluate,
)
from escrow_backend.solana.program import Config, UserVault

T0 = 1_760_000_000
VAULT = UserVault(
    owner=Pubkey.default(),
    held_total=200,
    daily_limit=1_000,
    daily_spent=900,
    day_start_ts=T0,
    velocity_max_auths=3,
    velocity_window_seconds=60,
    window_start_ts=T0,
    window_count=3,
    bump=255,
)


def snap(**kw: object) -> VaultSnapshot:
    base: dict[str, object] = {
        "slot": 1,
        "chain_ts": T0 + 10,
        "paused": False,
        "vault": VAULT,
        "balance": 1_000,
    }
    base.update(kw)
    return VaultSnapshot(**base)  # type: ignore[arg-type]


def test_declines_in_priority_order() -> None:
    assert evaluate(snap(paused=True), 1).reason == DeclineReason.PAUSED
    assert evaluate(snap(vault=None), 1).reason == DeclineReason.VAULT_NOT_FOUND
    assert evaluate(snap(), 0).reason == DeclineReason.INVALID_AMOUNT
    assert evaluate(snap(), 801).reason == DeclineReason.INSUFFICIENT_FUNDS
    assert evaluate(snap(), 101).reason == DeclineReason.DAILY_LIMIT
    assert evaluate(snap(), 100).reason == DeclineReason.VELOCITY_LIMIT


def test_windows_roll_like_the_program() -> None:
    v = replace(VAULT, window_count=0)
    assert evaluate(snap(vault=v), 100).approved
    assert evaluate(snap(vault=v), 101).reason == DeclineReason.DAILY_LIMIT
    # Daily window rolls at exactly 24h.
    assert (
        evaluate(snap(vault=v, chain_ts=T0 + SECONDS_PER_DAY - 1), 101).reason
        == DeclineReason.DAILY_LIMIT
    )
    assert evaluate(snap(vault=v, chain_ts=T0 + SECONDS_PER_DAY), 800).approved
    # Velocity window rolls at exactly velocity_window_seconds.
    assert evaluate(snap(chain_ts=T0 + 59), 1).reason == DeclineReason.VELOCITY_LIMIT
    assert evaluate(snap(chain_ts=T0 + 60), 1).approved


def test_snapshot_json() -> None:
    j = snap().to_json()
    assert j["vault"]["owner"] == str(Pubkey.default())
    assert snap(vault=None).to_json()["vault"] is None


def test_config_paused() -> None:
    assert config_paused(None)
    cfg = Config(
        Pubkey.default(),
        Pubkey.default(),
        Pubkey.default(),
        Pubkey.default(),
        Pubkey.default(),
        False,
        60,
        1,
    )
    assert not config_paused(cfg)
