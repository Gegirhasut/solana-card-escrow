//! Happy paths for every instruction and the full authorization lifecycle.
mod common;

use card_escrow::{
    events::*,
    instructions::{UpdateConfigArgs, VaultLimits},
    state::HoldStatus,
};
use common::*;
use litesvm_token::CreateAssociatedTokenAccount;
use solana_keypair::Keypair;
use solana_signer::Signer;

#[test]
fn initialize_config_stores_settings() {
    let mut env = Env::bare(anchor_spl::token::ID);
    let meta = assert_ok(env.init_config());
    let cfg = env.config();
    assert_eq!(cfg.admin, env.admin.pubkey());
    assert_eq!(cfg.operator, env.operator.pubkey());
    assert_eq!(cfg.mint, env.mint);
    assert_eq!(cfg.settlement_token_account, env.settlement_ata);
    assert_eq!(cfg.settlement_authority, env.settlement_authority.pubkey());
    assert!(!cfg.paused);
    assert_eq!(cfg.default_hold_ttl_seconds, DEFAULT_TTL);
    let ev = events::<ConfigInitialized>(&meta);
    assert_eq!(ev.len(), 1);
    assert_eq!(ev[0].mint, env.mint);
}

#[test]
fn open_vault_and_deposit() {
    let mut env = Env::new();
    let user = env.new_user();
    let meta = assert_ok(env.send(&[env.ix_open_vault(&user, default_limits())], &[&user.kp]));
    let v = env.vault(&user);
    assert_eq!(v.owner, user.pubkey());
    assert_eq!(v.held_total, 0);
    assert_eq!(v.daily_limit, default_limits().daily_limit);
    assert_eq!(v.day_start_ts, START_TS);
    assert_eq!(v.window_start_ts, START_TS);
    assert_eq!(events::<VaultOpened>(&meta)[0].owner, user.pubkey());

    let meta = assert_ok(env.send(&[env.ix_deposit(&user, 250 * USD)], &[&user.kp]));
    assert_eq!(env.balance(&user.vault_ata), 250 * USD);
    assert_eq!(env.balance(&user.wallet_ata), INITIAL_USER_TOKENS - 250 * USD);
    let ev = &events::<Deposited>(&meta)[0];
    assert_eq!((ev.amount, ev.balance), (250 * USD, 250 * USD));
}

#[test]
fn open_vault_succeeds_when_ata_was_precreated_by_a_griefer() {
    let mut env = Env::new();
    let user = env.new_user();
    let griefer = Keypair::new();
    env.svm.airdrop(&griefer.pubkey(), 1_000_000_000).unwrap();
    let mint = env.mint;
    let tp = env.token_program;
    let ata = CreateAssociatedTokenAccount::new(&mut env.svm, &griefer, &mint)
        .owner(&user.vault)
        .token_program_id(&tp)
        .send()
        .unwrap();
    assert_eq!(ata, user.vault_ata);
    assert_ok(env.send(&[env.ix_open_vault(&user, default_limits())], &[&user.kp]));
}

#[test]
fn withdraw_returns_funds_to_owner() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let meta = assert_ok(env.withdraw(&user, 400 * USD));
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT - 400 * USD);
    assert_eq!(
        env.balance(&user.wallet_ata),
        INITIAL_USER_TOKENS - INITIAL_DEPOSIT + 400 * USD
    );
    assert_eq!(events::<Withdrawn>(&meta)[0].amount, 400 * USD);
    // Full remaining balance.
    assert_ok(env.withdraw(&user, INITIAL_DEPOSIT - 400 * USD));
    assert_eq!(env.balance(&user.vault_ata), 0);
}

#[test]
fn withdraw_to_any_token_account_of_the_mint() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let other = env.new_user_with_tokens(0);
    let ix = env.ix_withdraw_raw(&user.pubkey(), &user.vault, &user.vault_ata, &other.wallet_ata, USD);
    assert_ok(env.send(&[ix], &[&user.kp]));
    assert_eq!(env.balance(&other.wallet_ata), USD);
}

#[test]
fn set_limits_by_owner() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let limits = VaultLimits {
        daily_limit: 42 * USD,
        velocity_max_auths: 2,
        velocity_window_seconds: 60,
    };
    let meta = assert_ok(env.send(
        &[env.ix_set_limits(&user.pubkey(), &user.vault, limits)],
        &[&user.kp],
    ));
    let v = env.vault(&user);
    assert_eq!(
        (v.daily_limit, v.velocity_max_auths, v.velocity_window_seconds),
        (42 * USD, 2, 60)
    );
    assert_eq!(events::<LimitsUpdated>(&meta)[0].daily_limit, 42 * USD);
}

