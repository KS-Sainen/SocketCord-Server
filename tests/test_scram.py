"""Challenge-response math and the verifier wrap (no network)."""
import random
from decimal import ROUND_FLOOR, Decimal, getcontext

import pytest

from socketcord import scram


def test_rfc7677_vector():
    """SCRAM-SHA-256 example exchange from RFC 7677 section 3 (user/pencil)."""
    salt = scram.b64d("W22ZaJ0SNY7soEsUEjb6gQ==")
    client_key, stored_key, server_key = scram.derive_keys("pencil", salt, 4096)
    auth_message = (b"n=user,r=rOprNGfwEbeRWgbNEkqO,"
                    b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
                    b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0")
    proof = scram.client_proof(client_key, stored_key, auth_message)
    assert scram.b64e(proof) == "dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
    assert scram.b64e(scram.server_signature(server_key, auth_message)) == \
        "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    assert scram.verify_client_proof(stored_key, auth_message, proof)


def test_hkdf_rfc5869_vector():
    okm = scram.hkdf_sha256(bytes([0x0B] * 22), bytes(range(0x0D)), bytes(range(0xF0, 0xFA)), 42)
    assert okm.hex() == ("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
                         "34007208d5b887185865")


def _is_probable_prime(n: int, rounds: int = 16) -> bool:
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    rng = random.Random(7)
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _pi(digits: int) -> Decimal:
    getcontext().prec = digits + 20
    eps = Decimal(10) ** -(digits + 15)

    def arctan_inv(n: int) -> Decimal:
        x, term, total, k, sign = Decimal(n), Decimal(1) / n, Decimal(1) / n, 1, -1
        while True:
            term /= x * x
            delta = term / (2 * k + 1)
            if delta < eps:
                return total
            total += sign * delta
            sign, k = -sign, k + 1

    return 4 * (4 * arctan_inv(5) - arctan_inv(239))   # Machin's formula


def test_dh_group_is_rfc3526_group14():
    pi = _pi(700)
    getcontext().prec = 720
    floor_term = int((Decimal(2) ** 1918 * pi).to_integral_value(rounding=ROUND_FLOOR))
    assert scram.DH_PRIME == 2 ** 2048 - 2 ** 1984 - 1 + 2 ** 64 * (floor_term + 124476)
    assert _is_probable_prime(scram.DH_PRIME) and _is_probable_prime((scram.DH_PRIME - 1) // 2)


def test_dh_rejects_bad_public_values():
    p = scram.DH_PRIME
    for bad in (0, 1, p - 1, p, p + 1):      # all fit in 256 bytes, all must be refused
        with pytest.raises(ValueError):
            scram.dh_decode(scram.b64e(bad.to_bytes(scram.DH_LEN, "big")))
    assert scram.dh_decode(scram.b64e((2).to_bytes(scram.DH_LEN, "big"))) == 2
    with pytest.raises(ValueError):
        scram.dh_decode(scram.b64e(b"\x02" * 10))    # wrong length


def _server_challenge(salt: bytes, iterations: int = 4096, dh: bool = False) -> dict:
    ch = {"challenge_id": "c0ffee00-0000-4000-8000-000000000001", "salt": scram.b64e(salt),
          "iterations": iterations, "server_nonce": scram.new_nonce()}
    if dh:
        private, public = scram.dh_keypair()
        ch.update(dh_group=scram.DH_GROUP, dh_public=scram.dh_encode(public), _private=private)
    return ch


def test_login_flow_both_sides():
    verifier, _ = scram.make_verifier("hunter2", iterations=4096)
    flow = scram.LoginFlow("Alice", "hunter2", min_iterations=4096)
    ch = _server_challenge(verifier.salt)
    answer = flow.respond(ch)
    am = scram.login_auth_message("alice", flow.client_nonce, ch["server_nonce"], ch["salt"], 4096,
                                  ch["challenge_id"])
    assert scram.verify_client_proof(verifier.stored_key, am, scram.b64d(answer["client_proof"]))
    sig = scram.server_signature(verifier.server_key, am)
    assert flow.verify({"server_signature": scram.b64e(sig)})
    assert not flow.verify({"server_signature": scram.b64e(b"\0" * 32)})


def test_wrong_password_fails():
    verifier, _ = scram.make_verifier("right", iterations=4096)
    flow = scram.LoginFlow("bob", "wrong", min_iterations=4096)
    ch = _server_challenge(verifier.salt)
    answer = flow.respond(ch)
    am = scram.login_auth_message("bob", flow.client_nonce, ch["server_nonce"], ch["salt"], 4096,
                                  ch["challenge_id"])
    assert not scram.verify_client_proof(verifier.stored_key, am, scram.b64d(answer["client_proof"]))


def test_proof_is_bound_to_the_nonces():
    """A proof captured from one login is useless for the next challenge (replay)."""
    verifier, _ = scram.make_verifier("pw", iterations=4096)
    flow = scram.LoginFlow("eve", "pw", min_iterations=4096)
    ch1 = _server_challenge(verifier.salt)
    answer = flow.respond(ch1)
    ch2 = dict(ch1, server_nonce=scram.new_nonce())
    am2 = scram.login_auth_message("eve", flow.client_nonce, ch2["server_nonce"], ch2["salt"], 4096,
                                   ch2["challenge_id"])
    assert not scram.verify_client_proof(verifier.stored_key, am2, scram.b64d(answer["client_proof"]))


def _server_register_view(flow: scram.RegisterFlow, ch: dict, answer: dict):
    am = scram.register_auth_message(flow.username, flow.client_nonce, ch["server_nonce"], ch["salt"],
                                     ch["iterations"], ch["challenge_id"], ch["dh_public"], answer["dh_public"])
    full = scram.with_ciphertext(am, answer["wrapped_verifier"])
    shared = scram.dh_shared_secret(ch["_private"], scram.dh_decode(answer["dh_public"]))
    return am, full, shared


def test_register_flow_sends_the_verifier_encrypted():
    salt = scram.new_salt()
    flow = scram.RegisterFlow("Carol", "s3cret", device_id=None, min_iterations=4096)
    ch = _server_challenge(salt, dh=True)
    answer = flow.respond(ch)
    expected, _ = scram.make_verifier("s3cret", salt, 4096)
    am, full, shared = _server_register_view(flow, ch, answer)
    keys = scram.unwrap_verifier(shared, am, full, scram.b64d(answer["wrapped_verifier"]),
                                 scram.b64d(answer["wrap_tag"]))
    assert keys == (expected.stored_key, expected.server_key)
    # What a sniffer sees contains neither key, raw or base64, and no proof to XOR with.
    wire = repr(answer).encode()
    for secret in (expected.stored_key, expected.server_key):
        assert secret not in wire and scram.b64e(secret).encode() not in wire
    assert set(answer) == {"challenge_id", "dh_public", "wrapped_verifier", "wrap_tag"}
    assert flow.verify({"server_signature": scram.b64e(scram.server_signature(expected.server_key, full))})


def test_register_wrap_detects_tampering():
    flow = scram.RegisterFlow("dan", "pw", device_id=None, min_iterations=4096)
    ch = _server_challenge(scram.new_salt(), dh=True)
    answer = flow.respond(ch)
    am, full, shared = _server_register_view(flow, ch, answer)
    wrapped = bytearray(scram.b64d(answer["wrapped_verifier"]))
    wrapped[0] ^= 1
    forged_full = scram.with_ciphertext(am, scram.b64e(bytes(wrapped)))
    assert scram.unwrap_verifier(shared, am, forged_full, bytes(wrapped), scram.b64d(answer["wrap_tag"])) is None
    other_private, _ = scram.dh_keypair()   # someone without the client's DH secret
    wrong = scram.dh_shared_secret(other_private, scram.dh_decode(answer["dh_public"]))
    assert scram.unwrap_verifier(wrong, am, full, scram.b64d(answer["wrapped_verifier"]),
                                 scram.b64d(answer["wrap_tag"])) is None


def test_password_change_proof_covers_dh_and_ciphertext():
    old, _ = scram.make_verifier("old-pw", iterations=4096)
    uid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    flow = scram.ChangePasswordFlow(uid, "old-pw", "new-pw", min_iterations=4096)
    ch = dict(_server_challenge(old.salt, dh=True), new_salt=scram.b64e(scram.new_salt()), new_iterations=4096)
    answer = flow.respond(ch)

    def full_for(client_dh: str, wrapped: str) -> bytes:
        am = scram.password_auth_message(uid, flow.client_nonce, ch["server_nonce"], ch["salt"], 4096,
                                         ch["challenge_id"], ch["new_salt"], 4096, ch["dh_public"], client_dh)
        return scram.with_ciphertext(am, wrapped)

    proof = scram.b64d(answer["client_proof"])
    assert scram.verify_client_proof(old.stored_key, full_for(answer["dh_public"], answer["wrapped_verifier"]), proof)
    # A man in the middle swapping the DH value or the ciphertext breaks the old-password proof.
    _, mitm_public = scram.dh_keypair()
    assert not scram.verify_client_proof(old.stored_key, full_for(scram.dh_encode(mitm_public),
                                                                  answer["wrapped_verifier"]), proof)
    assert not scram.verify_client_proof(old.stored_key, full_for(answer["dh_public"], scram.b64e(bytes(64))), proof)
    # And the server recovers exactly the new verifier.
    am = scram.password_auth_message(uid, flow.client_nonce, ch["server_nonce"], ch["salt"], 4096,
                                     ch["challenge_id"], ch["new_salt"], 4096, ch["dh_public"], answer["dh_public"])
    shared = scram.dh_shared_secret(ch["_private"], scram.dh_decode(answer["dh_public"]))
    keys = scram.unwrap_verifier(shared, am, full_for(answer["dh_public"], answer["wrapped_verifier"]),
                                 scram.b64d(answer["wrapped_verifier"]), scram.b64d(answer["wrap_tag"]))
    new, _ = scram.make_verifier("new-pw", scram.b64d(ch["new_salt"]), 4096)
    assert keys == (new.stored_key, new.server_key)


def test_client_refuses_absurd_iterations_and_unknown_groups():
    with pytest.raises(ValueError):
        scram.LoginFlow("a", "b", min_iterations=4096).respond(_server_challenge(scram.new_salt(), iterations=10 ** 9))
    ch = dict(_server_challenge(scram.new_salt(), dh=True), dh_group="modp1024")
    with pytest.raises(ValueError):
        scram.RegisterFlow("abc", "pw", device_id=None, min_iterations=4096).respond(ch)
