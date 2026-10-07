#!/usr/bin/env bash
# Run a command inside the pinned Solana toolchain container.
#
#   scripts/tc.sh anchor build
#   scripts/tc.sh cargo test -p card-escrow
#
# Keypairs are mounted from ~/.config/solana (never from the repo).
# Cargo registry and target dir live in named volumes: the repo may sit on a
# shared folder where cargo is slow and hard links are unsupported.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${TOOLCHAIN_IMAGE:-card-escrow-toolchain:1}"
KEYS_DIR="${SOLANA_KEYS_DIR:-$HOME/.config/solana}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -t "$IMAGE" -f "$APP_DIR/docker/toolchain.Dockerfile" "$APP_DIR/docker"
fi

TTY_ARGS=()
[ -t 0 ] && [ -t 1 ] && TTY_ARGS=(-it)

exec docker run --rm "${TTY_ARGS[@]}" \
  --network host \
  -v "$APP_DIR:/work" \
  -v card-escrow-target:/work/target \
  -v card-escrow-cargo-registry:/opt/cargo/registry \
  -v card-escrow-cargo-git:/opt/cargo/git \
  -v "$KEYS_DIR:/root/.config/solana" \
  -e CARGO_TERM_COLOR=always \
  -e RUST_BACKTRACE \
  -e ANCHOR_WALLET \
  -w /work \
  "$IMAGE" "$@"