#[test]
fn authorize_creates_pending_hold() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    let meta = assert_ok(env.authorize(&user, id, 120 * USD));
    let h = env.hold(&user, &id);
    assert_eq!(h.status, HoldStatus::Pending);
    assert_eq!(h.amount, 120 * USD);
    assert_eq!(h.vault, user.vault);
    assert_eq!(h.auth_id, id);
    assert_eq!(h.created_ts, START_TS);
    assert_eq!(h.expires_ts, START_TS + DEFAULT_TTL);
    assert_eq!(h.rent_payer, env.operator.pubkey());

    let v = env.vault(&user);
    assert_eq!((v.held_total, v.daily_spent, v.window_count), (120 * USD, 120 * USD, 1));
    // Funds are reserved, not moved.
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT);

    let ev = &events::<HoldAuthorized>(&meta)[0];
    assert_eq!((ev.amount, ev.held_total, ev.auth_id), (120 * USD, 120 * USD, id));
}

#[test]
fn full_capture_pays_settlement() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 100 * USD));
    let meta = assert_ok(env.capture(&user, id, 100 * USD));

    let h = env.hold(&user, &id);
    assert_eq!((h.status, h.captured_amount), (HoldStatus::Captured, 100 * USD));
    let v = env.vault(&user);
    assert_eq!((v.held_total, v.daily_spent), (0, 100 * USD));
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT - 100 * USD);
    assert_eq!(env.balance(&env.settlement_ata), SETTLEMENT_FLOAT + 100 * USD);
    let ev = &events::<HoldCaptured>(&meta)[0];
    assert_eq!((ev.captured_amount, ev.released_amount), (100 * USD, 0));
}

#[test]
fn partial_capture_releases_remainder() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 100 * USD));
    assert_ok(env.authorize(&user, auth_id(2), 50 * USD));
    let meta = assert_ok(env.capture(&user, id, 70 * USD));

    let h = env.hold(&user, &id);
    assert_eq!((h.status, h.captured_amount), (HoldStatus::Captured, 70 * USD));
    let v = env.vault(&user);
    // Only the other hold is still reserved; the unused 30 return to the daily limit.
    assert_eq!((v.held_total, v.daily_spent), (50 * USD, 120 * USD));
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT - 70 * USD);
    let ev = &events::<HoldCaptured>(&meta)[0];
    assert_eq!((ev.captured_amount, ev.released_amount, ev.held_total), (70 * USD, 30 * USD, 50 * USD));
}

#[test]
fn release_frees_hold_and_daily_limit() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 100 * USD));
    let meta = assert_ok(env.release(&user, id));
    assert_eq!(env.hold(&user, &id).status, HoldStatus::Released);
    let v = env.vault(&user);
    assert_eq!((v.held_total, v.daily_spent), (0, 0));
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT);
    assert_eq!(events::<HoldReleased>(&meta)[0].amount, 100 * USD);
}

#[test]
fn expire_hold_after_ttl_by_anyone() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 100 * USD));
    env.advance(DEFAULT_TTL);

    let stranger = Keypair::new();
    env.svm.airdrop(&stranger.pubkey(), 1_000_000_000).unwrap();
    let meta = assert_ok(env.send(&[env.ix_expire(&user, id)], &[&stranger]));
    assert_eq!(env.hold(&user, &id).status, HoldStatus::Expired);
    assert_eq!(env.vault(&user).held_total, 0);
    assert_eq!(events::<HoldExpired>(&meta)[0].amount, 100 * USD);
    // The user can now withdraw everything.
    assert_ok(env.withdraw(&user, INITIAL_DEPOSIT));
}

#[test]
fn refund_moves_funds_back_to_vault() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 100 * USD));
    assert_ok(env.capture(&user, id, 100 * USD));

    let rid = auth_id(900);
    let meta = assert_ok(env.refund(&user, rid, 40 * USD));
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT - 60 * USD);
    assert_eq!(env.balance(&env.settlement_ata), SETTLEMENT_FLOAT + 60 * USD);
    let r = env.refund_record(&user, &rid);
    assert_eq!((r.vault, r.amount, r.refund_id, r.ts), (user.vault, 40 * USD, rid, START_TS));
    assert_eq!(events::<Refunded>(&meta)[0].amount, 40 * USD);
}

#[test]
fn close_hold_returns_rent_to_payer() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let id = auth_id(1);
    assert_ok(env.authorize(&user, id, 10 * USD));
    assert_ok(env.release(&user, id));
    let hold = hold_pda(&user.vault, &id);
    let rent = env.lamports(&hold);
    let before = env.lamports(&env.operator.pubkey());
    let op = env.operator.insecure_clone();
    let meta = assert_ok(env.send(&[env.ix_close_hold(&user, id)], &[&op]));
    assert!(!env.exists(&hold));
    let fee = meta.fee;
    assert_eq!(env.lamports(&env.operator.pubkey()), before + rent - fee);
    assert_eq!(events::<HoldClosed>(&meta)[0].auth_id, id);
}

