//! Account layouts and the pure money logic that operates on them.
//!
//! All balance/limit arithmetic lives here, free of account plumbing, so it can
//! be unit-tested natively in addition to the end-to-end LiteSVM tests.
use anchor_lang::prelude::*;

use crate::{
    constants::{
        MAX_HOLD_TTL_SECONDS, MAX_VELOCITY_WINDOW_SECONDS, MIN_HOLD_TTL_SECONDS, SECONDS_PER_DAY,
    },
    errors::EscrowError,
};

#[account]
#[derive(InitSpace, Debug)]
pub struct Config {
    /// Can update config and pause. Cannot move user funds.
    pub admin: Pubkey,
    /// Signs authorize / capture / release / close_hold.
    pub operator: Pubkey,
    /// The only mint this deployment accepts. Immutable after initialization.
    pub mint: Pubkey,
    /// Issuer's settlement token account: destination of captures, source of refunds.
    pub settlement_token_account: Pubkey,
    /// Owner of `settlement_token_account`; signs refunds.
    pub settlement_authority: Pubkey,
    /// Blocks new authorizations and captures. Never blocks withdrawals.
    pub paused: bool,
    pub default_hold_ttl_seconds: i64,
    pub bump: u8,
}

pub fn validate_hold_ttl(ttl: i64) -> Result<()> {
    require!(
        (MIN_HOLD_TTL_SECONDS..=MAX_HOLD_TTL_SECONDS).contains(&ttl),
        EscrowError::InvalidHoldTtl
    );
    Ok(())
}

pub fn validate_velocity_window(window: i64) -> Result<()> {
    require!(
        (1..=MAX_VELOCITY_WINDOW_SECONDS).contains(&window),
        EscrowError::InvalidVelocityWindow
    );
    Ok(())
}

#[account]
#[derive(InitSpace, Debug)]
pub struct UserVault {
    pub owner: Pubkey,
    /// Sum of `amount` over all Pending holds of this vault.
    pub held_total: u64,
    /// Max sum of authorizations per 24h window. 0 disables authorizations ("card frozen").
    pub daily_limit: u64,
    pub daily_spent: u64,
    pub day_start_ts: i64,
    /// Max authorizations per velocity window. 0 disables authorizations.
    pub velocity_max_auths: u32,
    pub velocity_window_seconds: i64,
    pub window_start_ts: i64,
    pub window_count: u32,
    pub bump: u8,
}

impl UserVault {
    /// Token balance not reserved by pending holds.
    pub fn available(&self, balance: u64) -> Result<u64> {
        balance
            .checked_sub(self.held_total)
            .ok_or_else(|| error!(EscrowError::HeldExceedsBalance))
    }

    /// Starts a new daily / velocity window when the current one has elapsed.
    pub fn roll_windows(&mut self, now: i64) -> Result<()> {
        let since_day = now
            .checked_sub(self.day_start_ts)
            .ok_or(EscrowError::MathOverflow)?;
        if since_day >= SECONDS_PER_DAY {
            self.day_start_ts = now;
            self.daily_spent = 0;
        }
        let since_window = now
            .checked_sub(self.window_start_ts)
            .ok_or(EscrowError::MathOverflow)?;
        if since_window >= self.velocity_window_seconds {
            self.window_start_ts = now;
            self.window_count = 0;
        }
        Ok(())
    }

