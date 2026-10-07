//! Shared LiteSVM harness for card_escrow integration tests.
//!
//! The tests run the real SBF binary (`target/deploy/card_escrow.so`), so build
//! it first with `anchor build` (or `scripts/build.sh`).
#![allow(dead_code)]
// TxResult carries LiteSVM's FailedTransactionMetadata (logs included) on purpose.
#![allow(clippy::result_large_err)]

use anchor_lang::{
    prelude::Pubkey, solana_program::instruction::Instruction, system_program, AccountDeserialize,
    Discriminator, InstructionData, ToAccountMetas,
};
use anchor_spl::{
    associated_token::{self, get_associated_token_address_with_program_id},
    token::ID as TOKEN_PROGRAM_ID,
    token_2022::ID as TOKEN_2022_PROGRAM_ID,
    token_interface::TokenAccount,
};
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use card_escrow::{
    constants::{CONFIG_SEED, HOLD_SEED, REFUND_SEED, VAULT_SEED},
    errors::EscrowError,
    instructions::{InitializeConfigArgs, UpdateConfigArgs, VaultLimits},
    state::{Config, Hold, Refund, UserVault},
};
use litesvm::{
    types::{FailedTransactionMetadata, TransactionMetadata},
    LiteSVM,
};
use litesvm_token::{CreateAssociatedTokenAccount, CreateMint, MintTo};
use solana_clock::Clock;
use solana_instruction::error::InstructionError;
use solana_keypair::Keypair;
use solana_message::{Message, VersionedMessage};
use solana_signer::Signer;
use solana_transaction::versioned::VersionedTransaction;
use solana_transaction_error::TransactionError;

pub type TxResult = Result<TransactionMetadata, FailedTransactionMetadata>;

pub const DECIMALS: u8 = 6;
pub const USD: u64 = 1_000_000;
pub const DEFAULT_TTL: i64 = 7 * 86_400;
pub const START_TS: i64 = 1_760_000_000;
pub const INITIAL_USER_TOKENS: u64 = 10_000 * USD;
pub const INITIAL_DEPOSIT: u64 = 1_000 * USD;
pub const SETTLEMENT_FLOAT: u64 = 1_000_000 * USD;

pub fn default_limits() -> VaultLimits {
    VaultLimits {
        daily_limit: 500 * USD,
        velocity_max_auths: 5,
        velocity_window_seconds: 3_600,
    }
}

pub fn program_so_path() -> String {
    std::env::var("CARD_ESCROW_SO").unwrap_or_else(|_| {
        format!(
            "{}/../../target/deploy/card_escrow.so",
            env!("CARGO_MANIFEST_DIR")
        )
    })
}

pub fn auth_id(n: u64) -> [u8; 32] {
    let mut id = [0u8; 32];
    id[..8].copy_from_slice(&n.to_le_bytes());
    id[31] = 0xA5;
    id
}

pub fn config_pda() -> Pubkey {
    Pubkey::find_program_address(&[CONFIG_SEED], &card_escrow::ID).0
}

pub fn vault_pda(owner: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[VAULT_SEED, owner.as_ref()], &card_escrow::ID).0
}

pub fn hold_pda(vault: &Pubkey, auth_id: &[u8; 32]) -> Pubkey {
    Pubkey::find_program_address(&[HOLD_SEED, vault.as_ref(), auth_id], &card_escrow::ID).0
}

pub fn refund_pda(vault: &Pubkey, refund_id: &[u8; 32]) -> Pubkey {
    Pubkey::find_program_address(&[REFUND_SEED, vault.as_ref(), refund_id], &card_escrow::ID).0
}

pub fn program_data_pda() -> Pubkey {
    Pubkey::find_program_address(
        &[card_escrow::ID.as_ref()],
        &anchor_lang::solana_program::bpf_loader_upgradeable::ID,
    )
    .0
}

