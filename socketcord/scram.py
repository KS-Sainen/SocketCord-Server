"""Password hashing for SocketCord.

The wire protocol sends the password itself (contract v2, see docs/PROTOCOL.md
section 2): the socket is not encrypted, so anyone who can watch the traffic
can read it. What this module does is keep the *stored* form safe:

    SaltedPassword = PBKDF2-HMAC-SHA256(password, salt, iterations)
    ClientKey      = HMAC(SaltedPassword, "Client Key")
    StoredKey      = SHA-256(ClientKey)
    ServerKey      = HMAC(SaltedPassword, "Server Key")

and `users.password_hash` holds (salt, iterations, StoredKey, ServerKey) in
the string format PostgreSQL itself uses for SCRAM-SHA-256 passwords. That is
why this file is still called scram.py and why the database schema did not
change when the challenge-response flow was removed: the column already held
a salted PBKDF2 hash. The password itself is never stored.

Checking a password means recomputing StoredKey with the row's own salt and
iteration count and comparing it in constant time (`verify_password`).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

KEY_LEN = 32
SALT_LEN = 16
DEFAULT_ITERATIONS = 600_000   # OWASP 2023 figure for PBKDF2-HMAC-SHA256
MIN_ITERATIONS = 4_096         # RFC 7677 minimum
MAX_ITERATIONS = 10_000_000    # beyond this, one login would stall a worker thread


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str, expect_len: Optional[int] = None) -> bytes:
    if not isinstance(text, str):
        raise ValueError("expected a base64 string")
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        raise ValueError("invalid base64") from None
    if expect_len is not None and len(raw) != expect_len:
        raise ValueError(f"expected {expect_len} bytes, got {len(raw)}")
    return raw


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def canonical_username(username: str) -> str:
    """Usernames are case-insensitive; this is the form rate limits count per."""
    return username.strip().lower()


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def salted_password(password: str, salt: bytes, iterations: int) -> bytes:
    # NFKC first, so the same password typed on two keyboards (or with a composed
    # vs decomposed accent) hashes the same.
    normalized = unicodedata.normalize("NFKC", password)
    return hashlib.pbkdf2_hmac("sha256", normalized.encode("utf-8"), salt, iterations, dklen=KEY_LEN)


def derive_keys(password: str, salt: bytes, iterations: int) -> Tuple[bytes, bytes, bytes]:
    """Returns (ClientKey, StoredKey, ServerKey)."""
    sp = salted_password(password, salt, iterations)
    client_key = _hmac(sp, b"Client Key")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = _hmac(sp, b"Server Key")
    return client_key, stored_key, server_key


@dataclass(frozen=True)
class Verifier:
    """What the server stores instead of a password."""

    salt: bytes
    iterations: int
    stored_key: bytes
    server_key: bytes


def make_verifier(password: str, salt: Optional[bytes] = None,
                  iterations: int = DEFAULT_ITERATIONS) -> Tuple[Verifier, bytes]:
    """Returns (verifier, ClientKey)."""
    salt = salt if salt is not None else new_salt()
    client_key, stored_key, server_key = derive_keys(password, salt, iterations)
    return Verifier(salt, iterations, stored_key, server_key), client_key


def verify_password(password: str, salt: bytes, iterations: int, stored_key: bytes) -> bool:
    """True if `password` is the one (salt, iterations, StoredKey) was made from."""
    if len(stored_key) != KEY_LEN:
        return False
    _, recomputed, _ = derive_keys(password, salt, iterations)
    return hmac.compare_digest(recomputed, stored_key)


VERIFIER_PREFIX = "SCRAM-SHA-256"


def encode_verifier(salt: bytes, iterations: int, stored_key: bytes, server_key: bytes) -> str:
    """The verifier as one "password hash" string, in the format PostgreSQL itself
    uses for SCRAM passwords: SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>."""
    return f"{VERIFIER_PREFIX}${int(iterations)}:{b64e(salt)}${b64e(stored_key)}:{b64e(server_key)}"


def decode_verifier(text: str) -> Verifier:
    """Inverse of encode_verifier(). Raises ValueError on anything malformed."""
    try:
        prefix, params, keys = text.split("$")
        iterations_text, salt_b64 = params.split(":")
        stored_b64, server_b64 = keys.split(":")
        if prefix != VERIFIER_PREFIX or not iterations_text.isdigit():
            raise ValueError("not a SCRAM-SHA-256 verifier")
        return Verifier(b64d(salt_b64), int(iterations_text), b64d(stored_b64, KEY_LEN), b64d(server_b64, KEY_LEN))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed password hash: {exc}") from None


def check_iterations(iterations: object) -> int:
    if isinstance(iterations, bool) or not isinstance(iterations, int) \
            or not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ValueError(f"iterations must be an int in {MIN_ITERATIONS}..{MAX_ITERATIONS}")
    return iterations


def device_fingerprint() -> str:
    """A stable per-machine id for the one-account-per-day rule.

    Hashes the MAC address from uuid.getnode(), so the server never sees the
    MAC itself. The server cannot read a client's MAC across routers (it only
    reaches the first hop), so the client has to volunteer this value.
    """
    return hashlib.sha256(f"socketcord-device:{uuid.getnode():012x}".encode()).hexdigest()[:32]
