//! Negative paths: idempotency, limits, pause semantics, wrong signers,
//! foreign accounts and overflow attempts.
mod common;

use anchor_lang::error::ErrorCode as Anchor;
use anchor_spl::token_2022::spl_token_2022::{
    self, extension::ExtensionType, state::Mint as MintState,
};
use card_escrow::{
    constants::{MAX_HOLD_TTL_SECONDS, MIN_HOLD_TTL_SECONDS, SECONDS_PER_DAY},
    errors::EscrowError,
    instructions::{UpdateConfigArgs, VaultLimits},
    state::HoldStatus,
};
use common::*;
use litesvm_token::{CreateAssociatedTokenAccount, CreateMint, MintTo};
use solana_keypair::Keypair;
use solana_signer::Signer;

fn limits(daily: u64, max_auths: u32, window: i64) -> VaultLimits {
    VaultLimits {
        daily_limit: daily,
        velocity_max_auths: max_auths,
        velocity_window_seconds: window,
    }
}

fn set_limits(env: &mut Env, user: &User, l: VaultLimits) {
    let ix = env.ix_set_limits(&user.pubkey(), &user.vault, l);
    assert_ok(env.send(&[ix], &[&user.kp]));
}

fn funded(env: &mut Env) -> Keypair {
    let kp = Keypair::new();
    env.svm.airdrop(&kp.pubkey(), 10_000_000_000).unwrap();
    kp
}

// ------------------------------------------------------------- idempotency

#[test]
fn duplicate_authorize_fails_and_changes_nothing() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(7);
    assert_ok(env.authorize(&user, id, 100 * USD));
    let before = env.vault(&user);
    assert_custom(env.authorize(&user, id, 100 * USD), ACCOUNT_ALREADY_IN_USE);
    // A different amount under the same auth_id is equally rejected.
    assert_custom(env.authorize(&user, id, 5 * USD), ACCOUNT_ALREADY_IN_USE);
    let after = env.vault(&user);
    assert_eq!(before.held_total, after.held_total);
    assert_eq!(before.window_count, after.window_count);
    assert_eq!(env.hold(&user, &id).amount, 100 * USD);
}

#[test]
fn same_auth_id_on_different_vaults_is_independent() {
    let mut env = Env::new();
    let a = take_user(&mut env);
    let b = env.new_user_with_vault(100 * USD);
    assert_ok(env.authorize(&a, auth_id(1), USD));
    assert_ok(env.authorize(&b, auth_id(1), USD));
}

#[test]
fn duplicate_refund_fails() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let rid = auth_id(77);
    assert_ok(env.refund(&user, rid, 10 * USD));
    assert_custom(env.refund(&user, rid, 10 * USD), ACCOUNT_ALREADY_IN_USE);
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT + 10 * USD);
}

#[test]
fn closed_hold_id_can_be_reauthorized_on_chain() {
    // Documented trade-off: after close_hold the on-chain record is gone, so
    // long-term auth_id idempotency is the backend's unique constraint.
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, USD));
    assert_ok(env.release(&user, id));
    let op = env.operator.insecure_clone();
    assert_ok(env.send(&[env.ix_close_hold(&user, id)], &[&op]));
    assert_ok(env.authorize(&user, id, USD));
    assert_eq!(env.hold(&user, &id).status, HoldStatus::Pending);
}

// ---------------------------------------------------------- capture rules

#[test]
fn capture_more_than_hold_fails() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 100 * USD));
    assert_escrow_err(env.capture(&user, id, 100 * USD + 1), EscrowError::CaptureExceedsHold);
    assert_escrow_err(env.capture(&user, id, u64::MAX), EscrowError::CaptureExceedsHold);
    assert_escrow_err(env.capture(&user, id, 0), EscrowError::ZeroAmount);
    assert_eq!(env.hold(&user, &id).status, HoldStatus::Pending);
}

