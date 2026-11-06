"""Register, log in, change password (challenge-response), log out.

Each of the three password flows takes two requests to the same URL on the
same TCP connection:

  step 1  client -> {username/client_nonce ...}          server -> {"step": "challenge", ...}
  step 2  client -> {"challenge_id", proof/wrapped keys}  server -> {"step": "done", ...}

The presence of "challenge_id" in the message tells the steps apart. Register
and password change also run an ephemeral Diffie-Hellman exchange, so the new
verifier travels encrypted. See docs/PROTOCOL.md and socketcord/scram.py.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from .. import scram
from ..db.base import RateLimited, RegistrationLimit, RegistrationRule, UsernameTaken
from ..protocol import PATCH, POST, ReplyCord, Status
from ..router import ApiError, RequestContext, bad_request, forbidden, ok, unauthorized
from ..security import Challenge, client_key, fake_salt, token_hash
from ..views import user_view
from . import router

log = logging.getLogger("socketcord.auth")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
PLAINTEXT_FIELDS = ("password", "old_password", "new_password")


# ---------------------------------------------------------------------- field helpers

def _reject_plaintext(ctx: RequestContext) -> None:
    """README rule: never send the password itself. Say so clearly instead of a vague 'missing field'."""
    for key in PLAINTEXT_FIELDS:
        if key in ctx.body:
            raise bad_request("PLAINTEXT_PASSWORD",
                              f"never send '{key}' over the socket; use the challenge-response flow "
                              "(socketcord/scram.py, docs/PROTOCOL.md)", field=key)


def _username(ctx: RequestContext) -> str:
    name = ctx.str_field("username", max_len=64)
    if not USERNAME_RE.match(name):
        raise bad_request("INVALID_USERNAME",
                          "username must be 3-32 characters of letters, digits, '_', '.' or '-'",
                          field="username")
    return name


def _client_nonce(ctx: RequestContext) -> str:
    nonce = ctx.str_field("client_nonce", min_len=16, max_len=128)
    if not B64_RE.match(nonce):
        raise bad_request("INVALID_FIELD", "'client_nonce' must be base64", field="client_nonce")
    return nonce


def _b64_text(ctx: RequestContext, key: str, length: int) -> str:
    """Validates a base64 field and returns the text exactly as the client sent it.

    AuthMessages are built from the client's own strings, so both sides hash
    identical bytes even if a client's encoder differs from ours.
    """
    ctx.b64_field(key, length)
    return str(ctx.body[key])


def _client_dh(ctx: RequestContext) -> Tuple[str, int]:
    text = _b64_text(ctx, "dh_public", scram.DH_LEN)
    try:
        return text, scram.dh_decode(text)
    except ValueError:
        raise bad_request("INVALID_FIELD", "'dh_public' is not a valid group element", field="dh_public") from None


def _challenge_fields(ctx: RequestContext, ch: Optional[Challenge]) -> dict:
    if ch is None:
        # The connection closed while this step was being handled; nobody will read this.
        raise bad_request("CONNECTION_CLOSED", "the connection closed during the request")
    fields = {"challenge_id": ch.challenge_id, "salt": scram.b64e(ch.salt), "iterations": ch.iterations,
              "server_nonce": ch.server_nonce, "expires_in": ctx.config.challenge_ttl}
    if ch.dh_public:
        fields.update({"dh_group": scram.DH_GROUP, "dh_public": ch.dh_public})
    return fields


def _new_dh(ctx: RequestContext) -> dict:
    private, public_b64 = ctx.app.dh_key.get()   # reused briefly; see security.ServerDHKey
    return {"dh_private": private, "dh_public": public_b64}


def _limit_dh_challenges(ctx: RequestContext) -> None:
    """Register / password-change challenges make the server do a DH step, so they are
    rate-limited per client address (this also slows down probing for taken names)."""
    wait = ctx.app.challenge_limiter.take(client_key(ctx.peer.ip))
    if wait > 0:
        raise forbidden("TOO_MANY_ATTEMPTS", "too many sign-up or password-change attempts from this "
                        "address; wait and try again", retry_after=int(wait) + 1)


def _take_challenge(ctx: RequestContext, kind: str, status: int = Status.UNAUTHORIZED) -> Challenge:
    challenge_id = ctx.str_field("challenge_id", max_len=64)
    ch = ctx.app.challenges.take(challenge_id, kind, ctx.peer.conn_id)
    if ch is None:
        raise ApiError(status, "CHALLENGE_INVALID",
                       "unknown or expired challenge (it must be answered on the same connection "
                       f"within {ctx.config.challenge_ttl}s); start again")
    return ch


def _throttle(ctx: RequestContext, canonical: str, what: str, real: bool) -> None:
    """Check only: refuse to hand out a challenge while the limit is reached.
    `real`: the username exists (counted per name) or not (counted per address)."""
    retry = ctx.app.login_guard.retry_after(canonical, client_key(ctx.peer.ip), real)
    if retry > 0:
        raise forbidden("TOO_MANY_ATTEMPTS", f"too many failed {what}; wait and try again",
                        retry_after=int(retry) + 1)


def _reserve_attempt(ctx: RequestContext, canonical: str, what: str, real: bool) -> None:
    """Check AND count this attempt in one step, so parallel answers cannot all get
    past the limit. A correct answer is taken back with login_guard.succeeded()."""
    retry = ctx.app.login_guard.reserve(canonical, client_key(ctx.peer.ip), real)
    if retry > 0:
        raise forbidden("TOO_MANY_ATTEMPTS", f"too many failed {what}; wait and try again",
                        retry_after=int(retry) + 1)


# ---------------------------------------------------------------------- one account per day

def _registration_limit(ctx: RequestContext, device_id: Optional[str]) -> Optional[RegistrationLimit]:
    cfg = ctx.config
    if not cfg.reg_limit_enabled:
        return None
    rules = []
    if cfg.reg_limit_device_max > 0 and device_id:
        rules.append(RegistrationRule("device", device_id, cfg.reg_limit_device_max))
    # The server always sees the source IP, whatever the client claims about its device.
    # IPv6 clients are counted per /64, since one home or site gets a whole /64.
    if cfg.reg_limit_ip_max > 0 and not cfg.ip_exempt(ctx.peer.ip):
        rules.append(RegistrationRule("ip", client_key(ctx.peer.ip), cfg.reg_limit_ip_max))
    return RegistrationLimit(cfg.reg_limit_window_hours * 3600, rules) if rules else None


def _rate_limited(ctx: RequestContext, retry_after: float) -> ApiError:
    cfg = ctx.config
    return forbidden("RATE_LIMITED",
                     f"too many new accounts from this device or address in the last "
                     f"{cfg.reg_limit_window_hours} hours; try again later",
                     retry_after=int(retry_after) + 1)


# ---------------------------------------------------------------------- POST users

@router.route(POST, "users", auth=False)
def register(ctx: RequestContext) -> ReplyCord:
    _reject_plaintext(ctx)
    if ctx.has("challenge_id"):
        return _register_finish(ctx)
    return _register_start(ctx)


def _register_start(ctx: RequestContext) -> ReplyCord:
    _limit_dh_challenges(ctx)
    username = _username(ctx)
    client_nonce = _client_nonce(ctx)
    device_id = ctx.str_field("device_id", required=False, min_len=8, max_len=128)
    if device_id is not None and not DEVICE_RE.match(device_id):
        raise bad_request("INVALID_FIELD", "'device_id' has invalid characters", field="device_id")
    if ctx.repo.get_user_by_username(username) is not None:
        raise forbidden("USERNAME_TAKEN", f"the name '{username}' is already taken", field="username")
    limit = _registration_limit(ctx, device_id)
    if limit is not None:
        retry = ctx.repo.registration_retry_after(limit)
        if retry > 0:
            raise _rate_limited(ctx, retry)
    ch = ctx.app.challenges.issue("register", ctx.peer.conn_id, username=username, client_nonce=client_nonce,
                                  salt=scram.new_salt(), iterations=ctx.config.scram_iterations,
                                  device_id=device_id, **_new_dh(ctx))
    return ok({"step": "challenge", **_challenge_fields(ctx, ch)})


def _register_finish(ctx: RequestContext) -> ReplyCord:
    ch = _take_challenge(ctx, "register")
    client_dh_text, client_dh = _client_dh(ctx)
    wrapped_text = _b64_text(ctx, "wrapped_verifier", scram.WRAPPED_LEN)
    tag = ctx.b64_field("wrap_tag", scram.KEY_LEN)
    am = scram.register_auth_message(ch.username, ch.client_nonce, ch.server_nonce, scram.b64e(ch.salt),
                                     ch.iterations, ch.challenge_id, ch.dh_public, client_dh_text)
    full = scram.with_ciphertext(am, wrapped_text)
    keys = scram.unwrap_verifier(scram.dh_shared_secret(ch.dh_private, client_dh), am, full,
                                 scram.b64d(wrapped_text), tag)
    if keys is None:
        raise unauthorized("INVALID_PROOF", "the encrypted verifier failed its integrity check")
    stored_key, server_key = keys
    try:
        user = ctx.repo.create_user(ch.username, ch.salt, ch.iterations, stored_key, server_key,
                                    ctx.peer.ip, ch.device_id, _registration_limit(ctx, ch.device_id))
    except UsernameTaken:
        raise forbidden("USERNAME_TAKEN", f"the name '{ch.username}' is already taken", field="username") from None
    except RateLimited as exc:
        raise _rate_limited(ctx, exc.retry_after) from None
    log.info("registered '%s' from %s", user.username, ctx.peer)
    return ok({"step": "done", "user_id": user.user_id, "user": user_view(user, ctx.hub, full=True),
               "server_signature": scram.b64e(scram.server_signature(server_key, full))})


# ---------------------------------------------------------------------- POST users/login

@router.route(POST, "users/login", auth=False)
def login(ctx: RequestContext) -> ReplyCord:
    _reject_plaintext(ctx)
    if ctx.has("challenge_id"):
        return _login_finish(ctx)
    return _login_start(ctx)


def _login_start(ctx: RequestContext) -> ReplyCord:
    username = _username(ctx)
    client_nonce = _client_nonce(ctx)
    canonical = scram.canonical_username(username)
    user = ctx.repo.get_user_by_username(username)
    _throttle(ctx, canonical, "logins", real=user is not None)
    if user is not None:
        salt, iterations, user_id = user.salt, user.iterations, user.user_id
    else:
        # Unknown name: hand out a believable challenge anyway, so login does
        # not tell an attacker which usernames exist. Step 2 will fail.
        salt, iterations, user_id = fake_salt(ctx.config.secret, username), ctx.config.scram_iterations, None
    ch = ctx.app.challenges.issue("login", ctx.peer.conn_id, username=canonical, user_id=user_id,
                                  client_nonce=client_nonce, salt=salt, iterations=iterations)
    return ok({"step": "challenge", **_challenge_fields(ctx, ch)})


def _login_finish(ctx: RequestContext) -> ReplyCord:
    ch = _take_challenge(ctx, "login")
    proof = ctx.b64_field("client_proof", scram.KEY_LEN)
    # Counted before checking, so challenges fetched earlier cannot buy extra guesses.
    _reserve_attempt(ctx, ch.username, "logins", real=ch.user_id is not None)
    user = ctx.repo.get_user(ch.user_id) if ch.user_id else None
    am = scram.login_auth_message(ch.username, ch.client_nonce, ch.server_nonce, scram.b64e(ch.salt),
                                  ch.iterations, ch.challenge_id)
    if user is None or user.salt != ch.salt or not scram.verify_client_proof(user.stored_key, am, proof):
        raise unauthorized("INVALID_CREDENTIALS", "wrong username or password")
    token = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ctx.config.session_ttl_seconds)
    # One session per account: this login replaces the previous one, atomically,
    # and only if the verifier the proof was checked against is still current.
    made = ctx.repo.create_session(token_hash(token), user.user_id, expires_at,
                                   expected_verifier=user.password_hash)
    if made is None:
        raise unauthorized("CHALLENGE_INVALID", "the password was changed meanwhile; log in again")
    session, replaced = made
    ctx.app.login_guard.succeeded(ch.username, client_key(ctx.peer.ip))
    # login=True: may switch this connection's account, and logs out the user's
    # oldest connection if they are at MAX_CONNECTIONS_PER_USER (not counting the
    # connections of the session this login replaces).
    ctx.app.bind_presence(ctx.peer.conn_id, user, session, login=True, replacing=replaced)
    if replaced is not None:
        # Every connection still on the previous session is logged out (another
        # device), whatever happens to this login next.
        ctx.hub.revoke_sessions([replaced], notify=ReplyCord.event("session_revoked",
                                                                   {"reason": "logged_in_elsewhere"}))
    if ctx.hub.session_of(ctx.peer.conn_id) != session.token_hash:
        # Another login of the same account replaced this session a moment later.
        raise unauthorized("INVALID_SESSION", "this account logged in somewhere else at the same moment; "
                           "log in again")
    log.info("'%s' logged in on %s", user.username, ctx.peer)
    return ok({"step": "done", "user_id": user.user_id, "session_token": token,
               "expires_at": session.expires_at, "user": user_view(user, ctx.hub, full=True),
               "server_signature": scram.b64e(scram.server_signature(user.server_key, am))})


# ---------------------------------------------------------------------- PATCH users/profile/password

@router.route(PATCH, "users/profile/password")
def change_password(ctx: RequestContext) -> ReplyCord:
    _reject_plaintext(ctx)
    if ctx.has("challenge_id"):
        return _password_finish(ctx)
    return _password_start(ctx)


def _password_start(ctx: RequestContext) -> ReplyCord:
    _limit_dh_challenges(ctx)
    client_nonce = _client_nonce(ctx)
    user = ctx.user
    canonical = scram.canonical_username(user.username)
    _throttle(ctx, canonical, "password checks", real=True)
    ch = ctx.app.challenges.issue("password", ctx.peer.conn_id, user_id=user.user_id, username=canonical,
                                  client_nonce=client_nonce, salt=user.salt, iterations=user.iterations,
                                  new_salt=scram.new_salt(), new_iterations=ctx.config.scram_iterations,
                                  session_hash=ctx.session.token_hash, **_new_dh(ctx))
    return ok({"step": "challenge", **_challenge_fields(ctx, ch),
               "new_salt": scram.b64e(ch.new_salt), "new_iterations": ch.new_iterations})


def _password_finish(ctx: RequestContext) -> ReplyCord:
    # Failures here answer 252, not 251: the session is still fine, and 251 tells
    # clients to show the login page.
    ch = _take_challenge(ctx, "password", status=Status.FORBIDDEN)
    if ch.user_id != ctx.user.user_id or ch.session_hash != ctx.session.token_hash:
        raise forbidden("CHALLENGE_INVALID", "this challenge belongs to another session")
    proof = ctx.b64_field("client_proof", scram.KEY_LEN)
    client_dh_text, client_dh = _client_dh(ctx)
    wrapped_text = _b64_text(ctx, "wrapped_verifier", scram.WRAPPED_LEN)
    tag = ctx.b64_field("wrap_tag", scram.KEY_LEN)
    am = scram.password_auth_message(str(ch.user_id), ch.client_nonce, ch.server_nonce, scram.b64e(ch.salt),
                                     ch.iterations, ch.challenge_id, scram.b64e(ch.new_salt),
                                     ch.new_iterations, ch.dh_public, client_dh_text)
    full = scram.with_ciphertext(am, wrapped_text)
    _reserve_attempt(ctx, ch.username, "password checks", real=True)
    user = ctx.repo.get_user(ch.user_id)
    # The old-password proof covers both DH values and the ciphertext.
    if user is None or user.salt != ch.salt or not scram.verify_client_proof(user.stored_key, full, proof):
        raise forbidden("INVALID_CREDENTIALS", "the current password is wrong")
    keys = scram.unwrap_verifier(scram.dh_shared_secret(ch.dh_private, client_dh), am, full,
                                 scram.b64d(wrapped_text), tag)
    if keys is None:
        raise bad_request("INVALID_PROOF", "the encrypted verifier failed its integrity check")
    new_stored, new_server = keys
    # New verifier AND a new session token, in one transaction: the old token
    # stops working everywhere (other devices are logged out).
    token = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ctx.config.session_ttl_seconds)
    old_hash = ctx.session.token_hash
    session = ctx.repo.change_credentials(user.user_id, ch.new_salt, ch.new_iterations, new_stored, new_server,
                                          expected_verifier=user.password_hash, current_token_hash=old_hash,
                                          new_token_hash=token_hash(token), expires_at=expires_at)
    if session is None:
        if ctx.repo.get_session(old_hash) is None:
            raise unauthorized("INVALID_SESSION", "this session ended meanwhile; log in again")
        raise forbidden("CHALLENGE_INVALID", "the password was changed meanwhile; start again")
    ctx.app.login_guard.succeeded(ch.username, client_key(ctx.peer.ip))
    # This connection moves to the new token (the old one keeps working here for a
    # few seconds, for requests already on their way); every other is logged out.
    others = ctx.hub.rotate(ctx.peer.conn_id, user.user_id, old_hash, session.token_hash, session.expires_at,
                            notify=ReplyCord.event("session_revoked", {"reason": "password_changed"}))
    if ctx.peer.conn_id in others:
        # A login with the new password replaced the new session at the same moment.
        raise unauthorized("INVALID_SESSION", "the password was changed, but the account logged in somewhere "
                           "else at the same moment; log in again")
    log.info("'%s' changed password; %d other connection(s) logged out", user.username, len(others))
    return ok({"step": "done", "user_id": user.user_id, "session_token": token, "expires_at": session.expires_at,
               "logged_out_connections": len(others),
               "server_signature": scram.b64e(scram.server_signature(user.server_key, full))})


# ---------------------------------------------------------------------- POST users/logout

@router.route(POST, "users/logout")
def logout(ctx: RequestContext) -> ReplyCord:
    ended = ctx.repo.revoke_session(ctx.session.token_hash)
    ctx.hub.unbind(ctx.peer.conn_id)
    if ended:
        # Every connection using this session goes offline, not only this one; the
        # others are told why. (If the session had already been replaced, whoever
        # replaced it logged those connections out.)
        ctx.hub.revoke_sessions([ctx.session.token_hash],
                                notify=ReplyCord.event("session_revoked", {"reason": "logged_out"}))
    return ok({"logged_out": True})
