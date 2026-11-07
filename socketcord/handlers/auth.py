"""Register, log in, change password, log out.

One request each, with the password in the message body as clear text (wire
contract v2). The socket is not encrypted, so the password is readable by
anyone who can watch the traffic; the server hashes it with PBKDF2-HMAC-SHA256
and stores only the hash (socketcord/scram.py, docs/PROTOCOL.md section 4).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import scram
from ..db.base import RateLimited, RegistrationLimit, RegistrationRule, UsernameTaken
from ..protocol import PATCH, POST, ReplyCord
from ..router import ApiError, RequestContext, bad_request, forbidden, ok, unauthorized
from ..security import client_key, token_hash
from ..views import user_view
from . import router
from .common import username_field

log = logging.getLogger("socketcord.auth")

DEVICE_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
MAX_PASSWORD_LEN = 256     # PBKDF2 hashes any length; this only keeps the message small

_DUMMY_SALT = scram.new_salt()


def _password(ctx: RequestContext, key: str = "password") -> str:
    """The password as sent. Not trimmed: spaces inside a password count."""
    return ctx.str_field(key, min_len=1, max_len=MAX_PASSWORD_LEN, strip=False)


def _verify(ctx: RequestContext, password: str, user) -> bool:
    if user is None:
        # An unknown name costs the same PBKDF2 run as a wrong password, so the
        # two cannot be told apart by how long the reply takes.
        scram.salted_password(password, _DUMMY_SALT, ctx.config.scram_iterations)
        return False
    return scram.verify_password(password, user.salt, user.iterations, user.stored_key)


def _throttle(ctx: RequestContext, canonical: str, what: str, real: bool) -> None:
    """Checks AND counts this attempt in one step, so attempts sent in parallel
    cannot all slip under the limit. A correct password is taken back with
    login_guard.succeeded()."""
    retry = ctx.app.login_guard.reserve(canonical, client_key(ctx.peer.ip), real)
    if retry > 0:
        raise forbidden("RATE_LIMITED", f"too many failed {what}; wait and try again",
                        retry_after=int(retry) + 1)


def _registration_limit(ctx: RequestContext, device_id: Optional[str]) -> Optional[RegistrationLimit]:
    """The "one new account per device/address per day" rules that apply here."""
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


@router.route(POST, "users", auth=False, path="login/created")
def register(ctx: RequestContext) -> ReplyCord:
    username = username_field(ctx)
    password = _password(ctx)
    device_id = ctx.str_field("device_id", required=False, min_len=8, max_len=128)
    if device_id is not None and not DEVICE_RE.match(device_id):
        raise bad_request("INVALID_FIELD", "'device_id' has invalid characters", field="device_id")
    wait = ctx.app.register_limiter.take(client_key(ctx.peer.ip))
    if wait > 0:
        raise forbidden("RATE_LIMITED", "too many sign-up attempts from this address; wait and try again",
                        retry_after=int(wait) + 1)
    if ctx.repo.get_user_by_username(username) is not None:
        raise forbidden("USERNAME_TAKEN", f"the name '{username}' is already taken", field="username")
    limit = _registration_limit(ctx, device_id)
    if limit is not None:
        retry = ctx.repo.registration_retry_after(limit)
        if retry > 0:
            raise _rate_limited(ctx, retry)
    verifier, _ = scram.make_verifier(password, iterations=ctx.config.scram_iterations)
    try:
        user = ctx.repo.create_user(username, verifier.salt, verifier.iterations, verifier.stored_key,
                                    verifier.server_key, ctx.peer.ip, device_id, limit)
    except UsernameTaken:
        raise forbidden("USERNAME_TAKEN", f"the name '{username}' is already taken", field="username") from None
    except RateLimited as exc:
        raise _rate_limited(ctx, exc.retry_after) from None
    log.info("registered '%s' from %s", user.username, ctx.peer)
    # No session: the client logs in next (contract v2 section 1).
    return ok({"user_id": user.user_id, "username": user.username})


@router.route(POST, "users/login", auth=False, path="login/success")
def login(ctx: RequestContext) -> ReplyCord:
    username = username_field(ctx)
    password = _password(ctx)
    canonical = scram.canonical_username(username)
    user = ctx.repo.get_user_by_username(username)
    # Counted before the password is checked, so a flood of guesses cannot outrun the limit.
    _throttle(ctx, canonical, "logins", real=user is not None)
    if not _verify(ctx, password, user):
        # The same code for a wrong name and a wrong password: the reply does not
        # say which accounts exist.
        raise unauthorized("INVALID_CREDENTIALS", "wrong username or password")
    token = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ctx.config.session_ttl_seconds)
    # One session per account: this login replaces the previous one, atomically,
    # and only if the password it was just checked against is still current.
    made = ctx.repo.create_session(token_hash(token), user.user_id, expires_at,
                                   expected_verifier=user.password_hash)
    if made is None:
        raise unauthorized("INVALID_CREDENTIALS", "the password was changed meanwhile; log in again")
    session, replaced = made
    ctx.app.login_guard.succeeded(canonical, client_key(ctx.peer.ip))
    # login=True: may switch this connection's account, and logs out the user's
    # oldest connection if they are at MAX_CONNECTIONS_PER_USER (not counting the
    # connections of the session this login replaces).
    ctx.app.bind_presence(ctx.peer.conn_id, user, session, login=True, replacing=replaced)
    if replaced is not None:
        # Every connection still on the previous session is logged out (another
        # device), whatever happens to this login next.
        ctx.hub.revoke_sessions([replaced], notify=ReplyCord.push("login/revoked",
                                                                  {"reason": "logged_in_elsewhere"}))
    if ctx.hub.session_of(ctx.peer.conn_id) != session.token_hash:
        # Another login of the same account replaced this session a moment later.
        raise unauthorized("INVALID_SESSION", "this account logged in somewhere else at the same moment; "
                           "log in again")
    log.info("'%s' logged in on %s", user.username, ctx.peer)
    view = user_view(user, ctx.hub, full=True)
    # The colours are repeated at the top level because that is where the GUI
    # reads them (contract v2 section 1); `user` has the whole account.
    return ok({"user_id": user.user_id, "session_token": token, "expires_at": session.expires_at,
               "username": view["username"], "text_color": view["text_color"],
               "background": view["background"], "user": view})


@router.route(PATCH, "users/password", path="profile/password")
@router.route(PATCH, "users/profile/password", path="profile/password")   # the older URL, same call
def change_password(ctx: RequestContext) -> ReplyCord:
    """Sets a new password on a logged-in session. The pop-up has only New /
    Confirm, so no current password is asked for, and the session token is not
    rotated: the caller (and its other connections) keep working."""
    new_password = _password(ctx, "new_password")
    user = ctx.user
    verifier, _ = scram.make_verifier(new_password, iterations=ctx.config.scram_iterations)
    # change_credentials() writes the hash and the session in one transaction, only
    # if both are still the ones this request read. Handing it back the token it
    # already has is how the password changes without rotating the session.
    token_hash_now = ctx.session.token_hash
    session = ctx.repo.change_credentials(user.user_id, verifier.salt, verifier.iterations,
                                          verifier.stored_key, verifier.server_key,
                                          expected_verifier=user.password_hash,
                                          current_token_hash=token_hash_now,
                                          new_token_hash=token_hash_now,
                                          expires_at=ctx.session.expires_at)
    if session is None:
        # The session ended, or another connection changed the password first.
        if ctx.repo.get_session(token_hash_now) is None:
            raise unauthorized("INVALID_SESSION", "this session ended meanwhile; log in again")
        raise forbidden("TRY_AGAIN", "the password was changed a moment ago; try again")
    log.info("'%s' changed password", user.username)
    return ok({"user_id": user.user_id, "changed": True})


@router.route(POST, "users/logout", path="login/logout")
def logout(ctx: RequestContext) -> ReplyCord:
    ended = ctx.repo.revoke_session(ctx.session.token_hash)
    ctx.hub.unbind(ctx.peer.conn_id)
    if ended:
        # Every connection using this session goes offline, not only this one; the
        # others are told why. (If the session had already been replaced, whoever
        # replaced it logged those connections out.)
        ctx.hub.revoke_sessions([ctx.session.token_hash],
                                notify=ReplyCord.push("login/revoked", {"reason": "logged_out"}))
    return ok({"logged_out": True})