#[test]
fn capture_and_release_require_pending() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    // Captured
    assert_ok(env.authorize(&user, auth_id(1), 10 * USD));
    assert_ok(env.capture(&user, auth_id(1), 5 * USD));
    assert_escrow_err(env.capture(&user, auth_id(1), USD), EscrowError::HoldNotPending);
    assert_escrow_err(env.release(&user, auth_id(1)), EscrowError::HoldNotPending);
    // Released
    assert_ok(env.authorize(&user, auth_id(2), 10 * USD));
    assert_ok(env.release(&user, auth_id(2)));
    assert_escrow_err(env.capture(&user, auth_id(2), USD), EscrowError::HoldNotPending);
    assert_escrow_err(env.release(&user, auth_id(2)), EscrowError::HoldNotPending);
    // Expired
    assert_ok(env.authorize(&user, auth_id(3), 10 * USD));
    env.advance(DEFAULT_TTL);
    let op = env.operator.insecure_clone();
    assert_ok(env.send(&[env.ix_expire(&user, auth_id(3))], &[&op]));
    assert_escrow_err(env.capture(&user, auth_id(3), USD), EscrowError::HoldNotPending);
    assert_escrow_err(env.release(&user, auth_id(3)), EscrowError::HoldNotPending);
    assert_escrow_err(
        env.send(&[env.ix_expire(&user, auth_id(3))], &[&op]),
        EscrowError::HoldNotPending,
    );
    assert_eq!(env.vault(&user).held_total, 0);
}

#[test]
fn capture_after_expiry_fails_but_release_works() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 10 * USD));
    env.advance(DEFAULT_TTL);
    assert_escrow_err(env.capture(&user, id, USD), EscrowError::HoldExpired);
    assert_ok(env.release(&user, id));
}

#[test]
fn expire_before_ttl_fails() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 10 * USD));
    let op = env.operator.insecure_clone();
    assert_escrow_err(
        env.send(&[env.ix_expire(&user, id)], &[&op]),
        EscrowError::HoldNotYetExpired,
    );
    env.advance(DEFAULT_TTL - 1);
    assert_escrow_err(
        env.send(&[env.ix_expire(&user, id)], &[&op]),
        EscrowError::HoldNotYetExpired,
    );
    // Capture is still possible in the last second.
    assert_ok(env.capture(&user, id, USD));
}

#[test]
fn close_pending_hold_fails() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, USD));
    let op = env.operator.insecure_clone();
    assert_escrow_err(
        env.send(&[env.ix_close_hold(&user, id)], &[&op]),
        EscrowError::HoldNotFinal,
    );
}

#[test]
fn close_hold_rent_goes_only_to_recorded_payer() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, USD));
    assert_ok(env.release(&user, id));
    let thief = funded(&mut env);
    let op = env.operator.insecure_clone();
    let ix = env.ix_close_hold_raw(&op.pubkey(), &hold_pda(&user.vault, &id), &thief.pubkey());
    assert_anchor_err(env.send(&[ix], &[&op]), Anchor::ConstraintHasOne);
}

// ------------------------------------------------------- balance & limits

#[test]
fn withdraw_blocked_by_held_funds() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    assert_ok(env.authorize(&user, auth_id(1), 300 * USD));
    assert_escrow_err(
        env.withdraw(&user, INITIAL_DEPOSIT - 300 * USD + 1),
        EscrowError::InsufficientAvailableBalance,
    );
    assert_escrow_err(env.withdraw(&user, INITIAL_DEPOSIT), EscrowError::InsufficientAvailableBalance);
    assert_ok(env.withdraw(&user, INITIAL_DEPOSIT - 300 * USD));
    assert_eq!(env.balance(&user.vault_ata), 300 * USD);
    // The hold can still be captured in full.
    assert_ok(env.capture(&user, auth_id(1), 300 * USD));
    assert_eq!(env.balance(&user.vault_ata), 0);
}

#[test]
fn authorize_more_than_available_fails() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    set_limits(&mut env, &user, limits(u64::MAX, 100, 60));
    assert_ok(env.authorize(&user, auth_id(1), 600 * USD));
    assert_escrow_err(
        env.authorize(&user, auth_id(2), 400 * USD + 1),
        EscrowError::InsufficientAvailableBalance,
    );
    assert_ok(env.authorize(&user, auth_id(3), 400 * USD));
    assert_escrow_err(env.authorize(&user, auth_id(4), 1), EscrowError::InsufficientAvailableBalance);
    assert_escrow_err(env.authorize(&user, auth_id(5), 0), EscrowError::ZeroAmount);
}

