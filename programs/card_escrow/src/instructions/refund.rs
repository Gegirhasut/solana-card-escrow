use anchor_lang::prelude::*;
use anchor_spl::token_interface::{self, Mint, TokenAccount, TokenInterface, TransferChecked};

use crate::{
    constants::{CONFIG_SEED, REFUND_SEED, VAULT_SEED},
    errors::EscrowError,
    events::Refunded,
    state::{Config, Refund, UserVault},
};

#[derive(Accounts)]
#[instruction(refund_id: [u8; 32])]
pub struct RefundToVault<'info> {
    pub settlement_authority: Signer<'info>,

    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(
        seeds = [CONFIG_SEED],
        bump = config.bump,
        has_one = settlement_authority @ EscrowError::Unauthorized,
        has_one = settlement_token_account @ EscrowError::InvalidSettlementAccount,
        has_one = mint @ EscrowError::InvalidMint,
    )]
    pub config: Box<Account<'info, Config>>,

    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(seeds = [VAULT_SEED, vault.owner.as_ref()], bump = vault.bump)]
    pub vault: Account<'info, UserVault>,

    #[account(
        mut,
        associated_token::mint = mint,
        associated_token::authority = vault,
        associated_token::token_program = token_program,
    )]
    pub vault_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    #[account(mut, token::mint = mint, token::token_program = token_program)]
    pub settlement_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    /// `init` makes refund_id idempotent: replaying it fails at account creation.
    #[account(
        init,
        payer = payer,
        space = 8 + Refund::INIT_SPACE,
        seeds = [REFUND_SEED, vault.key().as_ref(), refund_id.as_ref()],
        bump,
    )]
    pub refund: Account<'info, Refund>,

    pub token_program: Interface<'info, TokenInterface>,
    pub system_program: Program<'info, System>,
}

/// Moves funds from the issuer's settlement account back into a vault.
/// Allowed while paused: it can only increase user balances.
pub fn refund(ctx: Context<RefundToVault>, refund_id: [u8; 32], amount: u64) -> Result<()> {
    require!(amount > 0, EscrowError::ZeroAmount);
    let a = &ctx.accounts;
    token_interface::transfer_checked(
        CpiContext::new(
            a.token_program.key(),
            TransferChecked {
                from: a.settlement_token_account.to_account_info(),
                mint: a.mint.to_account_info(),
                to: a.vault_token_account.to_account_info(),
                authority: a.settlement_authority.to_account_info(),
            },
        ),
        amount,
        a.mint.decimals,
    )?;

    let ts = Clock::get()?.unix_timestamp;
    let vault = ctx.accounts.vault.key();
    let refund = &mut ctx.accounts.refund;
    refund.set_inner(Refund {
        vault,
        refund_id,
        amount,
        ts,
        bump: ctx.bumps.refund,
    });
    emit!(Refunded {
        vault,
        refund: refund.key(),
        refund_id,
        amount,
    });
    Ok(())
}