/// A user with a funded wallet token account and (optionally) an open vault.
pub struct User {
    pub kp: Keypair,
    pub wallet_ata: Pubkey,
    pub vault: Pubkey,
    pub vault_ata: Pubkey,
}

impl User {
    fn placeholder() -> Self {
        User {
            kp: Keypair::new(),
            wallet_ata: Pubkey::default(),
            vault: Pubkey::default(),
            vault_ata: Pubkey::default(),
        }
    }

    pub fn pubkey(&self) -> Pubkey {
        self.kp.pubkey()
    }
}

pub struct Env {
    pub svm: LiteSVM,
    pub admin: Keypair,
    pub operator: Keypair,
    pub settlement_authority: Keypair,
    pub mint_authority: Keypair,
    pub mint: Pubkey,
    pub token_program: Pubkey,
    pub settlement_ata: Pubkey,
    pub user: User,
}

impl Env {
    /// Classic SPL Token environment with config initialized and one user
    /// whose vault is open and funded with `INITIAL_DEPOSIT`.
    pub fn new() -> Self {
        Self::with_token_program(TOKEN_PROGRAM_ID)
    }

    pub fn new_2022() -> Self {
        Self::with_token_program(TOKEN_2022_PROGRAM_ID)
    }

    pub fn with_token_program(token_program: Pubkey) -> Self {
        let mut env = Self::bare(token_program);
        env.init_config().expect("initialize_config");
        env.user = env.new_user_with_vault(INITIAL_DEPOSIT);
        env
    }

    /// Program deployed, mint + settlement account created, config NOT initialized.
    pub fn bare(token_program: Pubkey) -> Self {
        let mut svm = LiteSVM::new();
        let bytes = std::fs::read(program_so_path()).unwrap_or_else(|e| {
            panic!(
                "cannot read {} ({e}); run `anchor build` first",
                program_so_path()
            )
        });
        svm.add_program(card_escrow::ID, &bytes).unwrap();

        let admin = Keypair::new();
        let operator = Keypair::new();
        let settlement_authority = Keypair::new();
        let mint_authority = Keypair::new();
        for kp in [&admin, &operator, &settlement_authority, &mint_authority] {
            svm.airdrop(&kp.pubkey(), 100_000_000_000).unwrap();
        }
        set_upgrade_authority(&mut svm, Some(admin.pubkey()));

        let mut clock: Clock = svm.get_sysvar();
        clock.unix_timestamp = START_TS;
        svm.set_sysvar(&clock);

        let mint = CreateMint::new(&mut svm, &mint_authority)
            .authority(&mint_authority.pubkey())
            .decimals(DECIMALS)
            .token_program_id(&token_program)
            .send()
            .unwrap();
        let settlement_ata = CreateAssociatedTokenAccount::new(&mut svm, &mint_authority, &mint)
            .owner(&settlement_authority.pubkey())
            .token_program_id(&token_program)
            .send()
            .unwrap();
        MintTo::new(
            &mut svm,
            &mint_authority,
            &mint,
            &settlement_ata,
            SETTLEMENT_FLOAT,
        )
        .token_program_id(&token_program)
        .send()
        .unwrap();

        Env {
            svm,
            admin,
            operator,
            settlement_authority,
            mint_authority,
            mint,
            token_program,
            settlement_ata,
            user: User::placeholder(),
        }
    }

    // ------------------------------------------------------------------ setup

    pub fn init_config(&mut self) -> TxResult {
        let ix = self.ix_initialize_config(&self.admin.pubkey(), DEFAULT_TTL);
        let admin = self.admin.insecure_clone();
        self.send(&[ix], &[&admin])
    }

    /// Creates a funded user and opens their vault with `deposit` tokens.
    pub fn new_user_with_vault(&mut self, deposit: u64) -> User {
        let user = self.new_user();
        self.send(&[self.ix_open_vault(&user, default_limits())], &[&user.kp])
            .expect("open_vault");
        if deposit > 0 {
            self.send(&[self.ix_deposit(&user, deposit)], &[&user.kp])
                .expect("deposit");
        }
        user
    }