#[test]
fn zero_amount_deposit_withdraw_refund_rejected() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    assert_escrow_err(
        env.send(&[env.ix_deposit(&user, 0)], &[&user.kp]),
        EscrowError::ZeroAmount,
    );
    assert_escrow_err(env.withdraw(&user, 0), EscrowError::ZeroAmount);
    assert_escrow_err(env.refund(&user, auth_id(1), 0), EscrowError::ZeroAmount);
}

#[test]
fn daily_limit_boundary_and_rollover() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    set_limits(&mut env, &user, limits(100 * USD, 100, 60));
    assert_ok(env.authorize(&user, auth_id(1), 60 * USD));
    assert_ok(env.authorize(&user, auth_id(2), 40 * USD)); // exactly at limit
    assert_escrow_err(env.authorize(&user, auth_id(3), 1), EscrowError::DailyLimitExceeded);

    // A release gives the amount back to today's limit.
    assert_ok(env.release(&user, auth_id(2)));
    assert_ok(env.authorize(&user, auth_id(4), 40 * USD));
    assert_escrow_err(env.authorize(&user, auth_id(5), 1), EscrowError::DailyLimitExceeded);

    // One second before rollover: still blocked.
    env.warp_to(START_TS + SECONDS_PER_DAY - 1);
    assert_escrow_err(env.authorize(&user, auth_id(6), 1), EscrowError::DailyLimitExceeded);
    // At rollover the window restarts.
    env.warp_to(START_TS + SECONDS_PER_DAY);
    assert_ok(env.authorize(&user, auth_id(7), 100 * USD));
    let v = env.vault(&user);
    assert_eq!((v.day_start_ts, v.daily_spent), (START_TS + SECONDS_PER_DAY, 100 * USD));

    // Releasing yesterday's hold does not inflate today's remaining limit.
    assert_ok(env.release(&user, auth_id(1)));
    assert_eq!(env.vault(&user).daily_spent, 100 * USD);
    assert_escrow_err(env.authorize(&user, auth_id(8), 1), EscrowError::DailyLimitExceeded);
}

#[test]
fn velocity_limit_boundary_and_rollover() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    set_limits(&mut env, &user, limits(u64::MAX, 3, 600));
    for n in 0..3 {
        assert_ok(env.authorize(&user, auth_id(n), USD));
        env.advance(10);
    }
    assert_escrow_err(env.authorize(&user, auth_id(10), USD), EscrowError::VelocityLimitExceeded);
    // Releases do not refund velocity: count is about attempts, not amounts.
    assert_ok(env.release(&user, auth_id(0)));
    assert_escrow_err(env.authorize(&user, auth_id(11), USD), EscrowError::VelocityLimitExceeded);
    env.warp_to(START_TS + 599);
    assert_escrow_err(env.authorize(&user, auth_id(12), USD), EscrowError::VelocityLimitExceeded);
    env.warp_to(START_TS + 600);
    assert_ok(env.authorize(&user, auth_id(13), USD));
    let v = env.vault(&user);
    assert_eq!((v.window_start_ts, v.window_count), (START_TS + 600, 1));
}

#[test]
fn zero_limits_freeze_card_but_not_withdrawals() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    set_limits(&mut env, &user, limits(0, 5, 60));
    assert_escrow_err(env.authorize(&user, auth_id(1), USD), EscrowError::DailyLimitExceeded);
    set_limits(&mut env, &user, limits(USD, 0, 60));
    assert_escrow_err(env.authorize(&user, auth_id(1), USD), EscrowError::VelocityLimitExceeded);
    assert_ok(env.withdraw(&user, INITIAL_DEPOSIT));
}

