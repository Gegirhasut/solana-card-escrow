use anchor_lang::prelude::*;
use anchor_spl::token_interface::{Mint, TokenAccount, TokenInterface};

use super::transfer_from_vault;
use crate::{
    constants::{CONFIG_SEED, HOLD_SEED, VAULT_SEED},
    errors::EscrowError,
    events::{HoldAuthorized, HoldCaptured, HoldClosed, HoldExpired, HoldReleased},
    state::{Config, Hold, HoldStatus, UserVault},
};

#[derive(Accounts)]
#[instruction(auth_id: [u8; 32])]
pub struct Authorize<'info> {
    pub operator: Signer<'info>,

    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(
        seeds = [CONFIG_SEED],
        bump = config.bump,
        has_one = operator @ EscrowError::Unauthorized,
        has_one = mint @ EscrowError::InvalidMint,
    )]
    pub config: Box<Account<'info, Config>>,

    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(mut, seeds = [VAULT_SEED, vault.owner.as_ref()], bump = vault.bump)]
    pub vault: Account<'info, UserVault>,

    #[account(
        associated_token::mint = mint,
        associated_token::authority = vault,
        associated_token::token_program = token_program,
    )]
    pub vault_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    /// `init` makes the auth_id idempotent on-chain: a second authorize with
    /// the same id fails because the account already exists.
    #[account(
        init,
        payer = payer,
        space = 8 + Hold::INIT_SPACE,
        seeds = [HOLD_SEED, vault.key().as_ref(), auth_id.as_ref()],
        bump,
    )]
    pub hold: Account<'info, Hold>,

    pub token_program: Interface<'info, TokenInterface>,
    pub system_program: Program<'info, System>,
}

pub fn authorize(ctx: Context<Authorize>, auth_id: [u8; 32], amount: u64) -> Result<()> {
    let config = &ctx.accounts.config;
    require!(!config.paused, EscrowError::Paused);
    let now = Clock::get()?.unix_timestamp;
    let balance = ctx.accounts.vault_token_account.amount;

    let vault = &mut ctx.accounts.vault;
    vault.record_authorization(amount, balance, now)?;

    let expires_ts = now
        .checked_add(config.default_hold_ttl_seconds)
        .ok_or(EscrowError::MathOverflow)?;
    let hold = &mut ctx.accounts.hold;
    hold.set_inner(Hold {
        vault: vault.key(),
        auth_id,
        amount,
        captured_amount: 0,
        status: HoldStatus::Pending,
        created_ts: now,
        expires_ts,
        rent_payer: ctx.accounts.payer.key(),
        bump: ctx.bumps.hold,
    });

    emit!(HoldAuthorized {
        vault: vault.key(),
        hold: hold.key(),
        auth_id,
        amount,
        held_total: vault.held_total,
        expires_ts,
    });
    Ok(())
}

#[derive(Accounts)]
pub struct Capture<'info> {
    pub operator: Signer<'info>,

    #[account(
        seeds = [CONFIG_SEED],
        bump = config.bump,
        has_one = operator @ EscrowError::Unauthorized,
        has_one = mint @ EscrowError::InvalidMint,
        has_one = settlement_token_account @ EscrowError::InvalidSettlementAccount,
    )]
    pub config: Box<Account<'info, Config>>,

    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(mut, seeds = [VAULT_SEED, vault.owner.as_ref()], bump = vault.bump)]
    pub vault: Account<'info, UserVault>,

    #[account(
        mut,
        seeds = [HOLD_SEED, vault.key().as_ref(), hold.auth_id.as_ref()],
        bump = hold.bump,
        has_one = vault,
    )]
    pub hold: Account<'info, Hold>,

    #[account(
        mut,
        associated_token::mint = mint,
        associated_token::authority = vault,
        associated_token::token_program = token_program,
    )]
    pub vault_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(mut, token::mint = mint, token::token_program = token_program)]
    pub settlement_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Interface<'info, TokenInterface>,
}

