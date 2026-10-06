#!/usr/bin/env bash
# Builds the program + IDL with the pinned toolchain.
#
# Anchor 1.2.1 defaults to platform-tools v1.57 / SBPF v3, but SBPF v3 is not
# activated on mainnet-beta or devnet (feature BUwGLeF3...). We pin the
# platform-tools of Agave 3.1.14 (v1.52) and SBPF v0 — cargo-build-sbf's own
# default, which is also what `solana-verify build` reproduces.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
TOOLS_VERSION="${SBF_TOOLS_VERSION:-v1.52}"
ARCH="${SBF_ARCH:-v0}"
exec scripts/tc.sh bash -c "
  set -euo pipefail
  mkdir -p target/deploy
  if [ -f /root/.config/solana/card-escrow/program.json ]; then
    cp /root/.config/solana/card-escrow/program.json target/deploy/card_escrow-keypair.json
  fi
  anchor build --tools-version $TOOLS_VERSION --arch $ARCH $*
  mkdir -p idl && cp target/idl/card_escrow.json idl/card_escrow.json
  ls -l target/deploy/card_escrow.so
"