#[test]
fn invalid_velocity_window_rejected() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    for w in [0, -1, SECONDS_PER_DAY + 1] {
        let ix = env.ix_set_limits(&user.pubkey(), &user.vault, limits(1, 1, w));
        assert_escrow_err(env.send(&[ix], &[&user.kp]), EscrowError::InvalidVelocityWindow);
    }
    let other = env.new_user();
    let ix = env.ix_open_vault(&other, limits(1, 1, 0));
    assert_escrow_err(env.send(&[ix], &[&other.kp]), EscrowError::InvalidVelocityWindow);
}

// ------------------------------------------------------------------ pause

#[test]
fn pause_blocks_authorize_capture_deposit_only() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    assert_ok(env.authorize(&user, auth_id(1), 100 * USD));
    assert_ok(env.authorize(&user, auth_id(2), 100 * USD));
    assert_ok(env.set_paused(true));

    assert_escrow_err(env.authorize(&user, auth_id(3), USD), EscrowError::Paused);
    assert_escrow_err(env.capture(&user, auth_id(1), USD), EscrowError::Paused);
    assert_escrow_err(
        env.send(&[env.ix_deposit(&user, USD)], &[&user.kp]),
        EscrowError::Paused,
    );
    // Everything that only returns value to users keeps working.
    assert_ok(env.withdraw(&user, INITIAL_DEPOSIT - 200 * USD));
    assert_ok(env.release(&user, auth_id(1)));
    assert_ok(env.withdraw(&user, 100 * USD));
    assert_ok(env.refund(&user, auth_id(50), 5 * USD));
    env.advance(DEFAULT_TTL);
    let op = env.operator.insecure_clone();
    assert_ok(env.send(&[env.ix_expire(&user, auth_id(2))], &[&op]));
    assert_ok(env.withdraw(&user, 105 * USD));
    assert_eq!(env.balance(&user.vault_ata), 0);
    let ix = env.ix_set_limits(&user.pubkey(), &user.vault, default_limits());
    assert_ok(env.send(&[ix], &[&user.kp]));

    assert_ok(env.set_paused(false));
    assert_ok(env.send(&[env.ix_deposit(&user, USD)], &[&user.kp]));
}

// ---------------------------------------------------------- wrong signers

#[test]
fn initialize_config_only_by_upgrade_authority() {
    let mut env = Env::bare(anchor_spl::token::ID);
    let attacker = funded(&mut env);
    let ix = env.ix_initialize_config(&attacker.pubkey(), DEFAULT_TTL);
    assert_escrow_err(env.send(&[ix], &[&attacker]), EscrowError::NotUpgradeAuthority);

    // An immutable program (no authority) cannot be initialized by anyone.
    set_upgrade_authority(&mut env.svm, None);
    let admin = env.admin.insecure_clone();
    assert_escrow_err(env.init_config(), EscrowError::NotUpgradeAuthority);

    set_upgrade_authority(&mut env.svm, Some(admin.pubkey()));
    assert_ok(env.init_config());
    // Re-initialization is impossible.
    assert_custom(env.init_config(), ACCOUNT_ALREADY_IN_USE);
}

#[test]
fn initialize_config_with_fake_program_data_fails() {
    let mut env = Env::bare(anchor_spl::token::ID);
    let attacker = funded(&mut env);
    let mut ix = env.ix_initialize_config(&attacker.pubkey(), DEFAULT_TTL);
    // Point program_data at some other account.
    ix.accounts[5].pubkey = env.settlement_ata;
    let res = env.send(&[ix], &[&attacker]);
    assert!(res.is_err());
}

