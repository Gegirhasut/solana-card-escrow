pub mod admin;
pub mod hold;
pub mod refund;
pub mod vault;

pub use admin::*;
pub use hold::*;
pub use refund::*;
pub use vault::*;

use anchor_lang::prelude::*;
use anchor_spl::{
    token_2022::spl_token_2022::{
        extension::{BaseStateWithExtensions, ExtensionType, StateWithExtensions},
        state::Mint as MintState,
    },
    token_interface::{self, Mint, TokenAccount, TokenInterface, TransferChecked},
};

use crate::{constants::VAULT_SEED, errors::EscrowError, state::UserVault};

/// Rejects Token-2022 mints whose extensions would break the escrow's
/// accounting or custody assumptions (fees change transferred amounts, hooks
/// run arbitrary code, a permanent delegate can drain vaults, etc.).
pub(crate) fn assert_supported_mint(mint: &InterfaceAccount<Mint>) -> Result<()> {
    let info = mint.to_account_info();
    if *info.owner == anchor_spl::token::ID {
        return Ok(());
    }
    let data = info.try_borrow_data()?;
    let state = StateWithExtensions::<MintState>::unpack(&data)?;
    for ext in state.get_extension_types()? {
        if matches!(
            ext,
            ExtensionType::TransferFeeConfig
                | ExtensionType::TransferHook
                | ExtensionType::PermanentDelegate
                | ExtensionType::NonTransferable
                | ExtensionType::ConfidentialTransferMint
                | ExtensionType::ConfidentialTransferFeeConfig
                | ExtensionType::ConfidentialMintBurn
        ) {
            return err!(EscrowError::UnsupportedMintExtension);
        }
    }
    Ok(())
}

/// Transfers out of a vault's token account, signed by the vault PDA.
pub(crate) fn transfer_from_vault<'info>(
    vault: &Account<'info, UserVault>,
    vault_token_account: &InterfaceAccount<'info, TokenAccount>,
    to: AccountInfo<'info>,
    mint: &InterfaceAccount<'info, Mint>,
    token_program: &Interface<'info, TokenInterface>,
    amount: u64,
) -> Result<()> {
    let seeds: &[&[u8]] = &[VAULT_SEED, vault.owner.as_ref(), &[vault.bump]];
    token_interface::transfer_checked(
        CpiContext::new_with_signer(
            token_program.key(),
            TransferChecked {
                from: vault_token_account.to_account_info(),
                mint: mint.to_account_info(),
                to,
                authority: vault.to_account_info(),
            },
            &[seeds],
        ),
        amount,
        mint.decimals,
    )
}
