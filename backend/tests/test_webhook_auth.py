import pytest

from escrow_backend.webhook_auth import SignatureError, sign, verify

SECRET = b"s3cret"
BODY = b'{"auth_id":"a1"}'
NOW = 1_760_000_000


def test_roundtrip() -> None:
    header = sign(SECRET, BODY, NOW)
    assert verify(SECRET, BODY, header, 300, now=NOW + 10) == NOW


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "garbage",
        "t=,v1=abc",
        f"t={NOW}",
        "v1=deadbeef",
        f"t=notanumber,v1={'0' * 64}",
        f"t={NOW},t={NOW},v1={'0' * 64}",
        f"t={NOW},v1={'0' * 64}",
    ],
)
def test_rejects_malformed_or_wrong(header: str | None) -> None:
    with pytest.raises(SignatureError):
        verify(SECRET, BODY, header, 300, now=NOW)


def test_rejects_tampered_body() -> None:
    header = sign(SECRET, BODY, NOW)
    with pytest.raises(SignatureError, match="mismatch"):
        verify(SECRET, BODY + b" ", header, 300, now=NOW)


def test_rejects_wrong_secret() -> None:
    header = sign(b"other", BODY, NOW)
    with pytest.raises(SignatureError):
        verify(SECRET, BODY, header, 300, now=NOW)


@pytest.mark.parametrize("skew", [301, -301, 10_000])
def test_rejects_stale_and_future_timestamps(skew: int) -> None:
    header = sign(SECRET, BODY, NOW)
    with pytest.raises(SignatureError, match="tolerance"):
        verify(SECRET, BODY, header, 300, now=NOW + skew)


def test_accepts_any_of_multiple_v1_for_rotation() -> None:
    good = sign(SECRET, BODY, NOW).split("v1=")[1]
    header = f"t={NOW},v1={'0' * 64},v1={good}"
    assert verify(SECRET, BODY, header, 300, now=NOW) == NOW


def test_sign_defaults_to_current_time() -> None:
    header = sign(SECRET, BODY)
    assert verify(SECRET, BODY, header, 5) > 0
