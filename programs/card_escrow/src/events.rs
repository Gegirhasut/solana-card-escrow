//! Every state change emits exactly one event so off-chain indexers can
//! rebuild state from logs alone.
use anchor_lang::prelude::*;

#[event]
pub struct ConfigInitialized {
    pub admin: Pubkey,
    pub operator: Pubkey,
    pub mint: Pubkey,
    pub settlement_token_account: Pubkey,
    pub settlement_authority: Pubkey,
    pub default_hold_ttl_seconds: i64,
}

#[event]
pub struct ConfigUpdated {
    pub admin: Pubkey,
    pub operator: Pubkey,
    pub settlement_token_account: Pubkey,
    pub settlement_authority: Pubkey,
    pub default_hold_ttl_seconds: i64,
}

#[event]
pub struct PausedSet {
    pub paused: bool,
    pub ts: i64,
}

#[event]
pub struct VaultOpened {
    pub vault: Pubkey,
    pub owner: Pubkey,
    pub daily_limit: u64,
    pub velocity_max_auths: u32,
    pub velocity_window_seconds: i64,
}

#[event]
pub struct Deposited {
    pub vault: Pubkey,
    pub amount: u64,
    pub balance: u64,
}

#[event]
pub struct Withdrawn {
    pub vault: Pubkey,
    pub destination: Pubkey,
    pub amount: u64,
    pub balance: u64,
}

#[event]
pub struct LimitsUpdated {
    pub vault: Pubkey,
    pub daily_limit: u64,
    pub velocity_max_auths: u32,
    pub velocity_window_seconds: i64,
}

#[event]
pub struct HoldAuthorized {
    pub vault: Pubkey,
    pub hold: Pubkey,
    pub auth_id: [u8; 32],
    pub amount: u64,
    pub held_total: u64,
    pub expires_ts: i64,
}

#[event]
pub struct HoldCaptured {
    pub vault: Pubkey,
    pub hold: Pubkey,
    pub auth_id: [u8; 32],
    pub captured_amount: u64,
    pub released_amount: u64,
    pub held_total: u64,
}

#[event]
pub struct HoldReleased {
    pub vault: Pubkey,
    pub hold: Pubkey,
    pub auth_id: [u8; 32],
    pub amount: u64,
    pub held_total: u64,
}

#[event]
pub struct HoldExpired {
    pub vault: Pubkey,
    pub hold: Pubkey,
    pub auth_id: [u8; 32],
    pub amount: u64,
    pub held_total: u64,
}

#[event]
pub struct HoldClosed {
    pub vault: Pubkey,
    pub hold: Pubkey,
    pub auth_id: [u8; 32],
}

#[event]
pub struct Refunded {
    pub vault: Pubkey,
    pub refund: Pubkey,
    pub refund_id: [u8; 32],
    pub amount: u64,
}
