//! # card_escrow
//!
//! Non-custodial escrow for crypto card top-ups. Users keep funds in a vault
//! PDA they alone can withdraw from; a card-issuer operator can place
//! authorization holds that reserve (not move) funds, and capture them to the
//! issuer's settlement account when the transaction clears.
//!
//! Reference implementation with a simulated issuer — not a live card program.
#![allow(unexpected_cfgs)]

use anchor_lang::prelude::*;

pub mod constants;
pub mod errors;
pub mod events;
pub mod instructions;
pub mod state;

use instructions::*;

declare_id!("8PyM1gDSssAqmn1qNPcwQ2y6nxFjUGwhPp81obhmAwpK");

#[program]
pub mod card_escrow {
    use super::*;

    // ---- admin ----

    pub fn initialize_config(ctx: Context<InitializeConfig>, args: InitializeConfigArgs) -> Result<()> {
        instructions::admin::initialize_config(ctx, args)
    }

    pub fn update_config(ctx: Context<UpdateConfig>, args: UpdateConfigArgs) -> Result<()> {
        instructions::admin::update_config(ctx, args)
    }

    pub fn set_paused(ctx: Context<SetPaused>, paused: bool) -> Result<()> {
        instructions::admin::set_paused(ctx, paused)
    }

    // ---- vault owner ----

    pub fn open_vault(ctx: Context<OpenVault>, limits: VaultLimits) -> Result<()> {
        instructions::vault::open_vault(ctx, limits)
    }

    pub fn deposit(ctx: Context<Deposit>, amount: u64) -> Result<()> {
        instructions::vault::deposit(ctx, amount)
    }

    pub fn withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
        instructions::vault::withdraw(ctx, amount)
    }

    pub fn set_limits(ctx: Context<SetLimits>, limits: VaultLimits) -> Result<()> {
        instructions::vault::set_limits(ctx, limits)
    }

    // ---- operator: authorization lifecycle ----

    pub fn authorize(ctx: Context<Authorize>, auth_id: [u8; 32], amount: u64) -> Result<()> {
        instructions::hold::authorize(ctx, auth_id, amount)
    }

    pub fn capture(ctx: Context<Capture>, amount: u64) -> Result<()> {
        instructions::hold::capture(ctx, amount)
    }

    pub fn release(ctx: Context<Release>) -> Result<()> {
        instructions::hold::release(ctx)
    }

    pub fn close_hold(ctx: Context<CloseHold>) -> Result<()> {
        instructions::hold::close_hold(ctx)
    }

    // ---- permissionless ----

    pub fn expire_hold(ctx: Context<ExpireHold>) -> Result<()> {
        instructions::hold::expire_hold(ctx)
    }

    // ---- settlement authority ----

    pub fn refund(ctx: Context<RefundToVault>, refund_id: [u8; 32], amount: u64) -> Result<()> {
        instructions::refund::refund(ctx, refund_id, amount)
    }
}
