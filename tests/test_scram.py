"""Password hashing and the stored verifier format (no network)."""
import hashlib
import hmac
import unicodedata

import pytest

from socketcord import scram


def test_rfc7677_key_derivation():
    """The key math is still RFC 7677's, so the stored hash stays the one
    PostgreSQL writes for a SCRAM password. Vector: user "user", password
    "pencil", 4096 rounds (RFC 7677 section 3)."""
    salt = scram.b64d("W22ZaJ0SNY7soEsUEjb6gQ==")
    client_key, stored_key, server_key = scram.derive_keys("pencil", salt, 4096)
    auth_message = (b"n=user,r=rOprNGfwEbeRWgbNEkqO,"
                    b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
                    b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0")
    signature = hmac.new(stored_key, auth_message, hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, signature))
    assert scram.b64e(proof) == "dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
    assert scram.b64e(hmac.new(server_key, auth_message, hashlib.sha256).digest()) == \
        "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    assert stored_key == hashlib.sha256(client_key).digest()


def test_verify_password():
    v, _ = scram.make_verifier("hunter2", iterations=4096)
    assert scram.verify_password("hunter2", v.salt, v.iterations, v.stored_key)
    for wrong in ("hunter3", "HUNTER2", "hunter2 ", "", "hunter"):
        assert not scram.verify_password(wrong, v.salt, v.iterations, v.stored_key)
    # Another salt, or another iteration count, is another hash.
    assert not scram.verify_password("hunter2", scram.new_salt(), v.iterations, v.stored_key)
    assert not scram.verify_password("hunter2", v.salt, v.iterations + 1, v.stored_key)
    assert not scram.verify_password("hunter2", v.salt, v.iterations, b"short")


def test_verify_password_normalizes_unicode():
    """NFKC: the same password typed with a composed or a decomposed accent matches."""
    composed, decomposed = "passé", "passé"
    assert composed != decomposed and unicodedata.normalize("NFKC", decomposed) == composed
    v, _ = scram.make_verifier(decomposed, iterations=4096)
    assert scram.verify_password(composed, v.salt, v.iterations, v.stored_key)


def test_the_stored_hash_is_not_the_password():
    v, _ = scram.make_verifier("TopSecret-รหัสผ่าน", iterations=4096)
    stored = scram.encode_verifier(v.salt, v.iterations, v.stored_key, v.server_key)
    assert stored.startswith("SCRAM-SHA-256$4096:")
    assert "TopSecret" not in stored and "รหัส" not in stored
    assert scram.decode_verifier(stored) == v         # PostgreSQL's own format, round-trips


def test_decode_verifier_refuses_rubbish():
    v, _ = scram.make_verifier("pw", iterations=4096)
    good = scram.encode_verifier(v.salt, v.iterations, v.stored_key, v.server_key)
    for bad in ("", "plaintext", "SCRAM-SHA-256$abc:x$y:z", "MD5$4096:AA==$AA==:AA==",
                good.replace("$", ":", 1), good[:-4], "SCRAM-SHA-256$4096:" + scram.b64e(b"s") + "$AA==:AA=="):
        with pytest.raises(ValueError):
            scram.decode_verifier(bad)


def test_canonical_username_and_iterations():
    assert scram.canonical_username("  Alice  ") == "alice"
    assert scram.check_iterations(600_000) == 600_000
    for bad in (True, 4095, 10_000_001, "4096", 4096.0, None):
        with pytest.raises(ValueError):
            scram.check_iterations(bad)


def test_device_fingerprint_is_stable_and_hides_the_mac():
    import uuid as uuid_mod
    fp = scram.device_fingerprint()
    assert fp == scram.device_fingerprint() and len(fp) == 32
    assert f"{uuid_mod.getnode():012x}" not in fp