    /// Validates a new authorization against balance and limits and reserves it.
    /// State is only mutated once every check has passed.
    pub fn record_authorization(&mut self, amount: u64, balance: u64, now: i64) -> Result<()> {
        require!(amount > 0, EscrowError::ZeroAmount);
        require!(
            amount <= self.available(balance)?,
            EscrowError::InsufficientAvailableBalance
        );
        self.roll_windows(now)?;

        let daily_spent = self
            .daily_spent
            .checked_add(amount)
            .ok_or(EscrowError::MathOverflow)?;
        require!(
            daily_spent <= self.daily_limit,
            EscrowError::DailyLimitExceeded
        );
        let window_count = self
            .window_count
            .checked_add(1)
            .ok_or(EscrowError::MathOverflow)?;
        require!(
            window_count <= self.velocity_max_auths,
            EscrowError::VelocityLimitExceeded
        );
        let held_total = self
            .held_total
            .checked_add(amount)
            .ok_or(EscrowError::MathOverflow)?;

        self.daily_spent = daily_spent;
        self.window_count = window_count;
        self.held_total = held_total;
        Ok(())
    }

    /// Removes a hold from `held_total` and gives the unused part back to the
    /// daily limit if the hold was counted in the current day window.
    pub fn settle_hold(
        &mut self,
        hold_amount: u64,
        unused: u64,
        hold_created_ts: i64,
    ) -> Result<()> {
        require!(unused <= hold_amount, EscrowError::MathOverflow);
        self.held_total = self
            .held_total
            .checked_sub(hold_amount)
            .ok_or(EscrowError::MathOverflow)?;
        if hold_created_ts >= self.day_start_ts {
            self.daily_spent = self
                .daily_spent
                .checked_sub(unused)
                .ok_or(EscrowError::MathOverflow)?;
        }
        Ok(())
    }
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, Debug, InitSpace)]
pub enum HoldStatus {
    Pending,
    Captured,
    Released,
    Expired,
}

#[account]
#[derive(InitSpace, Debug)]
pub struct Hold {
    pub vault: Pubkey,
    pub auth_id: [u8; 32],
    pub amount: u64,
    pub captured_amount: u64,
    pub status: HoldStatus,
    pub created_ts: i64,
    pub expires_ts: i64,
    /// Receives the rent back on `close_hold`.
    pub rent_payer: Pubkey,
    pub bump: u8,
}

impl Hold {
    pub fn is_expired(&self, now: i64) -> bool {
        now >= self.expires_ts
    }

    pub fn require_pending(&self) -> Result<()> {
        require!(
            self.status == HoldStatus::Pending,
            EscrowError::HoldNotPending
        );
        Ok(())
    }

    pub fn is_final(&self) -> bool {
        self.status != HoldStatus::Pending
    }

    /// Validates a capture and returns the amount released back to the vault.
    pub fn capture(&mut self, amount: u64, now: i64) -> Result<u64> {
        self.require_pending()?;
        require!(!self.is_expired(now), EscrowError::HoldExpired);
        require!(amount > 0, EscrowError::ZeroAmount);
        require!(amount <= self.amount, EscrowError::CaptureExceedsHold);
        let released = self
            .amount
            .checked_sub(amount)
            .ok_or(EscrowError::MathOverflow)?;
        self.captured_amount = amount;
        self.status = HoldStatus::Captured;
        Ok(released)
    }

    pub fn release(&mut self) -> Result<()> {
        self.require_pending()?;
        self.status = HoldStatus::Released;
        Ok(())
    }

    pub fn expire(&mut self, now: i64) -> Result<()> {
        self.require_pending()?;
        require!(self.is_expired(now), EscrowError::HoldNotYetExpired);
        self.status = HoldStatus::Expired;
        Ok(())
    }
}

/// Exists only so a `refund_id` can never be processed twice.
#[account]
#[derive(InitSpace, Debug)]
pub struct Refund {
    pub vault: Pubkey,
    pub refund_id: [u8; 32],
    pub amount: u64,
    pub ts: i64,
    pub bump: u8,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vault() -> UserVault {
        UserVault {
            owner: Pubkey::default(),
            held_total: 0,
            daily_limit: 1_000,
            daily_spent: 0,
            day_start_ts: 0,
            velocity_max_auths: 3,
            velocity_window_seconds: 60,
            window_start_ts: 0,
            window_count: 0,
            bump: 255,
        }
    }

