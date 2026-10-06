"""HMAC webhook signatures.

Header: `X-Issuer-Signature: t=<unix seconds>,v1=<hex hmac_sha256(secret, f"{t}." + body)>`

Verification is fail-closed: any parse error, missing part, stale timestamp or
mismatch raises `SignatureError`; there is no code path that accepts a request
without a valid signature.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Issuer-Signature"


class SignatureError(Exception):
    pass


def sign(secret: bytes, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(
    secret: bytes, body: bytes, header: str | None, tolerance_s: int, now: float | None = None
) -> int:
    """Returns the signed timestamp or raises SignatureError."""
    try:
        if not header:
            raise SignatureError("missing signature header")
        parts: dict[str, list[str]] = {}
        for item in header.split(","):
            k, _, v = item.strip().partition("=")
            if not k or not v:
                raise SignatureError("malformed signature header")
            parts.setdefault(k, []).append(v)
        if len(parts.get("t", [])) != 1:
            raise SignatureError("missing or repeated timestamp")
        ts = int(parts["t"][0])
        candidates = parts.get("v1", [])
        if not candidates:
            raise SignatureError("missing v1 signature")
        now = time.time() if now is None else now
        if abs(now - ts) > tolerance_s:
            raise SignatureError("timestamp outside tolerance")
        expected = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        # Several v1 entries allow secret rotation on the issuer side.
        if not any(hmac.compare_digest(expected, c) for c in candidates):
            raise SignatureError("signature mismatch")
        return ts
    except SignatureError:
        raise
    except Exception as e:  # any unexpected parsing problem is a rejection
        raise SignatureError(f"invalid signature header: {e}") from e