    /// Creates a user with SOL and `INITIAL_USER_TOKENS` in their wallet ATA.
    pub fn new_user(&mut self) -> User {
        self.new_user_with_tokens(INITIAL_USER_TOKENS)
    }

    pub fn new_user_with_tokens(&mut self, tokens: u64) -> User {
        let kp = Keypair::new();
        self.svm.airdrop(&kp.pubkey(), 10_000_000_000).unwrap();
        let wallet_ata = CreateAssociatedTokenAccount::new(&mut self.svm, &kp, &self.mint)
            .token_program_id(&self.token_program)
            .send()
            .unwrap();
        if tokens > 0 {
            MintTo::new(
                &mut self.svm,
                &self.mint_authority,
                &self.mint,
                &wallet_ata,
                tokens,
            )
            .token_program_id(&self.token_program)
            .send()
            .unwrap();
        }
        let vault = vault_pda(&kp.pubkey());
        let vault_ata =
            get_associated_token_address_with_program_id(&vault, &self.mint, &self.token_program);
        User {
            kp,
            wallet_ata,
            vault,
            vault_ata,
        }
    }

    // ------------------------------------------------------------ transport

    /// Signs with `signers` (first one pays) and sends. A fresh blockhash per
    /// call lets tests resend byte-identical instructions.
    pub fn send(&mut self, ixs: &[Instruction], signers: &[&Keypair]) -> TxResult {
        self.svm.expire_blockhash();
        let blockhash = self.svm.latest_blockhash();
        let msg = Message::new_with_blockhash(ixs, Some(&signers[0].pubkey()), &blockhash);
        let tx = VersionedTransaction::try_new(VersionedMessage::Legacy(msg), signers).unwrap();
        self.svm.send_transaction(tx)
    }

    pub fn now(&self) -> i64 {
        self.svm.get_sysvar::<Clock>().unix_timestamp
    }

    pub fn warp_to(&mut self, ts: i64) {
        let mut clock: Clock = self.svm.get_sysvar();
        clock.unix_timestamp = ts;
        clock.slot += 1;
        self.svm.set_sysvar(&clock);
    }

    pub fn advance(&mut self, seconds: i64) {
        let now = self.now();
        self.warp_to(now + seconds);
    }

    // ---------------------------------------------------------------- reads

    pub fn account<T: AccountDeserialize>(&self, address: &Pubkey) -> T {
        let acc = self
            .svm
            .get_account(address)
            .unwrap_or_else(|| panic!("account {address} missing"));
        T::try_deserialize(&mut acc.data.as_slice()).unwrap()
    }

    pub fn exists(&self, address: &Pubkey) -> bool {
        self.svm
            .get_account(address)
            .is_some_and(|a| a.lamports > 0)
    }

    pub fn config(&self) -> Config {
        self.account(&config_pda())
    }

    pub fn vault(&self, user: &User) -> UserVault {
        self.account(&user.vault)
    }

    pub fn hold(&self, user: &User, id: &[u8; 32]) -> Hold {
        self.account(&hold_pda(&user.vault, id))
    }

    pub fn refund_record(&self, user: &User, id: &[u8; 32]) -> Refund {
        self.account(&refund_pda(&user.vault, id))
    }

    pub fn balance(&self, token_account: &Pubkey) -> u64 {
        let acc = self.svm.get_account(token_account).unwrap();
        TokenAccount::try_deserialize_unchecked(&mut acc.data.as_slice())
            .unwrap()
            .amount
    }

    pub fn lamports(&self, address: &Pubkey) -> u64 {
        self.svm.get_balance(address).unwrap_or(0)
    }

    // ---------------------------------------------------------- instructions

    pub fn ix_initialize_config(&self, admin: &Pubkey, ttl: i64) -> Instruction {
        self.ix_initialize_config_with(admin, &self.mint, &self.settlement_ata, ttl)
    }

