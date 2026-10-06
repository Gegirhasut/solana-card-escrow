use anchor_lang::prelude::*;

#[error_code]
pub enum EscrowError {
    #[msg("Signer is not authorized for this instruction")]
    Unauthorized,
    #[msg("Protocol is paused")]
    Paused,
    #[msg("Amount must be greater than zero")]
    ZeroAmount,
    #[msg("Amount exceeds the vault's available (unheld) balance")]
    InsufficientAvailableBalance,
    #[msg("Authorization would exceed the vault's daily limit")]
    DailyLimitExceeded,
    #[msg("Authorization would exceed the vault's velocity limit")]
    VelocityLimitExceeded,
    #[msg("Hold is not in Pending status")]
    HoldNotPending,
    #[msg("Capture amount exceeds the held amount")]
    CaptureExceedsHold,
    #[msg("Hold has expired")]
    HoldExpired,
    #[msg("Hold has not expired yet")]
    HoldNotYetExpired,
    #[msg("Hold is not in a final status")]
    HoldNotFinal,
    #[msg("Arithmetic overflow or underflow")]
    MathOverflow,
    #[msg("Hold TTL is outside the allowed range")]
    InvalidHoldTtl,
    #[msg("Velocity window is outside the allowed range")]
    InvalidVelocityWindow,
    #[msg("Mint uses a Token-2022 extension that is not supported")]
    UnsupportedMintExtension,
    #[msg("Settlement token account must be owned by the settlement authority")]
    SettlementOwnerMismatch,
    #[msg("Token account is not the configured settlement account")]
    InvalidSettlementAccount,
    #[msg("Mint does not match the configured mint")]
    InvalidMint,
    #[msg("Program data account does not belong to this program")]
    InvalidProgramData,
    #[msg("Only the program upgrade authority can initialize the config")]
    NotUpgradeAuthority,
    #[msg("Held total exceeds the vault token balance")]
    HeldExceedsBalance,
}
