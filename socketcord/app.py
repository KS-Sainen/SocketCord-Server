"""The application: turns one complete request into one ReplyCord.

The networking thread calls dispatch() through the worker pool. dispatch()
never raises: every failure becomes a reply with an error status, so the
server keeps running whatever a client sends.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from . import presence
from .ai import SummaryService
from .config import Config
from .db.base import DatabaseUnavailable, NotFoundError, Repository, Session, User, UUID
from .presence import PresenceHub
from .protocol import MAX_MESSAGE_BYTES, METHOD_NAMES, Frame, MessageError, ReplyCord, Status, peek_rid, valid_rid
from .router import (ApiError, RequestContext, Router, bad_request, forbidden, not_found, normalize_url,
                     unauthorized)
from .security import LoginGuard, RateLimiter, token_hash
from .views import online_user

log = logging.getLogger("socketcord.app")

PATH_ECHO_LIMIT = 256          # replies echo at most this much of the request URL
CHAT_LOCK_STRIPES = 64


def loggable(text: str, limit: int = 200) -> str:
    """Client-supplied text for the log: control characters escaped (so nobody can
    forge extra log lines with a newline), and cut to `limit` characters."""
    cleaned = "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text[:limit])
    return cleaned + ("..." if len(text) > limit else "")


class Application:
    def __init__(self, config: Config, repo: Repository, router: Optional[Router] = None) -> None:
        if router is None:
            from .handlers import router as default_router
            router = default_router
        self.config = config
        self.repo = repo
        self.router = router
        self.hub = PresenceHub()
        self.login_guard = LoginGuard(config.login_max_failures, config.login_failure_window,
                                      config.login_max_failures_per_client)
        # Per client address: registering costs a PBKDF2 run before anything is stored.
        self.register_limiter = RateLimiter(config.register_rate_per_minute, config.register_burst)
        # Per account: actions that push packets to other people, or fill the database.
        self.group_limiter = RateLimiter(config.group_create_per_hour / 60.0, burst=5)
        self.request_limiter = RateLimiter(config.request_rate_per_hour / 60.0, burst=20)
        self.membership_limiter = RateLimiter(config.membership_rate_per_minute, burst=10)
        self.message_limiter = RateLimiter(config.message_rate_per_minute, burst=30)
        self.profile_limiter = RateLimiter(config.profile_rate_per_minute, burst=5)
        # AI summaries cost money and hold a worker thread: limited per account,
        # and SummaryService caps them per server.
        self.ai_limiter = RateLimiter(config.ai_rate_per_minute, burst=3)
        self.ai = SummaryService(config)
        # Per chat: "write, then push" runs one at a time, so every member receives a
        # chat's events in the order they were committed.
        self._chat_locks = [threading.RLock() for _ in range(CHAT_LOCK_STRIPES)]
        # Per pair of users: opening their DM runs one at a time. Always taken BEFORE
        # any chat lock (a separate family, so the two can never wait on each other).
        self._pair_locks = [threading.RLock() for _ in range(CHAT_LOCK_STRIPES)]

    def chat_lock(self, chat_id: UUID) -> threading.RLock:
        return self._chat_locks[hash(chat_id) % CHAT_LOCK_STRIPES]

    def pair_lock(self, user_a: UUID, user_b: UUID) -> threading.RLock:
        return self._pair_locks[hash(frozenset((user_a, user_b))) % CHAT_LOCK_STRIPES]

    # These run on the networking thread, so they must never touch the database.
    def attach(self, server) -> None:
        self.hub.attach(server.send, server.close_connection)

    def detach(self) -> None:
        self.hub.detach()

    def on_connect(self, peer) -> None:
        self.hub.connection_opened(peer.conn_id)

    def on_disconnect(self, peer) -> None:
        self.hub.connection_closed(peer.conn_id)

    def tick(self) -> None:
        self.hub.tick(time.monotonic())

    def housekeeping(self) -> None:
        self.login_guard.purge()
        self.hub.expire_sessions(datetime.now(timezone.utc))

    def is_authenticated(self, conn_id: int) -> bool:
        return self.hub.user_of(conn_id) is not None

    def bind_presence(self, conn_id: int, user: User, session: Session, login: bool = False,
                      replacing: Optional[bytes] = None) -> str:
        return self.hub.bind(conn_id, online_user(user), session.token_hash, session.expires_at,
                             allow_switch=login, max_per_user=self.config.max_connections_per_user,
                             evict_oldest=login, replacing=replacing)

    def dispatch(self, peer, frame: Frame) -> ReplyCord:
        started = time.perf_counter()
        path = normalize_url(frame.url_hint)
        method = frame.request.method_name if frame.request else METHOD_NAMES.get(frame.method, str(frame.method))
        rid = None
        reply_path = None
        try:
            if frame.error:
                raise bad_request(frame.error, frame.detail)
            req = frame.request
            if frame.msg_size > MAX_MESSAGE_BYTES:
                rid = peek_rid(req.msg)
                raise bad_request("MESSAGE_TOO_LARGE", f"the message may be at most {MAX_MESSAGE_BYTES} bytes")
            try:
                body = req.as_dict()
            except MessageError as exc:
                rid = exc.rid
                raise bad_request("INVALID_JSON", f"the message must be a JSON object ({exc})") from None
            rid = body.pop("rid", None)
            if rid is not None and not valid_rid(rid):
                rid = None
            matched = self.router.match(req.method, req.url)
            if matched is None:
                raise not_found("NOT_FOUND", f"no such call ({method})")
            route, params = matched
            reply_path = route.path
            ctx = RequestContext(app=self, peer=peer, request=req, body=body)
            if route.auth:
                self._authenticate(ctx)
            reply = route.handler(ctx, **params)
        except ApiError as exc:
            reply = exc.reply()
        except NotFoundError:
            reply = ApiError(Status.NOT_FOUND, "NOT_FOUND", "the chat or user no longer exists").reply()
        except DatabaseUnavailable as exc:
            log.error("database unavailable: %s", exc)
            reply = ApiError(Status.INTERNAL_ERROR, "DATABASE_UNAVAILABLE",
                             "the database is not reachable right now; try again").reply()
        except Exception:
            log.exception("handler error for %s %s from %s", loggable(method, 20), loggable(path), peer)
            reply = ApiError(Status.INTERNAL_ERROR, "INTERNAL_ERROR", "unexpected server error").reply()

        # Context for the client: which call this reply answers. `path` is the
        # route's semantic name (contract v2 section 2) and falls back to the
        # request URL for a route the contract does not name; GET chats and POST
        # chats share a path, so the method is echoed too. An error is never
        # given the semantic name, which is the name of the call SUCCEEDING: it
        # echoes the request URL, so a wrong password is not "login/success".
        if not reply.ok:
            reply_path = None
        head = {"path": reply_path or path[:PATH_ECHO_LIMIT], "method": method[:16]}
        if rid is not None:
            head["rid"] = rid
        head.update({k: v for k, v in reply.data.items() if k not in ("path", "method", "rid")})
        reply.data = head
        error = reply.data.get("error")
        log.info("%s %s %s -> %d%s (%.1f ms)", peer, loggable(method, 20), loggable(path), reply.status,
                 f" {error}" if error else "", (time.perf_counter() - started) * 1000)
        return reply

    def _authenticate(self, ctx: RequestContext) -> None:
        req = ctx.request
        if not req.has_auth:
            # README: calls other than register/login without credentials are "Not Found".
            raise not_found("NOT_FOUND", "this call needs a User ID and Session Token")
        hashed = token_hash(req.ses_token)
        found = self.repo.get_session(hashed)
        if found is None:
            # A password change on this very connection replaced the token a moment
            # ago: requests the client sent before reading that reply still count.
            moved = self.hub.rotated_session(ctx.peer.conn_id, hashed)
            if moved is not None and moved[0] == req.user_id:
                found = self.repo.get_session(moved[1])
        if found is None or found[0].user_id != req.user_id:
            # Logged out, replaced by a newer login, or never valid. If this
            # connection was still bound to it, it is not any more.
            self.hub.drop_if_bound(ctx.peer.conn_id, hashed)
            raise unauthorized("INVALID_SESSION", "the session is invalid or expired; log in again")
        session, user = found
        if session.expires_at <= datetime.now(timezone.utc):
            # A dead session: make sure no connection keeps getting pushes on it.
            self.hub.revoke_sessions([session.token_hash],
                                     notify=ReplyCord.push("login/revoked", {"reason": "expired"}))
            raise unauthorized("INVALID_SESSION", "the session is invalid or expired; log in again")
        ctx.user, ctx.session = user, session
        # Any authenticated request marks this connection as online for the user,
        # so a client that reconnects with a saved session shows up again.
        status = self.bind_presence(ctx.peer.conn_id, user, session)
        if status == presence.REFUSED_SWITCH:
            raise forbidden("CONNECTION_IN_USE", "this connection is logged in as another account; "
                            "log in again on it, or use another connection")
        if status == presence.REFUSED_LIMIT:
            raise forbidden("TOO_MANY_CONNECTIONS",
                            f"at most {self.config.max_connections_per_user} connections per account")
        if status in (presence.REFUSED_REVOKED, presence.REFUSED_EXPIRED):
            raise unauthorized("INVALID_SESSION", "the session is invalid or expired; log in again")
