use anchor_lang::prelude::*;
use anchor_spl::{
    associated_token::AssociatedToken,
    token_interface::{self, Mint, TokenAccount, TokenInterface, TransferChecked},
};

use super::transfer_from_vault;
use crate::{
    constants::{CONFIG_SEED, VAULT_SEED},
    errors::EscrowError,
    events::{Deposited, LimitsUpdated, VaultOpened, Withdrawn},
    state::{validate_velocity_window, Config, UserVault},
};

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Debug)]
pub struct VaultLimits {
    pub daily_limit: u64,
    pub velocity_max_auths: u32,
    pub velocity_window_seconds: i64,
}

#[derive(Accounts)]
pub struct OpenVault<'info> {
    #[account(mut)]
    pub owner: Signer<'info>,

    #[account(seeds = [CONFIG_SEED], bump = config.bump, has_one = mint @ EscrowError::InvalidMint)]
    pub config: Box<Account<'info, Config>>,

    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(
        init,
        payer = owner,
        space = 8 + UserVault::INIT_SPACE,
        seeds = [VAULT_SEED, owner.key().as_ref()],
        bump,
    )]
    pub vault: Account<'info, UserVault>,

    /// `init_if_needed`: the ATA address is public, so anyone could create it
    /// first; a plain `init` would let them block the vault from ever opening.
    #[account(
        init_if_needed,
        payer = owner,
        associated_token::mint = mint,
        associated_token::authority = vault,
        associated_token::token_program = token_program,
    )]
    pub vault_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Interface<'info, TokenInterface>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

pub fn open_vault(ctx: Context<OpenVault>, limits: VaultLimits) -> Result<()> {
    validate_velocity_window(limits.velocity_window_seconds)?;
    let now = Clock::get()?.unix_timestamp;
    let vault = &mut ctx.accounts.vault;
    vault.set_inner(UserVault {
        owner: ctx.accounts.owner.key(),
        held_total: 0,
        daily_limit: limits.daily_limit,
        daily_spent: 0,
        day_start_ts: now,
        velocity_max_auths: limits.velocity_max_auths,
        velocity_window_seconds: limits.velocity_window_seconds,
        window_start_ts: now,
        window_count: 0,
        bump: ctx.bumps.vault,
    });
    emit!(VaultOpened {
        vault: vault.key(),
        owner: vault.owner,
        daily_limit: vault.daily_limit,
        velocity_max_auths: vault.velocity_max_auths,
        velocity_window_seconds: vault.velocity_window_seconds,
    });
    Ok(())
}

#[derive(Accounts)]
pub struct Deposit<'info> {
    pub owner: Signer<'info>,

    #[account(seeds = [CONFIG_SEED], bump = config.bump, has_one = mint @ EscrowError::InvalidMint)]
    pub config: Box<Account<'info, Config>>,

    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(
        seeds = [VAULT_SEED, owner.key().as_ref()],
        bump = vault.bump,
        has_one = owner @ EscrowError::Unauthorized,
    )]
    pub vault: Account<'info, UserVault>,

    #[account(
        mut,
        associated_token::mint = mint,
        associated_token::authority = vault,
        associated_token::token_program = token_program,
    )]
    pub vault_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(
        mut,
        token::mint = mint,
        token::authority = owner,
        token::token_program = token_program,
    )]
    pub owner_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Interface<'info, TokenInterface>,
}

pub fn deposit(ctx: Context<Deposit>, amount: u64) -> Result<()> {
    require!(!ctx.accounts.config.paused, EscrowError::Paused);
    require!(amount > 0, EscrowError::ZeroAmount);
    let a = &ctx.accounts;
    token_interface::transfer_checked(
        CpiContext::new(
            a.token_program.key(),
            TransferChecked {
                from: a.owner_token_account.to_account_info(),
                mint: a.mint.to_account_info(),
                to: a.vault_token_account.to_account_info(),
                authority: a.owner.to_account_info(),
            },
        ),
        amount,
        a.mint.decimals,
    )?;
    ctx.accounts.vault_token_account.reload()?;
    emit!(Deposited {
        vault: ctx.accounts.vault.key(),
        amount,
        balance: ctx.accounts.vault_token_account.amount,
    });
    Ok(())
}

/// Deliberately has no `paused` check and no operator involvement: the owner
/// can always take back every token that is not reserved by a pending hold.
#[derive(Accounts)]
pub struct Withdraw<'info> {
    pub owner: Signer<'info>,

    #[account(seeds = [CONFIG_SEED], bump = config.bump, has_one = mint @ EscrowError::InvalidMint)]
    pub config: Box<Account<'info, Config>>,

    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(
        seeds = [VAULT_SEED, owner.key().as_ref()],
        bump = vault.bump,
        has_one = owner @ EscrowError::Unauthorized,
    )]
    pub vault: Account<'info, UserVault>,

    #[account(
        mut,
        associated_token::mint = mint,
        associated_token::authority = vault,
        associated_token::token_program = token_program,
    )]
    pub vault_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    /// Any token account of the configured mint chosen by the owner.
    #[account(mut, token::mint = mint, token::token_program = token_program)]
    pub destination: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Interface<'info, TokenInterface>,
}

pub fn withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
    require!(amount > 0, EscrowError::ZeroAmount);
    let a = &ctx.accounts;
    let available = a.vault.available(a.vault_token_account.amount)?;
    require!(
        amount <= available,
        EscrowError::InsufficientAvailableBalance
    );
    transfer_from_vault(
        &a.vault,
        &a.vault_token_account,
        a.destination.to_account_info(),
        &a.mint,
        &a.token_program,
        amount,
    )?;
    ctx.accounts.vault_token_account.reload()?;
    emit!(Withdrawn {
        vault: ctx.accounts.vault.key(),
        destination: ctx.accounts.destination.key(),
        amount,
        balance: ctx.accounts.vault_token_account.amount,
    });
    Ok(())
}

/// Owner-only: the operator can never relax (or tighten) a user's limits.
#[derive(Accounts)]
pub struct SetLimits<'info> {
    pub owner: Signer<'info>,

    #[account(
        mut,
        seeds = [VAULT_SEED, owner.key().as_ref()],
        bump = vault.bump,
        has_one = owner @ EscrowError::Unauthorized,
    )]
    pub vault: Account<'info, UserVault>,
}

pub fn set_limits(ctx: Context<SetLimits>, limits: VaultLimits) -> Result<()> {
    validate_velocity_window(limits.velocity_window_seconds)?;
    let vault = &mut ctx.accounts.vault;
    vault.daily_limit = limits.daily_limit;
    vault.velocity_max_auths = limits.velocity_max_auths;
    vault.velocity_window_seconds = limits.velocity_window_seconds;
    emit!(LimitsUpdated {
        vault: vault.key(),
        daily_limit: vault.daily_limit,
        velocity_max_auths: vault.velocity_max_auths,
        velocity_window_seconds: vault.velocity_window_seconds,
    });
    Ok(())
}