    fn hold(amount: u64) -> Hold {
        Hold {
            vault: Pubkey::default(),
            auth_id: [1; 32],
            amount,
            captured_amount: 0,
            status: HoldStatus::Pending,
            created_ts: 0,
            expires_ts: 100,
            rent_payer: Pubkey::default(),
            bump: 255,
        }
    }

    fn code<T: std::fmt::Debug>(r: Result<T>) -> u32 {
        match r.unwrap_err() {
            Error::AnchorError(e) => e.error_code_number,
            other => panic!("unexpected error {other:?}"),
        }
    }

    macro_rules! assert_err {
        ($expr:expr, $variant:ident) => {
            assert_eq!(code($expr), u32::from(EscrowError::$variant))
        };
    }

    #[test]
    fn authorization_reserves_and_counts() {
        let mut v = vault();
        v.record_authorization(400, 1_000, 10).unwrap();
        assert_eq!((v.held_total, v.daily_spent, v.window_count), (400, 400, 1));
        assert_eq!(v.available(1_000).unwrap(), 600);
    }

    #[test]
    fn authorization_rejects_zero_and_insufficient() {
        let mut v = vault();
        assert_err!(v.record_authorization(0, 1_000, 1), ZeroAmount);
        v.held_total = 900;
        v.daily_spent = 900;
        assert_err!(
            v.record_authorization(101, 1_000, 1),
            InsufficientAvailableBalance
        );
        v.record_authorization(100, 1_000, 1).unwrap();
        assert_eq!(v.held_total, 1_000);
    }

    #[test]
    fn held_exceeding_balance_is_an_invariant_error() {
        let mut v = vault();
        v.held_total = 10;
        assert_err!(v.available(5), HeldExceedsBalance);
        assert_err!(v.record_authorization(1, 5, 1), HeldExceedsBalance);
    }

    #[test]
    fn daily_limit_is_inclusive_and_rolls_over() {
        let mut v = vault();
        v.velocity_max_auths = 100;
        v.record_authorization(1_000, 5_000, 1).unwrap();
        assert_err!(v.record_authorization(1, 5_000, 2), DailyLimitExceeded);
        // One second before the window ends: still blocked.
        assert_err!(
            v.record_authorization(1, 5_000, SECONDS_PER_DAY - 1),
            DailyLimitExceeded
        );
        // Exactly at the boundary the window rolls.
        v.record_authorization(1_000, 5_000, SECONDS_PER_DAY)
            .unwrap();
        assert_eq!(v.day_start_ts, SECONDS_PER_DAY);
        assert_eq!(v.daily_spent, 1_000);
    }

    #[test]
    fn failed_check_does_not_mutate_counters() {
        let mut v = vault();
        v.velocity_max_auths = 100;
        v.record_authorization(900, 5_000, 1).unwrap();
        let before = (v.held_total, v.daily_spent, v.window_count);
        assert!(v.record_authorization(200, 5_000, 2).is_err());
        assert_eq!(before, (v.held_total, v.daily_spent, v.window_count));
    }

    #[test]
    fn velocity_window_counts_and_rolls_over() {
        let mut v = vault();
        for t in 0..3 {
            v.record_authorization(1, 5_000, t).unwrap();
        }
        assert_err!(v.record_authorization(1, 5_000, 59), VelocityLimitExceeded);
        v.record_authorization(1, 5_000, 60).unwrap();
        assert_eq!((v.window_start_ts, v.window_count), (60, 1));
    }

    #[test]
    fn zero_limits_freeze_the_card() {
        let mut v = vault();
        v.daily_limit = 0;
        assert_err!(v.record_authorization(1, 10, 1), DailyLimitExceeded);
        let mut v = vault();
        v.velocity_max_auths = 0;
        assert_err!(v.record_authorization(1, 10, 1), VelocityLimitExceeded);
    }