#[test]
fn initialize_config_validates_inputs() {
    let mut env = Env::bare(anchor_spl::token::ID);
    let admin = env.admin.insecure_clone();
    for ttl in [MIN_HOLD_TTL_SECONDS - 1, MAX_HOLD_TTL_SECONDS + 1, 0, -5] {
        let ix = env.ix_initialize_config(&admin.pubkey(), ttl);
        assert_escrow_err(env.send(&[ix], &[&admin]), EscrowError::InvalidHoldTtl);
    }
    // Settlement account owned by someone other than settlement_authority.
    let stranger = funded(&mut env);
    let mint = env.mint;
    let tp = env.token_program;
    let foreign = CreateAssociatedTokenAccount::new(&mut env.svm, &stranger, &mint)
        .token_program_id(&tp)
        .send()
        .unwrap();
    let ix = env.ix_initialize_config_with(&admin.pubkey(), &mint, &foreign, DEFAULT_TTL);
    assert_escrow_err(env.send(&[ix], &[&admin]), EscrowError::SettlementOwnerMismatch);
    // Settlement account of a different mint.
    let other_mint = CreateMint::new(&mut env.svm, &stranger)
        .decimals(6)
        .token_program_id(&tp)
        .send()
        .unwrap();
    let ix = env.ix_initialize_config_with(&admin.pubkey(), &other_mint, &env.settlement_ata, DEFAULT_TTL);
    assert_anchor_err(env.send(&[ix], &[&admin]), Anchor::ConstraintTokenMint);
}

#[test]
fn admin_instructions_reject_non_admin() {
    let mut env = Env::new();
    let op = env.operator.insecure_clone();
    for signer in [op, env.user.kp.insecure_clone(), env.settlement_authority.insecure_clone()] {
        let ix = env.ix_set_paused(&signer.pubkey(), true);
        assert_escrow_err(env.send(&[ix], &[&signer]), EscrowError::Unauthorized);
        let args = UpdateConfigArgs {
            operator: Some(signer.pubkey()),
            ..Default::default()
        };
        let ix = env.ix_update_config(&signer.pubkey(), &env.settlement_ata, args);
        assert_escrow_err(env.send(&[ix], &[&signer]), EscrowError::Unauthorized);
    }
    assert!(!env.config().paused);
}

#[test]
fn update_config_validates_inputs() {
    let mut env = Env::new();
    let admin = env.admin.insecure_clone();
    let args = UpdateConfigArgs {
        default_hold_ttl_seconds: Some(1),
        ..Default::default()
    };
    let ix = env.ix_update_config(&admin.pubkey(), &env.settlement_ata, args);
    assert_escrow_err(env.send(&[ix], &[&admin]), EscrowError::InvalidHoldTtl);
    // Changing the settlement authority without a matching settlement account.
    let args = UpdateConfigArgs {
        settlement_authority: Some(Keypair::new().pubkey()),
        ..Default::default()
    };
    let ix = env.ix_update_config(&admin.pubkey(), &env.settlement_ata, args);
    assert_escrow_err(env.send(&[ix], &[&admin]), EscrowError::SettlementOwnerMismatch);
}

#[test]
fn operator_instructions_reject_non_operator() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    assert_ok(env.authorize(&user, auth_id(1), 10 * USD));
    let admin = env.admin.insecure_clone();
    let sa = env.settlement_authority.insecure_clone();
    let hold = hold_pda(&user.vault, &auth_id(1));
    for signer in [&admin, &user.kp, &sa] {
        let ix = env.ix_authorize_raw(&signer.pubkey(), &user, auth_id(2), USD);
        assert_escrow_err(env.send(&[ix], &[signer]), EscrowError::Unauthorized);
        let ix = env.ix_capture_raw(
            &signer.pubkey(),
            &user.vault,
            &hold,
            &user.vault_ata,
            &env.settlement_ata,
            USD,
        );
        assert_escrow_err(env.send(&[ix], &[signer]), EscrowError::Unauthorized);
        let ix = env.ix_release_raw(&signer.pubkey(), &user.vault, &hold);
        assert_escrow_err(env.send(&[ix], &[signer]), EscrowError::Unauthorized);
    }
    assert_ok(env.release(&user, auth_id(1)));
    for signer in [&admin, &user.kp, &sa] {
        let ix = env.ix_close_hold_raw(&signer.pubkey(), &hold, &env.operator.pubkey());
        assert_escrow_err(env.send(&[ix], &[signer]), EscrowError::Unauthorized);
    }
}

