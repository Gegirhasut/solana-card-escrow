use anchor_lang::prelude::*;
use anchor_spl::token_interface::{Mint, TokenAccount, TokenInterface};

use super::assert_supported_mint;
use crate::{
    constants::CONFIG_SEED,
    errors::EscrowError,
    events::{ConfigInitialized, ConfigUpdated, PausedSet},
    program::CardEscrow,
    state::{validate_hold_ttl, Config},
};

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug)]
pub struct InitializeConfigArgs {
    pub operator: Pubkey,
    pub settlement_authority: Pubkey,
    pub default_hold_ttl_seconds: i64,
}

#[derive(Accounts)]
pub struct InitializeConfig<'info> {
    #[account(mut)]
    pub admin: Signer<'info>,

    #[account(
        init,
        payer = admin,
        space = 8 + Config::INIT_SPACE,
        seeds = [CONFIG_SEED],
        bump,
    )]
    pub config: Box<Account<'info, Config>>,

    #[account(mint::token_program = token_program)]
    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(token::mint = mint, token::token_program = token_program)]
    pub settlement_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    /// Gating initialization on the upgrade authority prevents anyone from
    /// front-running the deploy and installing themselves as admin.
    #[account(
        constraint = program.programdata_address()? == Some(program_data.key())
            @ EscrowError::InvalidProgramData
    )]
    pub program: Program<'info, CardEscrow>,

    #[account(
        constraint = program_data.upgrade_authority_address == Some(admin.key())
            @ EscrowError::NotUpgradeAuthority
    )]
    pub program_data: Account<'info, ProgramData>,

    pub token_program: Interface<'info, TokenInterface>,
    pub system_program: Program<'info, System>,
}

pub fn initialize_config(ctx: Context<InitializeConfig>, args: InitializeConfigArgs) -> Result<()> {
    validate_hold_ttl(args.default_hold_ttl_seconds)?;
    assert_supported_mint(&ctx.accounts.mint)?;
    require_keys_eq!(
        ctx.accounts.settlement_token_account.owner,
        args.settlement_authority,
        EscrowError::SettlementOwnerMismatch
    );

    let config = &mut ctx.accounts.config;
    config.set_inner(Config {
        admin: ctx.accounts.admin.key(),
        operator: args.operator,
        mint: ctx.accounts.mint.key(),
        settlement_token_account: ctx.accounts.settlement_token_account.key(),
        settlement_authority: args.settlement_authority,
        paused: false,
        default_hold_ttl_seconds: args.default_hold_ttl_seconds,
        bump: ctx.bumps.config,
    });

    emit!(ConfigInitialized {
        admin: config.admin,
        operator: config.operator,
        mint: config.mint,
        settlement_token_account: config.settlement_token_account,
        settlement_authority: config.settlement_authority,
        default_hold_ttl_seconds: config.default_hold_ttl_seconds,
    });
    Ok(())
}

/// `None` leaves a field unchanged. The mint is intentionally not updatable:
/// existing vault token accounts are bound to it.
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug, Default)]
pub struct UpdateConfigArgs {
    pub admin: Option<Pubkey>,
    pub operator: Option<Pubkey>,
    pub settlement_authority: Option<Pubkey>,
    pub default_hold_ttl_seconds: Option<i64>,
}

#[derive(Accounts)]
pub struct UpdateConfig<'info> {
    pub admin: Signer<'info>,

    #[account(
        mut,
        seeds = [CONFIG_SEED],
        bump = config.bump,
        has_one = admin @ EscrowError::Unauthorized,
        has_one = mint @ EscrowError::InvalidMint,
    )]
    pub config: Box<Account<'info, Config>>,

    pub mint: Box<InterfaceAccount<'info, Mint>>,

    /// The settlement account to use from now on (may be the current one).
    /// Always passed so its owner can be checked against the settlement authority.
    #[account(token::mint = mint, token::token_program = token_program)]
    pub settlement_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Interface<'info, TokenInterface>,
}

pub fn update_config(ctx: Context<UpdateConfig>, args: UpdateConfigArgs) -> Result<()> {
    let config = &mut ctx.accounts.config;
    if let Some(admin) = args.admin {
        config.admin = admin;
    }
    if let Some(operator) = args.operator {
        config.operator = operator;
    }
    if let Some(settlement_authority) = args.settlement_authority {
        config.settlement_authority = settlement_authority;
    }
    if let Some(ttl) = args.default_hold_ttl_seconds {
        validate_hold_ttl(ttl)?;
        config.default_hold_ttl_seconds = ttl;
    }
    require_keys_eq!(
        ctx.accounts.settlement_token_account.owner,
        config.settlement_authority,
        EscrowError::SettlementOwnerMismatch
    );
    config.settlement_token_account = ctx.accounts.settlement_token_account.key();

    emit!(ConfigUpdated {
        admin: config.admin,
        operator: config.operator,
        settlement_token_account: config.settlement_token_account,
        settlement_authority: config.settlement_authority,
        default_hold_ttl_seconds: config.default_hold_ttl_seconds,
    });
    Ok(())
}

#[derive(Accounts)]
pub struct SetPaused<'info> {
    pub admin: Signer<'info>,

    #[account(
        mut,
        seeds = [CONFIG_SEED],
        bump = config.bump,
        has_one = admin @ EscrowError::Unauthorized,
    )]
    pub config: Box<Account<'info, Config>>,
}

pub fn set_paused(ctx: Context<SetPaused>, paused: bool) -> Result<()> {
    ctx.accounts.config.paused = paused;
    emit!(PausedSet {
        paused,
        ts: Clock::get()?.unix_timestamp,
    });
    Ok(())
}
