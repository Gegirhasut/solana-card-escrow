//! PDA seeds and protocol-wide bounds.

pub const CONFIG_SEED: &[u8] = b"config";
pub const VAULT_SEED: &[u8] = b"vault";
pub const HOLD_SEED: &[u8] = b"hold";
pub const REFUND_SEED: &[u8] = b"refund";

pub const SECONDS_PER_DAY: i64 = 86_400;

/// Holds shorter than a minute would race normal clearing latency.
pub const MIN_HOLD_TTL_SECONDS: i64 = 60;
/// Card networks keep authorizations open for up to ~30 days.
pub const MAX_HOLD_TTL_SECONDS: i64 = 31 * SECONDS_PER_DAY;

/// The velocity window may not exceed one day.
pub const MAX_VELOCITY_WINDOW_SECONDS: i64 = SECONDS_PER_DAY;