#[test]
fn operator_cannot_touch_user_funds_or_limits() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let op = env.operator.insecure_clone();
    let op_ata = {
        let mint = env.mint;
        let tp = env.token_program;
        CreateAssociatedTokenAccount::new(&mut env.svm, &op, &mint)
            .token_program_id(&tp)
            .send()
            .unwrap()
    };
    // Withdraw from the user's vault, signing as operator.
    let ix = env.ix_withdraw_raw(&op.pubkey(), &user.vault, &user.vault_ata, &op_ata, USD);
    assert_anchor_err(env.send(&[ix], &[&op]), Anchor::ConstraintSeeds);
    // Change the user's limits.
    let ix = env.ix_set_limits(&op.pubkey(), &user.vault, limits(u64::MAX, u32::MAX, 1));
    assert_anchor_err(env.send(&[ix], &[&op]), Anchor::ConstraintSeeds);
    // Capture into its own token account instead of the settlement account.
    assert_ok(env.authorize(&user, auth_id(1), 10 * USD));
    let ix = env.ix_capture_raw(
        &op.pubkey(),
        &user.vault,
        &hold_pda(&user.vault, &auth_id(1)),
        &user.vault_ata,
        &op_ata,
        10 * USD,
    );
    assert_escrow_err(env.send(&[ix], &[&op]), EscrowError::InvalidSettlementAccount);
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT);
}

#[test]
fn vault_instructions_reject_non_owner() {
    let mut env = Env::new();
    let victim = take_user(&mut env);
    let attacker = env.new_user_with_vault(10 * USD);
    // Attacker signs, passes the victim's vault: PDA seeds use the signer key.
    let ix = env.ix_withdraw_raw(
        &attacker.pubkey(),
        &victim.vault,
        &victim.vault_ata,
        &attacker.wallet_ata,
        USD,
    );
    assert_anchor_err(env.send(&[ix], &[&attacker.kp]), Anchor::ConstraintSeeds);
    let ix = env.ix_deposit_raw(
        &attacker.pubkey(),
        &victim.vault,
        &victim.vault_ata,
        &attacker.wallet_ata,
        USD,
    );
    assert_anchor_err(env.send(&[ix], &[&attacker.kp]), Anchor::ConstraintSeeds);
    let ix = env.ix_set_limits(&attacker.pubkey(), &victim.vault, default_limits());
    assert_anchor_err(env.send(&[ix], &[&attacker.kp]), Anchor::ConstraintSeeds);
    // Own vault PDA but the victim's token account.
    let ix = env.ix_withdraw_raw(
        &attacker.pubkey(),
        &attacker.vault,
        &victim.vault_ata,
        &attacker.wallet_ata,
        USD,
    );
    assert!(env.send(&[ix], &[&attacker.kp]).is_err());
    assert_eq!(env.balance(&victim.vault_ata), INITIAL_DEPOSIT);
}

#[test]
fn refund_rejects_wrong_signer_and_account() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let op = env.operator.insecure_clone();
    let admin = env.admin.insecure_clone();
    for signer in [&op, &admin, &user.kp] {
        let ix = env.ix_refund_raw(&signer.pubkey(), &user, &env.settlement_ata, auth_id(1), USD);
        assert_escrow_err(env.send(&[ix], &[signer]), EscrowError::Unauthorized);
    }
    // Correct signer, but a different source token account it also owns.
    let sa = env.settlement_authority.insecure_clone();
    let mint = env.mint;
    let tp = env.token_program;
    let side = litesvm_token::CreateAccount::new(&mut env.svm, &sa, &mint)
        .owner(&sa.pubkey())
        .token_program_id(&tp)
        .send()
        .unwrap();
    let ix = env.ix_refund_raw(&sa.pubkey(), &user, &side, auth_id(1), USD);
    assert_escrow_err(env.send(&[ix], &[&sa]), EscrowError::InvalidSettlementAccount);
}

// --------------------------------------------------------- foreign accounts