/// Captures `amount <= hold.amount`; any remainder is released in the same
/// step (a hold is captured at most once, like a card clearing record).
pub fn capture(ctx: Context<Capture>, amount: u64) -> Result<()> {
    require!(!ctx.accounts.config.paused, EscrowError::Paused);
    let now = Clock::get()?.unix_timestamp;

    let hold = &mut ctx.accounts.hold;
    let released = hold.capture(amount, now)?;
    let vault = &mut ctx.accounts.vault;
    vault.settle_hold(hold.amount, released, hold.created_ts)?;

    let a = &ctx.accounts;
    transfer_from_vault(
        &a.vault,
        &a.vault_token_account,
        a.settlement_token_account.to_account_info(),
        &a.mint,
        &a.token_program,
        amount,
    )?;

    emit!(HoldCaptured {
        vault: a.vault.key(),
        hold: a.hold.key(),
        auth_id: a.hold.auth_id,
        captured_amount: amount,
        released_amount: released,
        held_total: a.vault.held_total,
    });
    Ok(())
}

#[derive(Accounts)]
pub struct Release<'info> {
    pub operator: Signer<'info>,

    #[account(seeds = [CONFIG_SEED], bump = config.bump, has_one = operator @ EscrowError::Unauthorized)]
    pub config: Box<Account<'info, Config>>,

    #[account(mut, seeds = [VAULT_SEED, vault.owner.as_ref()], bump = vault.bump)]
    pub vault: Account<'info, UserVault>,

    #[account(
        mut,
        seeds = [HOLD_SEED, vault.key().as_ref(), hold.auth_id.as_ref()],
        bump = hold.bump,
        has_one = vault,
    )]
    pub hold: Account<'info, Hold>,
}

/// Reversal / post-auth decline. Allowed while paused: it only frees user funds.
pub fn release(ctx: Context<Release>) -> Result<()> {
    let hold = &mut ctx.accounts.hold;
    hold.release()?;
    let vault = &mut ctx.accounts.vault;
    vault.settle_hold(hold.amount, hold.amount, hold.created_ts)?;
    emit!(HoldReleased {
        vault: vault.key(),
        hold: hold.key(),
        auth_id: hold.auth_id,
        amount: hold.amount,
        held_total: vault.held_total,
    });
    Ok(())
}

/// Permissionless: anyone may expire a pending hold once `expires_ts` has
/// passed, so the operator cannot keep user funds locked indefinitely.
#[derive(Accounts)]
pub struct ExpireHold<'info> {
    #[account(mut, seeds = [VAULT_SEED, vault.owner.as_ref()], bump = vault.bump)]
    pub vault: Account<'info, UserVault>,

    #[account(
        mut,
        seeds = [HOLD_SEED, vault.key().as_ref(), hold.auth_id.as_ref()],
        bump = hold.bump,
        has_one = vault,
    )]
    pub hold: Account<'info, Hold>,
}

pub fn expire_hold(ctx: Context<ExpireHold>) -> Result<()> {
    let now = Clock::get()?.unix_timestamp;
    let hold = &mut ctx.accounts.hold;
    hold.expire(now)?;
    let vault = &mut ctx.accounts.vault;
    vault.settle_hold(hold.amount, hold.amount, hold.created_ts)?;
    emit!(HoldExpired {
        vault: vault.key(),
        hold: hold.key(),
        auth_id: hold.auth_id,
        amount: hold.amount,
        held_total: vault.held_total,
    });
    Ok(())
}

/// Reclaims rent of a finalized hold. Note: once closed, the same auth_id
/// could be authorized again on-chain; the backend's unique constraint on
/// auth_id remains the long-term idempotency record.
#[derive(Accounts)]
pub struct CloseHold<'info> {
    pub operator: Signer<'info>,

    #[account(seeds = [CONFIG_SEED], bump = config.bump, has_one = operator @ EscrowError::Unauthorized)]
    pub config: Box<Account<'info, Config>>,

    #[account(
        mut,
        seeds = [HOLD_SEED, hold.vault.as_ref(), hold.auth_id.as_ref()],
        bump = hold.bump,
        has_one = rent_payer,
        constraint = hold.is_final() @ EscrowError::HoldNotFinal,
        close = rent_payer,
    )]
    pub hold: Account<'info, Hold>,

    #[account(mut)]
    pub rent_payer: SystemAccount<'info>,
}

pub fn close_hold(ctx: Context<CloseHold>) -> Result<()> {
    emit!(HoldClosed {
        vault: ctx.accounts.hold.vault,
        hold: ctx.accounts.hold.key(),
        auth_id: ctx.accounts.hold.auth_id,
    });
    Ok(())
}
