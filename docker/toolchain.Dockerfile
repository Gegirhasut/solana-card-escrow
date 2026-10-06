# Reproducible Solana toolchain image.
#
# Agave release binaries require glibc >= 2.34, so the toolchain runs in a
# container regardless of the host distro. Versions are pinned here and in
# Anchor.toml / rust-toolchain.toml; bump them together.
FROM ubuntu:24.04

ARG RUST_VERSION=1.91.1
ARG AGAVE_VERSION=3.1.14
ARG ANCHOR_VERSION=1.2.1

ENV DEBIAN_FRONTEND=noninteractive \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:/opt/solana/active_release/bin:/usr/local/bin:/usr/bin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config libssl-dev libudev-dev clang curl ca-certificates \
        git bzip2 jq \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal \
        --default-toolchain ${RUST_VERSION} --component clippy,rustfmt \
    && chmod -R a+rwX /opt/rustup /opt/cargo

RUN mkdir -p /opt/solana && curl -sSfL \
        https://github.com/anza-xyz/agave/releases/download/v${AGAVE_VERSION}/solana-release-x86_64-unknown-linux-gnu.tar.bz2 \
        | tar -xj -C /opt/solana \
    && mv /opt/solana/solana-release /opt/solana/active_release \
    && solana --version

RUN curl -sSfL -o /usr/local/bin/anchor \
        https://github.com/otter-sec/anchor/releases/download/v${ANCHOR_VERSION}/anchor-${ANCHOR_VERSION}-x86_64-unknown-linux-gnu \
    && chmod +x /usr/local/bin/anchor && anchor --version

# Pre-fetch SBF platform tools so the first build does not download them.
RUN cargo new --lib /tmp/warm && cd /tmp/warm \
    && printf '[lib]\ncrate-type=["cdylib"]\n' >> Cargo.toml \
    && cargo build-sbf >/dev/null 2>&1; rm -rf /tmp/warm; \
    chmod -R a+rwX /opt/solana /root 2>/dev/null; ls /root/.cache/solana || true

WORKDIR /work