#[test]
fn foreign_vault_and_hold_combinations_fail() {
    let mut env = Env::new();
    let a = take_user(&mut env);
    let b = env.new_user_with_vault(500 * USD);
    assert_ok(env.authorize(&a, auth_id(1), 100 * USD));
    let op = env.operator.insecure_clone();
    let hold_a = hold_pda(&a.vault, &auth_id(1));

    // Capture A's hold while debiting B's vault.
    let ix = env.ix_capture_raw(&op.pubkey(), &b.vault, &hold_a, &b.vault_ata, &env.settlement_ata, USD);
    assert_anchor_err(env.send(&[ix], &[&op]), Anchor::ConstraintSeeds);
    // A's hold + vault, but B's token account.
    let ix = env.ix_capture_raw(&op.pubkey(), &a.vault, &hold_a, &b.vault_ata, &env.settlement_ata, USD);
    assert!(env.send(&[ix], &[&op]).is_err());
    // Release A's hold against B's vault (would corrupt B's held_total).
    let ix = env.ix_release_raw(&op.pubkey(), &b.vault, &hold_a);
    assert_anchor_err(env.send(&[ix], &[&op]), Anchor::ConstraintSeeds);
    // Authorize against A's vault using B's (larger) token balance.
    let mut ix = env.ix_authorize(&a, auth_id(2), 600 * USD);
    ix.accounts[5].pubkey = b.vault_ata;
    assert!(env.send(&[ix], &[&op]).is_err());

    assert_eq!(env.vault(&a).held_total, 100 * USD);
    assert_eq!(env.vault(&b).held_total, 0);
    assert_eq!(env.balance(&b.vault_ata), 500 * USD);
}

#[test]
fn wrong_mint_is_rejected_everywhere() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let payer = env.mint_authority.insecure_clone();
    let tp = env.token_program;
    let fake_mint = CreateMint::new(&mut env.svm, &payer)
        .authority(&payer.pubkey())
        .decimals(DECIMALS)
        .token_program_id(&tp)
        .send()
        .unwrap();
    let fake_wallet = CreateAssociatedTokenAccount::new(&mut env.svm, &user.kp, &fake_mint)
        .token_program_id(&tp)
        .send()
        .unwrap();
    MintTo::new(&mut env.svm, &payer, &fake_mint, &fake_wallet, 1_000 * USD)
        .token_program_id(&tp)
        .send()
        .unwrap();

    // Deposit with a fake mint.
    let mut ix = env.ix_deposit_raw(&user.pubkey(), &user.vault, &user.vault_ata, &fake_wallet, USD);
    ix.accounts[2].pubkey = fake_mint;
    assert_escrow_err(env.send(&[ix], &[&user.kp]), EscrowError::InvalidMint);
    // Deposit from a token account of another mint.
    let ix = env.ix_deposit_raw(&user.pubkey(), &user.vault, &user.vault_ata, &fake_wallet, USD);
    assert_anchor_err(env.send(&[ix], &[&user.kp]), Anchor::ConstraintTokenMint);
    // Withdraw into a token account of another mint.
    let ix = env.ix_withdraw_raw(&user.pubkey(), &user.vault, &user.vault_ata, &fake_wallet, USD);
    assert_anchor_err(env.send(&[ix], &[&user.kp]), Anchor::ConstraintTokenMint);
    // Authorize with a fake mint.
    let mut ix = env.ix_authorize(&user, auth_id(1), USD);
    ix.accounts[3].pubkey = fake_mint;
    let op = env.operator.insecure_clone();
    assert_escrow_err(env.send(&[ix], &[&op]), EscrowError::InvalidMint);
    // Open a vault with a fake mint.
    let other = env.new_user();
    // Anchor runs the vault ATA `init` before `has_one = mint`, so this fails
    // inside the ATA CPI instead of with InvalidMint; the tx is atomic either way.
    let mut ix = env.ix_open_vault(&other, default_limits());
    ix.accounts[2].pubkey = fake_mint;
    assert!(env.send(&[ix], &[&other.kp]).is_err());
    assert!(!env.exists(&other.vault));
}

#[test]
fn deposit_from_someone_elses_token_account_fails() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let victim = env.new_user();
    let ix = env.ix_deposit_raw(&user.pubkey(), &user.vault, &user.vault_ata, &victim.wallet_ata, USD);
    assert_anchor_err(env.send(&[ix], &[&user.kp]), Anchor::ConstraintTokenOwner);
}

#[test]
fn withdraw_to_own_vault_account_is_rejected() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let ix = env.ix_withdraw_raw(&user.pubkey(), &user.vault, &user.vault_ata, &user.vault_ata, USD);
    assert!(env.send(&[ix], &[&user.kp]).is_err());
}