    pub fn ix_initialize_config_with(
        &self,
        admin: &Pubkey,
        mint: &Pubkey,
        settlement_ata: &Pubkey,
        ttl: i64,
    ) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::InitializeConfig {
                args: InitializeConfigArgs {
                    operator: self.operator.pubkey(),
                    settlement_authority: self.settlement_authority.pubkey(),
                    default_hold_ttl_seconds: ttl,
                },
            }
            .data(),
            card_escrow::accounts::InitializeConfig {
                admin: *admin,
                config: config_pda(),
                mint: *mint,
                settlement_token_account: *settlement_ata,
                program: card_escrow::ID,
                program_data: program_data_pda(),
                token_program: self.token_program,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_update_config(
        &self,
        admin: &Pubkey,
        settlement_ata: &Pubkey,
        args: UpdateConfigArgs,
    ) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::UpdateConfig { args }.data(),
            card_escrow::accounts::UpdateConfig {
                admin: *admin,
                config: config_pda(),
                mint: self.mint,
                settlement_token_account: *settlement_ata,
                token_program: self.token_program,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_set_paused(&self, admin: &Pubkey, paused: bool) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::SetPaused { paused }.data(),
            card_escrow::accounts::SetPaused {
                admin: *admin,
                config: config_pda(),
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_open_vault(&self, user: &User, limits: VaultLimits) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::OpenVault { limits }.data(),
            card_escrow::accounts::OpenVault {
                owner: user.pubkey(),
                config: config_pda(),
                mint: self.mint,
                vault: user.vault,
                vault_token_account: user.vault_ata,
                token_program: self.token_program,
                associated_token_program: associated_token::ID,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_deposit(&self, user: &User, amount: u64) -> Instruction {
        self.ix_deposit_raw(
            &user.pubkey(),
            &user.vault,
            &user.vault_ata,
            &user.wallet_ata,
            amount,
        )
    }

    pub fn ix_deposit_raw(
        &self,
        owner: &Pubkey,
        vault: &Pubkey,
        vault_ata: &Pubkey,
        from: &Pubkey,
        amount: u64,
    ) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::Deposit { amount }.data(),
            card_escrow::accounts::Deposit {
                owner: *owner,
                config: config_pda(),
                mint: self.mint,
                vault: *vault,
                vault_token_account: *vault_ata,
                owner_token_account: *from,
                token_program: self.token_program,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_withdraw(&self, user: &User, amount: u64) -> Instruction {
        self.ix_withdraw_raw(
            &user.pubkey(),
            &user.vault,
            &user.vault_ata,
            &user.wallet_ata,
            amount,
        )
    }

    pub fn ix_withdraw_raw(
        &self,
        owner: &Pubkey,
        vault: &Pubkey,
        vault_ata: &Pubkey,
        destination: &Pubkey,
        amount: u64,
    ) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::Withdraw { amount }.data(),
            card_escrow::accounts::Withdraw {
                owner: *owner,
                config: config_pda(),
                mint: self.mint,
                vault: *vault,
                vault_token_account: *vault_ata,
                destination: *destination,
                token_program: self.token_program,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_set_limits(&self, owner: &Pubkey, vault: &Pubkey, limits: VaultLimits) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::SetLimits { limits }.data(),
            card_escrow::accounts::SetLimits {
                owner: *owner,
                vault: *vault,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_authorize(&self, user: &User, id: [u8; 32], amount: u64) -> Instruction {
        self.ix_authorize_raw(&self.operator.pubkey(), user, id, amount)
    }

    pub fn ix_authorize_raw(
        &self,
        operator: &Pubkey,
        user: &User,
        id: [u8; 32],
        amount: u64,
    ) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::Authorize { auth_id: id, amount }.data(),
            card_escrow::accounts::Authorize {
                operator: *operator,
                payer: *operator,
                config: config_pda(),
                mint: self.mint,
                vault: user.vault,
                vault_token_account: user.vault_ata,
                hold: hold_pda(&user.vault, &id),
                token_program: self.token_program,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_capture(&self, user: &User, id: [u8; 32], amount: u64) -> Instruction {
        self.ix_capture_raw(
            &self.operator.pubkey(),
            &user.vault,
            &hold_pda(&user.vault, &id),
            &user.vault_ata,
            &self.settlement_ata,
            amount,
        )
    }

    pub fn ix_capture_raw(
        &self,
        operator: &Pubkey,
        vault: &Pubkey,
        hold: &Pubkey,
        vault_ata: &Pubkey,
        settlement: &Pubkey,
        amount: u64,
    ) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::Capture { amount }.data(),
            card_escrow::accounts::Capture {
                operator: *operator,
                config: config_pda(),
                mint: self.mint,
                vault: *vault,
                hold: *hold,
                vault_token_account: *vault_ata,
                settlement_token_account: *settlement,
                token_program: self.token_program,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_release(&self, user: &User, id: [u8; 32]) -> Instruction {
        self.ix_release_raw(
            &self.operator.pubkey(),
            &user.vault,
            &hold_pda(&user.vault, &id),
        )
    }

    pub fn ix_release_raw(&self, operator: &Pubkey, vault: &Pubkey, hold: &Pubkey) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::Release {}.data(),
            card_escrow::accounts::Release {
                operator: *operator,
                config: config_pda(),
                vault: *vault,
                hold: *hold,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_expire(&self, user: &User, id: [u8; 32]) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::ExpireHold {}.data(),
            card_escrow::accounts::ExpireHold {
                vault: user.vault,
                hold: hold_pda(&user.vault, &id),
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_close_hold(&self, user: &User, id: [u8; 32]) -> Instruction {
        self.ix_close_hold_raw(
            &self.operator.pubkey(),
            &hold_pda(&user.vault, &id),
            &self.operator.pubkey(),
        )
    }

    pub fn ix_close_hold_raw(
        &self,
        operator: &Pubkey,
        hold: &Pubkey,
        rent_payer: &Pubkey,
    ) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::CloseHold {}.data(),
            card_escrow::accounts::CloseHold {
                operator: *operator,
                config: config_pda(),
                hold: *hold,
                rent_payer: *rent_payer,
            }
            .to_account_metas(None),
        )
    }

    pub fn ix_refund(&self, user: &User, id: [u8; 32], amount: u64) -> Instruction {
        self.ix_refund_raw(
            &self.settlement_authority.pubkey(),
            user,
            &self.settlement_ata,
            id,
            amount,
        )
    }

    pub fn ix_refund_raw(
        &self,
        settlement_authority: &Pubkey,
        user: &User,
        settlement: &Pubkey,
        id: [u8; 32],
        amount: u64,
    ) -> Instruction {
        Instruction::new_with_bytes(
            card_escrow::ID,
            &card_escrow::instruction::Refund {
                refund_id: id,
                amount,
            }
            .data(),
            card_escrow::accounts::RefundToVault {
                settlement_authority: *settlement_authority,
                payer: *settlement_authority,
                config: config_pda(),
                mint: self.mint,
                vault: user.vault,
                vault_token_account: user.vault_ata,
                settlement_token_account: *settlement,
                refund: refund_pda(&user.vault, &id),
                token_program: self.token_program,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
        )
    }

    // ------------------------------------------------------- convenience ops

    pub fn authorize(&mut self, user: &User, id: [u8; 32], amount: u64) -> TxResult {
        let ix = self.ix_authorize(user, id, amount);
        let op = self.operator.insecure_clone();
        self.send(&[ix], &[&op])
    }

    pub fn capture(&mut self, user: &User, id: [u8; 32], amount: u64) -> TxResult {
        let ix = self.ix_capture(user, id, amount);
        let op = self.operator.insecure_clone();
        self.send(&[ix], &[&op])
    }

    pub fn release(&mut self, user: &User, id: [u8; 32]) -> TxResult {
        let ix = self.ix_release(user, id);
        let op = self.operator.insecure_clone();
        self.send(&[ix], &[&op])
    }

    pub fn refund(&mut self, user: &User, id: [u8; 32], amount: u64) -> TxResult {
        let ix = self.ix_refund(user, id, amount);
        let sa = self.settlement_authority.insecure_clone();
        self.send(&[ix], &[&sa])
    }

    pub fn set_paused(&mut self, paused: bool) -> TxResult {
        let ix = self.ix_set_paused(&self.admin.pubkey(), paused);
        let admin = self.admin.insecure_clone();
        self.send(&[ix], &[&admin])
    }

    pub fn withdraw(&mut self, user: &User, amount: u64) -> TxResult {
        let ix = self.ix_withdraw(user, amount);
        self.send(&[ix], &[&user.kp])
    }
}

/// Moves the default user out of the env so it can be borrowed alongside `&mut env`.
pub fn take_user(env: &mut Env) -> User {
    std::mem::replace(&mut env.user, User::placeholder())
}

/// Rewrites the ProgramData header so `admin` is the upgrade authority
/// (LiteSVM deploys programs with no authority).
pub fn set_upgrade_authority(svm: &mut LiteSVM, authority: Option<Pubkey>) {
    let address = program_data_pda();
    let mut acc = svm.get_account(&address).unwrap();
    // Layout: u32 enum tag | u64 slot | Option<Pubkey> (1 + 32 bytes).
    match authority {
        Some(a) => {
            acc.data[12] = 1;
            acc.data[13..45].copy_from_slice(a.as_ref());
        }
        None => {
            acc.data[12] = 0;
            acc.data[13..45].fill(0);
        }
    }
    svm.set_account(address, acc).unwrap();
}

// ------------------------------------------------------------- assertions

pub fn custom_code(res: &TxResult) -> Option<u32> {
    match res {
        Err(f) => match &f.err {
            TransactionError::InstructionError(_, InstructionError::Custom(c)) => Some(*c),
            _ => None,
        },
        Ok(_) => None,
    }
}

#[track_caller]
pub fn assert_escrow_err(res: TxResult, expected: EscrowError) {
    assert_custom(res, u32::from(expected));
}

#[track_caller]
pub fn assert_anchor_err(res: TxResult, expected: anchor_lang::error::ErrorCode) {
    assert_custom(res, u32::from(expected));
}

#[track_caller]
pub fn assert_custom(res: TxResult, expected: u32) {
    match &res {
        Ok(_) => panic!("expected custom error {expected}, transaction succeeded"),
        Err(f) => assert_eq!(
            custom_code(&res),
            Some(expected),
            "unexpected error {:?}\nlogs:\n{}",
            f.err,
            f.meta.logs.join("\n")
        ),
    }
}

/// System program `AccountAlreadyInUse` — what a duplicate `init` returns.
pub const ACCOUNT_ALREADY_IN_USE: u32 = 0;

#[track_caller]
pub fn assert_ok(res: TxResult) -> TransactionMetadata {
    match res {
        Ok(m) => m,
        Err(f) => panic!("tx failed: {:?}\nlogs:\n{}", f.err, f.meta.logs.join("\n")),
    }
}

/// Decodes all events of type `E` emitted via `emit!` in a transaction.
pub fn events<E: anchor_lang::Event + anchor_lang::AnchorDeserialize + Discriminator>(
    meta: &TransactionMetadata,
) -> Vec<E> {
    meta.logs
        .iter()
        .filter_map(|l| l.strip_prefix("Program data: "))
        .filter_map(|b| B64.decode(b).ok())
        .filter(|d| d.starts_with(E::DISCRIMINATOR))
        .map(|d| E::try_from_slice(&d[E::DISCRIMINATOR.len()..]).unwrap())
        .collect()
}