    #[test]
    fn overflow_is_rejected() {
        let mut v = vault();
        v.daily_limit = u64::MAX;
        v.daily_spent = u64::MAX;
        assert_err!(v.record_authorization(1, u64::MAX, 1), MathOverflow);

        let mut v = vault();
        v.velocity_max_auths = u32::MAX;
        v.window_count = u32::MAX;
        v.velocity_window_seconds = i64::MAX;
        assert_err!(v.record_authorization(1, 10, 1), MathOverflow);

        let mut v = vault();
        v.day_start_ts = i64::MAX;
        assert_err!(v.roll_windows(-10), MathOverflow);
        let mut v = vault();
        v.window_start_ts = i64::MAX;
        assert_err!(v.roll_windows(-10), MathOverflow);
    }

    #[test]
    fn settle_hold_restores_daily_only_for_current_window() {
        let mut v = vault();
        v.record_authorization(300, 1_000, 10).unwrap();
        v.settle_hold(300, 100, 10).unwrap();
        assert_eq!((v.held_total, v.daily_spent), (0, 200));

        let mut v = vault();
        v.record_authorization(300, 1_000, 10).unwrap();
        v.day_start_ts = 20; // a later window started
        v.daily_spent = 0;
        v.settle_hold(300, 300, 10).unwrap();
        assert_eq!((v.held_total, v.daily_spent), (0, 0));
    }

    #[test]
    fn settle_hold_rejects_inconsistent_amounts() {
        let mut v = vault();
        assert_err!(v.settle_hold(10, 0, 0), MathOverflow);
        v.held_total = 10;
        assert_err!(v.settle_hold(10, 11, 0), MathOverflow);
        assert_err!(v.settle_hold(10, 5, 0), MathOverflow);
    }

    #[test]
    fn hold_capture_rules() {
        let mut h = hold(100);
        assert_err!(h.capture(0, 1), ZeroAmount);
        assert_err!(h.capture(101, 1), CaptureExceedsHold);
        assert_err!(h.capture(50, 100), HoldExpired);
        assert_eq!(h.capture(60, 99).unwrap(), 40);
        assert_eq!((h.status, h.captured_amount), (HoldStatus::Captured, 60));
        assert_err!(h.capture(1, 1), HoldNotPending);
        assert!(h.is_final());
    }

    #[test]
    fn hold_full_capture_releases_nothing() {
        let mut h = hold(100);
        assert_eq!(h.capture(100, 1).unwrap(), 0);
    }

    #[test]
    fn hold_release_and_expire_rules() {
        let mut h = hold(100);
        assert!(!h.is_final());
        h.release().unwrap();
        assert_err!(h.release(), HoldNotPending);
        assert_err!(h.expire(1_000), HoldNotPending);

        let mut h = hold(100);
        assert_err!(h.expire(99), HoldNotYetExpired);
        h.expire(100).unwrap();
        assert_eq!(h.status, HoldStatus::Expired);
        assert_err!(h.capture(1, 1), HoldNotPending);
    }

    #[test]
    fn ttl_and_window_bounds() {
        assert!(validate_hold_ttl(MIN_HOLD_TTL_SECONDS).is_ok());
        assert!(validate_hold_ttl(MAX_HOLD_TTL_SECONDS).is_ok());
        assert_err!(validate_hold_ttl(MIN_HOLD_TTL_SECONDS - 1), InvalidHoldTtl);
        assert_err!(validate_hold_ttl(MAX_HOLD_TTL_SECONDS + 1), InvalidHoldTtl);
        assert!(validate_velocity_window(1).is_ok());
        assert!(validate_velocity_window(MAX_VELOCITY_WINDOW_SECONDS).is_ok());
        assert_err!(validate_velocity_window(0), InvalidVelocityWindow);
        assert_err!(
            validate_velocity_window(MAX_VELOCITY_WINDOW_SECONDS + 1),
            InvalidVelocityWindow
        );
    }
}
