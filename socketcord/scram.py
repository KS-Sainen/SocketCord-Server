"""Challenge-response password authentication for SocketCord.

The README's rule is "never send a plaintext password". This module does the
SCRAM-SHA-256 key math (RFC 5802 / RFC 7677) with SocketCord-specific
AuthMessages, plus an ephemeral Diffie-Hellman exchange (RFC 3526 group 14)
for the two moments when a password verifier has to travel: registration and
password change. As a result:

* the password never crosses the socket, in any form;
* the verifier (StoredKey, ServerKey) never crosses the socket in the clear,
  so someone who records traffic cannot turn a later login into ClientKey;
* the server stores only (salt, iterations, StoredKey, ServerKey), so a
  database leak hands out neither passwords nor usable logins;
* every login proof is bound to fresh nonces from both sides, so a recorded
  proof cannot be replayed;
* the server proves it holds the verifier too (ServerSignature), so the
  client can detect a fake server.

Key math (H = SHA-256, HMAC = HMAC-SHA-256):

    SaltedPassword  = PBKDF2(password, salt, iterations)
    ClientKey       = HMAC(SaltedPassword, "Client Key")
    StoredKey       = H(ClientKey)
    ServerKey       = HMAC(SaltedPassword, "Server Key")
    ClientSignature = HMAC(StoredKey, AuthMessage)
    ClientProof     = ClientKey XOR ClientSignature
    ServerSignature = HMAC(ServerKey, AuthMessage)

The server checks a proof by recovering ClientKey = ClientProof XOR
ClientSignature and testing H(ClientKey) == StoredKey. That only works for
someone who knows StoredKey, which is why StoredKey must stay secret.

Verifier transfer: both sides pick a fresh DH key pair; from the shared DH
secret, HKDF derives a 64-byte one-time pad and a 32-byte MAC key. The client
sends (StoredKey || ServerKey) XOR pad, plus an HMAC tag over the whole
exchange. Each DH secret is used for exactly one message.

This file uses only the standard library. The frontend can copy it as-is and
drive the three client flows at the bottom (LoginFlow, RegisterFlow,
ChangePasswordFlow).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 18                 # 18 random bytes -> 24 base64 characters
DEFAULT_ITERATIONS = 600_000   # OWASP 2023 figure for PBKDF2-HMAC-SHA256
MIN_ITERATIONS = 4_096         # RFC 7677 minimum; clients refuse anything lower
MAX_ITERATIONS = 10_000_000    # clients refuse anything higher (a hostile server could stall them)

LOGIN_LABEL = "socketcord-login-v1"
REGISTER_LABEL = "socketcord-register-v2"
PASSWORD_LABEL = "socketcord-password-v2"

# RFC 3526, 2048-bit MODP group ("group 14"), generator 2:
#   p = 2^2048 - 2^1984 - 1 + 2^64 * (floor(2^1918 * pi) + 124476)
# p and (p-1)/2 are both prime (a "safe prime"); tests/test_scram.py checks
# this constant against the formula.
DH_GROUP = "modp2048"
DH_GENERATOR = 2
DH_PRIME = int(
    "FFFFFFFF FFFFFFFF C90FDAA2 2168C234 C4C6628B 80DC1CD1 29024E08 8A67CC74 "
    "020BBEA6 3B139B22 514A0879 8E3404DD EF9519B3 CD3A431B 302B0A6D F25F1437 "
    "4FE1356D 6D51C245 E485B576 625E7EC6 F44C42E9 A637ED6B 0BFF5CB6 F406B7ED "
    "EE386BFB 5A899FA5 AE9F2411 7C4B1FE6 49286651 ECE45B3D C2007CB8 A163BF05 "
    "98DA4836 1C55D39A 69163FA8 FD24CF5F 83655D23 DCA3AD96 1C62F356 208552BB "
    "9ED52907 7096966D 670C354E 4ABC9804 F1746C08 CA18217C 32905E46 2E36CE3B "
    "E39E772C 180E8603 9B2783A2 EC07A28F B5C55DF0 6F4C52C9 DE2BCBF6 95581718 "
    "3995497C EA956AE5 15D22618 98FA0510 15728E5A 8AACAA68 FFFFFFFF FFFFFFFF "
    .replace(" ", ""), 16)
DH_LEN = 256                   # bytes in a public value or shared secret
DH_EXPONENT_BITS = 256         # private exponent size (twice the ~112-bit group strength)
WRAPPED_LEN = 2 * KEY_LEN      # StoredKey || ServerKey


# --------------------------------------------------------------------------- encoding

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


def new_nonce() -> str:
    return b64e(os.urandom(NONCE_LEN))


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def canonical_username(username: str) -> str:
    """Usernames are case-insensitive. Both sides put this form in AuthMessage."""
    return username.strip().lower()


# --------------------------------------------------------------------------- key math

def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def salted_password(password: str, salt: bytes, iterations: int) -> bytes:
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


def client_proof(client_key: bytes, stored_key: bytes, auth_message: bytes) -> bytes:
    return _xor(client_key, _hmac(stored_key, auth_message))


def verify_client_proof(stored_key: bytes, auth_message: bytes, proof: bytes) -> bool:
    if len(proof) != KEY_LEN or len(stored_key) != KEY_LEN:
        return False
    recovered_client_key = _xor(proof, _hmac(stored_key, auth_message))
    return hmac.compare_digest(hashlib.sha256(recovered_client_key).digest(), stored_key)


def server_signature(server_key: bytes, auth_message: bytes) -> bytes:
    return _hmac(server_key, auth_message)


def verify_server_signature(server_key: bytes, auth_message: bytes, signature: bytes) -> bool:
    return hmac.compare_digest(server_signature(server_key, auth_message), signature)


# --------------------------------------------------------------------------- Diffie-Hellman wrap

def dh_keypair() -> Tuple[int, int]:
    """Returns (private exponent, public value g^x mod p)."""
    x = secrets.randbits(DH_EXPONENT_BITS) | (1 << (DH_EXPONENT_BITS - 1))
    return x, pow(DH_GENERATOR, x, DH_PRIME)


def dh_encode(public: int) -> str:
    return b64e(public.to_bytes(DH_LEN, "big"))


def dh_decode(text: str) -> int:
    """Parses and checks a peer's public value (rejects 0, 1, p-1 and out-of-range values)."""
    value = int.from_bytes(b64d(text, DH_LEN), "big")
    if not 2 <= value <= DH_PRIME - 2:
        raise ValueError("invalid Diffie-Hellman public value")
    return value


