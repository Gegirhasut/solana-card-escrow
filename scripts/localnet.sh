#!/usr/bin/env bash
# Starts a local validator (in the toolchain container) with card_escrow
# preloaded as an upgradeable program whose upgrade authority is the deployer,
# so `initialize_config` can be called by the deployer.
#
#   scripts/localnet.sh start | stop | logs
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${TOOLCHAIN_IMAGE:-card-escrow-toolchain:1}"
KEYS_DIR="${SOLANA_KEYS_DIR:-$HOME/.config/solana}"
NAME=card-escrow-validator
PROGRAM_ID=$(grep -oP 'declare_id!\("\K[^"]+' "$APP_DIR/programs/card_escrow/src/lib.rs")

case "${1:-start}" in
  start)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    if [ ! -f "$KEYS_DIR/card-escrow/deployer.json" ]; then
      # First run: the deployer is the program's upgrade authority on localnet.
      mkdir -p -m 700 "$KEYS_DIR/card-escrow"
      docker run --rm --user "$(id -u):$(id -g)" -v "$KEYS_DIR:/keys" "$IMAGE" \
        solana-keygen new --no-bip39-passphrase --silent -o /keys/card-escrow/deployer.json
    fi
    DEPLOYER=$(docker run --rm -v "$KEYS_DIR:/root/.config/solana:ro" "$IMAGE" \
      solana-keygen pubkey /root/.config/solana/card-escrow/deployer.json)
    docker run -d --name "$NAME" --network host \
      -v card-escrow-target:/work/target:ro \
      "$IMAGE" solana-test-validator --reset --quiet --ledger /tmp/ledger \
        --rpc-port 8899 --limit-ledger-size 50000000 \
        --upgradeable-program "$PROGRAM_ID" /work/target/deploy/card_escrow.so "$DEPLOYER" \
        --mint "$DEPLOYER" >/dev/null
    for _ in $(seq 1 60); do
      if curl -s -X POST -H 'content-type: application/json' \
          -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}' http://127.0.0.1:8899 | grep -q ok; then
        echo "validator up: http://127.0.0.1:8899 (program $PROGRAM_ID, upgrade authority $DEPLOYER)"
        exit 0
      fi
      sleep 1
    done
    echo "validator did not become healthy" >&2; docker logs "$NAME" | tail -20; exit 1 ;;
  stop) docker rm -f "$NAME" >/dev/null && echo stopped ;;
  logs) docker logs -f "$NAME" ;;
  *) echo "usage: $0 start|stop|logs" >&2; exit 2 ;;
esac