#[test]
fn close_hold_works_for_every_final_status() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    assert_ok(env.authorize(&user, auth_id(1), USD));
    assert_ok(env.capture(&user, auth_id(1), USD));
    assert_ok(env.authorize(&user, auth_id(2), USD));
    env.advance(DEFAULT_TTL);
    let op = env.operator.insecure_clone();
    assert_ok(env.send(&[env.ix_expire(&user, auth_id(2))], &[&op]));
    for n in [1, 2] {
        assert_ok(env.send(&[env.ix_close_hold(&user, auth_id(n))], &[&op]));
    }
}

#[test]
fn update_config_rotates_roles_and_settlement() {
    let mut env = Env::new();
    let new_operator = Keypair::new();
    let new_settlement_authority = Keypair::new();
    let mint = env.mint;
    let tp = env.token_program;
    let payer = env.mint_authority.insecure_clone();
    let new_settlement = CreateAssociatedTokenAccount::new(&mut env.svm, &payer, &mint)
        .owner(&new_settlement_authority.pubkey())
        .token_program_id(&tp)
        .send()
        .unwrap();
    let args = UpdateConfigArgs {
        admin: None,
        operator: Some(new_operator.pubkey()),
        settlement_authority: Some(new_settlement_authority.pubkey()),
        default_hold_ttl_seconds: Some(3_600),
    };
    let admin = env.admin.insecure_clone();
    let meta = assert_ok(env.send(
        &[env.ix_update_config(&admin.pubkey(), &new_settlement, args)],
        &[&admin],
    ));
    let cfg = env.config();
    assert_eq!(cfg.operator, new_operator.pubkey());
    assert_eq!(cfg.settlement_authority, new_settlement_authority.pubkey());
    assert_eq!(cfg.settlement_token_account, new_settlement);
    assert_eq!(cfg.default_hold_ttl_seconds, 3_600);
    assert_eq!(cfg.admin, admin.pubkey());
    assert_eq!(events::<ConfigUpdated>(&meta)[0].operator, new_operator.pubkey());

    // Admin hand-over.
    let new_admin = Keypair::new();
    env.svm.airdrop(&new_admin.pubkey(), 1_000_000_000).unwrap();
    let args = UpdateConfigArgs {
        admin: Some(new_admin.pubkey()),
        ..Default::default()
    };
    assert_ok(env.send(
        &[env.ix_update_config(&admin.pubkey(), &new_settlement, args)],
        &[&admin],
    ));
    assert_eq!(env.config().admin, new_admin.pubkey());
    assert_escrow_err(
        env.send(&[env.ix_set_paused(&admin.pubkey(), true)], &[&admin]),
        card_escrow::errors::EscrowError::Unauthorized,
    );
    assert_ok(env.send(&[env.ix_set_paused(&new_admin.pubkey(), true)], &[&new_admin]));
}

#[test]
fn set_paused_toggles() {
    let mut env = Env::new();
    let meta = assert_ok(env.set_paused(true));
    assert!(env.config().paused);
    let ev = &events::<PausedSet>(&meta)[0];
    assert!(ev.paused);
    assert_ok(env.set_paused(false));
    assert!(!env.config().paused);
}

#[test]
fn full_card_lifecycle_token_2022() {
    let mut env = Env::new_2022();
    let user = take_user(&mut env);
    assert_ok(env.authorize(&user, auth_id(1), 100 * USD));
    assert_ok(env.capture(&user, auth_id(1), 80 * USD));
    assert_ok(env.authorize(&user, auth_id(2), 30 * USD));
    assert_ok(env.release(&user, auth_id(2)));
    assert_ok(env.refund(&user, auth_id(3), 80 * USD));
    assert_ok(env.withdraw(&user, INITIAL_DEPOSIT));
    assert_eq!(env.balance(&user.vault_ata), 0);
    assert_eq!(env.balance(&env.settlement_ata), SETTLEMENT_FLOAT);
    assert_eq!(env.vault(&user).held_total, 0);
}

#[test]
fn many_holds_keep_held_total_consistent() {
    let mut env = Env::new();
    let user = take_user(&mut env);
    let limits = VaultLimits {
        daily_limit: u64::MAX,
        velocity_max_auths: u32::MAX,
        velocity_window_seconds: 60,
    };
    assert_ok(env.send(&[env.ix_set_limits(&user.pubkey(), &user.vault, limits)], &[&user.kp]));
    for n in 0..12u64 {
        assert_ok(env.authorize(&user, auth_id(n), (n + 1) * USD));
    }
    let mut expected_held: u64 = (1..=12).sum::<u64>() * USD;
    let mut spent = 0;
    for n in 0..12u64 {
        let amount = (n + 1) * USD;
        match n % 3 {
            0 => {
                assert_ok(env.capture(&user, auth_id(n), amount / 2));
                spent += amount / 2;
            }
            1 => {
                assert_ok(env.release(&user, auth_id(n)));
            }
            _ => {
                assert_ok(env.capture(&user, auth_id(n), amount));
                spent += amount;
            }
        }
        expected_held -= amount;
        assert_eq!(env.vault(&user).held_total, expected_held);
    }
    assert_eq!(env.balance(&user.vault_ata), INITIAL_DEPOSIT - spent);
}