#[test]
fn token_2022_mint_with_permanent_delegate_is_rejected() {
    let mut env = Env::bare(anchor_spl::token_2022::ID);
    let admin = env.admin.insecure_clone();
    let mint_kp = Keypair::new();
    let space =
        ExtensionType::try_calculate_account_len::<MintState>(&[ExtensionType::PermanentDelegate])
            .unwrap();
    let rent = env.svm.minimum_balance_for_rent_exemption(space);
    let ixs = vec![
        anchor_lang::solana_program::system_instruction::create_account(
            &admin.pubkey(),
            &mint_kp.pubkey(),
            rent,
            space as u64,
            &spl_token_2022::ID,
        ),
        spl_token_2022::instruction::initialize_permanent_delegate(
            &spl_token_2022::ID,
            &mint_kp.pubkey(),
            &admin.pubkey(),
        )
        .unwrap(),
        spl_token_2022::instruction::initialize_mint2(
            &spl_token_2022::ID,
            &mint_kp.pubkey(),
            &admin.pubkey(),
            None,
            6,
        )
        .unwrap(),
    ];
    assert_ok(env.send(&ixs, &[&admin, &mint_kp]));
    let sa = env.settlement_authority.insecure_clone();
    let settlement = CreateAssociatedTokenAccount::new(&mut env.svm, &sa, &mint_kp.pubkey())
        .token_program_id(&spl_token_2022::ID)
        .send()
        .unwrap();
    let ix = env.ix_initialize_config_with(&admin.pubkey(), &mint_kp.pubkey(), &settlement, DEFAULT_TTL);
    assert_escrow_err(env.send(&[ix], &[&admin]), EscrowError::UnsupportedMintExtension);
}

// ---------------------------------------------------------------- overflow

#[test]
fn u64_max_amounts_fail_cleanly() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    set_limits(&mut env, &user, limits(u64::MAX, u32::MAX, 60));
    assert_escrow_err(env.authorize(&user, auth_id(1), u64::MAX), EscrowError::InsufficientAvailableBalance);
    assert_escrow_err(env.withdraw(&user, u64::MAX), EscrowError::InsufficientAvailableBalance);
    assert!(env.send(&[env.ix_deposit(&user, u64::MAX)], &[&user.kp]).is_err());
    assert!(env.refund(&user, auth_id(2), u64::MAX).is_err());
    assert_eq!(env.vault(&user).held_total, 0);
}

#[test]
fn held_total_near_u64_max_cannot_overflow() {
    // A vault holding (almost) the entire u64 supply of the mint: authorizing
    // it fully, then any further amount, must fail on balance rather than wrap.
    let mut env = Env::new();
    let user = env.new_user_with_tokens(0);
    let ix = env.ix_open_vault(&user, limits(u64::MAX, u32::MAX, 60));
    assert_ok(env.send(&[ix], &[&user.kp]));
    let payer = env.mint_authority.insecure_clone();
    let mint = env.mint;
    let tp = env.token_program;
    let supply = env.account::<anchor_spl::token_interface::Mint>(&mint).supply;
    let headroom = u64::MAX - supply;
    MintTo::new(&mut env.svm, &payer, &mint, &user.vault_ata, headroom)
        .token_program_id(&tp)
        .send()
        .unwrap();
    assert_ok(env.authorize(&user, auth_id(1), headroom));
    assert_eq!(env.vault(&user).held_total, headroom);
    assert_escrow_err(env.authorize(&user, auth_id(2), 1), EscrowError::InsufficientAvailableBalance);
    assert_escrow_err(env.authorize(&user, auth_id(3), u64::MAX), EscrowError::InsufficientAvailableBalance);
    assert_escrow_err(env.withdraw(&user, 1), EscrowError::InsufficientAvailableBalance);
    assert_ok(env.release(&user, auth_id(1)));
    assert_eq!(env.vault(&user).daily_spent, 0);
    assert_ok(env.authorize(&user, auth_id(4), headroom));
}