def dh_shared_secret(private: int, peer_public: int) -> bytes:
    z = pow(peer_public, private, DH_PRIME)
    if z in (0, 1, DH_PRIME - 1):
        raise ValueError("degenerate Diffie-Hellman secret")
    return z.to_bytes(DH_LEN, "big")


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF."""
    prk = _hmac(salt, ikm)
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = _hmac(prk, block + info + bytes([counter]))
        okm += block
        counter += 1
    return okm[:length]


def _wrap_keys(shared: bytes, auth_message: bytes) -> Tuple[bytes, bytes]:
    okm = hkdf_sha256(shared, b"socketcord-verifier-wrap", auth_message, WRAPPED_LEN + KEY_LEN)
    return okm[:WRAPPED_LEN], okm[WRAPPED_LEN:]


def wrap_verifier(shared: bytes, auth_message: bytes, stored_key: bytes, server_key: bytes) -> bytes:
    """Encrypts StoredKey || ServerKey under a one-time pad derived from the DH secret."""
    pad, _ = _wrap_keys(shared, auth_message)
    return _xor(stored_key + server_key, pad)


def wrap_tag(shared: bytes, full_auth_message: bytes, auth_message: bytes) -> bytes:
    _, mac_key = _wrap_keys(shared, auth_message)
    return _hmac(mac_key, full_auth_message)


def unwrap_verifier(shared: bytes, auth_message: bytes, full_auth_message: bytes,
                    wrapped: bytes, tag: bytes) -> Optional[Tuple[bytes, bytes]]:
    """Returns (StoredKey, ServerKey), or None if the tag does not match."""
    if len(wrapped) != WRAPPED_LEN or len(tag) != KEY_LEN:
        return None
    pad, mac_key = _wrap_keys(shared, auth_message)
    if not hmac.compare_digest(_hmac(mac_key, full_auth_message), tag):
        return None
    plain = _xor(wrapped, pad)
    return plain[:KEY_LEN], plain[KEY_LEN:]


# --------------------------------------------------------------------------- AuthMessages
# Both sides must build these byte-for-byte identically. Every field is
# base64, a UUID, a decimal number, or a canonical username, so a comma can
# never appear inside a field.

def _join(*fields: object) -> bytes:
    return ",".join(str(f) for f in fields).encode("utf-8")


def with_ciphertext(auth_message: bytes, wrapped_b64: str) -> bytes:
    """The AuthMessage that the tag, the proof and the ServerSignature cover."""
    return auth_message + b"," + wrapped_b64.encode("ascii")


def login_auth_message(username: str, client_nonce: str, server_nonce: str,
                       salt_b64: str, iterations: int, challenge_id: str) -> bytes:
    return _join(LOGIN_LABEL, canonical_username(username), client_nonce, server_nonce,
                 salt_b64, iterations, challenge_id)


def register_auth_message(username: str, client_nonce: str, server_nonce: str, salt_b64: str,
                          iterations: int, challenge_id: str, server_dh_b64: str, client_dh_b64: str) -> bytes:
    return _join(REGISTER_LABEL, canonical_username(username), client_nonce, server_nonce,
                 salt_b64, iterations, challenge_id, DH_GROUP, server_dh_b64, client_dh_b64)


def password_auth_message(user_id: str, client_nonce: str, server_nonce: str, salt_b64: str,
                          iterations: int, challenge_id: str, new_salt_b64: str, new_iterations: int,
                          server_dh_b64: str, client_dh_b64: str) -> bytes:
    return _join(PASSWORD_LABEL, str(user_id).lower(), client_nonce, server_nonce, salt_b64,
                 iterations, challenge_id, new_salt_b64, new_iterations, DH_GROUP,
                 server_dh_b64, client_dh_b64)


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


# --------------------------------------------------------------------------- client flows
# Each flow object is used once, in three calls:
#   start()          -> dict to send as the first request
#   respond(reply)   -> dict to send after the server's "challenge" reply
#   verify(reply)    -> True if the server's "done" reply proves it holds the verifier
# respond() wipes the password from the object and refuses to run twice, so if a
# step fails (expired challenge, wrong password) create a NEW flow to try again.

CLIENT_MIN_ITERATIONS = DEFAULT_ITERATIONS   # a fake server could otherwise ask for cheap PBKDF2


class _Flow:
    _expected_signature: Optional[bytes] = None

    def __init__(self, min_iterations: int) -> None:
        self.min_iterations = check_iterations(min_iterations)
        self._used = False

    def _begin(self) -> None:
        if self._used:
            raise RuntimeError("this flow was already used; create a new one to try again")
        self._used = True

    def _iterations(self, value: object) -> int:
        iterations = check_iterations(value)
        if iterations < self.min_iterations:
            raise ValueError(f"the server asked for only {iterations} PBKDF2 iterations; "
                             f"this client requires at least {self.min_iterations}")
        return iterations

    def verify(self, done: dict) -> bool:
        if self._expected_signature is None or "server_signature" not in done:
            return False
        try:
            return hmac.compare_digest(b64d(done["server_signature"]), self._expected_signature)
        except ValueError:
            return False


def _check_group(challenge: dict) -> int:
    if challenge.get("dh_group") != DH_GROUP:
        raise ValueError(f"server offered DH group {challenge.get('dh_group')!r}, expected {DH_GROUP!r}")
    return dh_decode(challenge["dh_public"])


class LoginFlow(_Flow):
    """POST users/login, twice."""

    def __init__(self, username: str, password: str, min_iterations: int = CLIENT_MIN_ITERATIONS) -> None:
        super().__init__(min_iterations)
        self.username = username.strip()
        self._password = password
        self.client_nonce = new_nonce()

    def start(self) -> dict:
        return {"username": self.username, "client_nonce": self.client_nonce}

    def respond(self, challenge: dict) -> dict:
        self._begin()
        password, self._password = self._password, ""
        salt_b64 = challenge["salt"]
        iterations = self._iterations(challenge["iterations"])
        client_key, stored_key, server_key = derive_keys(password, b64d(salt_b64), iterations)
        am = login_auth_message(self.username, self.client_nonce, challenge["server_nonce"],
                                salt_b64, iterations, challenge["challenge_id"])
        self._expected_signature = server_signature(server_key, am)
        return {"challenge_id": challenge["challenge_id"],
                "client_proof": b64e(client_proof(client_key, stored_key, am))}


AUTO_DEVICE = object()   # RegisterFlow default: send device_fingerprint()


class RegisterFlow(_Flow):
    """POST users, twice. The server picks the salt; the client derives the verifier
    and sends it encrypted under a fresh DH key."""

    def __init__(self, username: str, password: str, device_id=AUTO_DEVICE,
                 min_iterations: int = CLIENT_MIN_ITERATIONS) -> None:
        super().__init__(min_iterations)
        self.username = username.strip()
        self._password = password
        # Default: a hash of this machine's MAC. Pass None to send no device id.
        self.device_id = device_fingerprint() if device_id is AUTO_DEVICE else device_id
        self.client_nonce = new_nonce()

    def start(self) -> dict:
        msg = {"username": self.username, "client_nonce": self.client_nonce}
        if self.device_id:
            msg["device_id"] = self.device_id
        return msg

    def respond(self, challenge: dict) -> dict:
        self._begin()
        password, self._password = self._password, ""
        salt_b64 = challenge["salt"]
        iterations = self._iterations(challenge["iterations"])
        server_dh = _check_group(challenge)
        verifier, _ = make_verifier(password, b64d(salt_b64), iterations)
        private, public = dh_keypair()
        shared = dh_shared_secret(private, server_dh)
        client_dh_b64 = dh_encode(public)
        am = register_auth_message(self.username, self.client_nonce, challenge["server_nonce"], salt_b64,
                                   iterations, challenge["challenge_id"], challenge["dh_public"], client_dh_b64)
        wrapped_b64 = b64e(wrap_verifier(shared, am, verifier.stored_key, verifier.server_key))
        full = with_ciphertext(am, wrapped_b64)
        self._expected_signature = server_signature(verifier.server_key, full)
        return {"challenge_id": challenge["challenge_id"],
                "dh_public": client_dh_b64,
                "wrapped_verifier": wrapped_b64,
                "wrap_tag": b64e(wrap_tag(shared, full, am))}


class ChangePasswordFlow(_Flow):
    """PATCH users/profile/password, twice, on a logged-in session.

    The old-password proof covers both DH public values and the encrypted new
    verifier, so nobody in the middle can swap in keys of their own.
    """

    def __init__(self, user_id: str, old_password: str, new_password: str,
                 min_iterations: int = CLIENT_MIN_ITERATIONS) -> None:
        super().__init__(min_iterations)
        self.user_id = str(user_id)
        self._old = old_password
        self._new = new_password
        self.client_nonce = new_nonce()

    def start(self) -> dict:
        return {"client_nonce": self.client_nonce}

    def respond(self, challenge: dict) -> dict:
        self._begin()
        old, new = self._old, self._new
        self._old = self._new = ""
        salt_b64, new_salt_b64 = challenge["salt"], challenge["new_salt"]
        iterations = self._iterations(challenge["iterations"])
        new_iterations = self._iterations(challenge["new_iterations"])
        server_dh = _check_group(challenge)
        old_ck, old_sk, old_svk = derive_keys(old, b64d(salt_b64), iterations)
        new_verifier, _ = make_verifier(new, b64d(new_salt_b64), new_iterations)
        private, public = dh_keypair()
        shared = dh_shared_secret(private, server_dh)
        client_dh_b64 = dh_encode(public)
        am = password_auth_message(self.user_id, self.client_nonce, challenge["server_nonce"], salt_b64,
                                   iterations, challenge["challenge_id"], new_salt_b64, new_iterations,
                                   challenge["dh_public"], client_dh_b64)
        wrapped_b64 = b64e(wrap_verifier(shared, am, new_verifier.stored_key, new_verifier.server_key))
        full = with_ciphertext(am, wrapped_b64)
        self._expected_signature = server_signature(old_svk, full)
        return {"challenge_id": challenge["challenge_id"],
                "dh_public": client_dh_b64,
                "wrapped_verifier": wrapped_b64,
                "wrap_tag": b64e(wrap_tag(shared, full, am)),
                "client_proof": b64e(client_proof(old_ck, old_sk, full))}
