"""Mapping between issuer identifiers (strings) and on-chain 32-byte ids.

Hashing gives fixed-size, uniformly distributed PDA seeds and avoids putting
issuer identifiers on-chain in clear text. Namespacing keeps auth and refund
ids from ever colliding.
"""

import hashlib


def auth_id_bytes(issuer_auth_id: str) -> bytes:
    return hashlib.sha256(b"card-escrow:auth:" + issuer_auth_id.encode()).digest()


def refund_id_bytes(issuer_refund_id: str) -> bytes:
    return hashlib.sha256(b"card-escrow:refund:" + issuer_refund_id.encode()).digest()
