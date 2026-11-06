"""Who is online, and how handlers push packets to them.

A connection becomes "online as user X" when it logs in, or when it sends any
request carrying a valid session (so a client that reconnects with a saved
session is online again). A user is online while at least one of their
connections is bound. A binding ends when the connection closes, the user
logs out, or the session is revoked or expires.

* Presence changes are pushed while holding the lock, so the networking
  thread (which sends queued packets in order) never delivers "online" after
  the matching "offline". Each event/presence carries an increasing `seq`.
* When the last connection of a user closes, "offline" waits a short grace
  period (LINGER_SECONDS) and is cancelled if the user reconnects, so a client
  that reconnects quickly, or someone flapping on purpose, does not make every
  online client redraw its list. Logout and revocation are immediate.

Worker threads call push(); the bytes are handed to the networking thread,
the only thread that writes to sockets.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .db.base import DEFAULT_TEXT_BG_COLOR, DEFAULT_TEXT_COLOR, UUID
from .protocol import ReplyCord

REVOKED_FENCE_SECONDS = 120.0   # how long a revoked session stays blocked here (requests in flight)
LINGER_SECONDS = 2.0            # grace period before announcing that someone went offline
ROTATION_GRACE_SECONDS = 30.0   # after a password change, the caller's old token still works on that connection

# bind() results
BOUND = "ok"
REFUSED_CLOSED = "closed"       # the connection already closed (a worker finishing late)
REFUSED_REVOKED = "revoked"     # the session was revoked a moment ago
REFUSED_EXPIRED = "expired"     # the session expired
REFUSED_SWITCH = "switch"       # the connection is logged in as someone else
REFUSED_LIMIT = "limit"         # the user already has the maximum number of connections


@dataclass
class OnlineUser:
    user_id: UUID
    username: str
    text_color: str = DEFAULT_TEXT_COLOR
    text_bg_color: str = DEFAULT_TEXT_BG_COLOR

    def as_dict(self, online: bool) -> dict:
        return {"user_id": self.user_id, "username": self.username, "text_color": self.text_color,
                "text_bg_color": self.text_bg_color, "online": online}


@dataclass
class _Binding:
    user_id: UUID
    session_hash: Optional[bytes]
    expires_at: Optional[datetime]
    since: float
    previous_hash: Optional[bytes] = None     # the token a password change just replaced ...
    previous_until: float = 0.0               # ... accepted on this connection until then


def _noop_send(conn_id: int, data: bytes) -> None:
    pass


def _noop_close(conn_id: int, reason: str) -> None:
    pass


class PresenceHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open: Set[int] = set()                          # connections still alive
        self._bindings: Dict[int, _Binding] = {}              # conn -> who it is logged in as
        self._user_conns: Dict[UUID, Set[int]] = {}
        self._profiles: Dict[UUID, OnlineUser] = {}
        self._lingering: "OrderedDict[UUID, float]" = OrderedDict()   # user -> when to announce offline
        self._revoked: "OrderedDict[bytes, float]" = OrderedDict()    # session hash -> when revoked
        self._seq = 0
        self._send: Callable[[int, bytes], None] = _noop_send
        self._close: Callable[[int, str], None] = _noop_close

    def attach(self, send: Callable[[int, bytes], None], close: Callable[[int, str], None]) -> None:
        self._send, self._close = send, close

    def detach(self) -> None:
        """Shutdown: stop pushing (closing N connections must not queue N*N offline events)."""
        with self._lock:
            self._send, self._close = _noop_send, _noop_close

    # ------------------------------------------------------------------ connection lifecycle
    def connection_opened(self, conn_id: int) -> None:
        with self._lock:
            self._open.add(conn_id)

    def connection_closed(self, conn_id: int) -> None:
        """Called once, by the networking thread, when a socket closes."""
        with self._lock:
            self._open.discard(conn_id)
            self._unbind_locked(conn_id, linger=True)

    def is_open(self, conn_id: int) -> bool:
        with self._lock:
            return conn_id in self._open

    # ------------------------------------------------------------------ binding
    def bind(self, conn_id: int, user: OnlineUser, session_hash: Optional[bytes],
             expires_at: Optional[datetime] = None, allow_switch: bool = False,
             max_per_user: int = 0, evict_oldest: bool = False, replacing: Optional[bytes] = None) -> str:
        """Marks a connection as logged in as `user`. Returns BOUND or why it refused.

        allow_switch: let a connection bound to another user switch (an explicit
        login); ordinary requests may not flip a connection between accounts.
        max_per_user: cap on a user's connections; with evict_oldest (login) the
        oldest one is logged out to make room, otherwise the bind is refused.
        replacing: the session a login replaces; its connections are about to be
        logged out (logged_in_elsewhere), so they do not count toward the cap.
        """
        with self._lock:
            if conn_id not in self._open:
                return REFUSED_CLOSED
            if session_hash is not None and session_hash in self._revoked:
                return REFUSED_REVOKED
            if expires_at is not None and expires_at <= datetime.now(expires_at.tzinfo):
                return REFUSED_EXPIRED
            current = self._bindings.get(conn_id)
            if current is not None and current.user_id != user.user_id:
                if not allow_switch:
                    return REFUSED_SWITCH
                self._unbind_locked(conn_id, linger=False)
                current = None
            if current is not None:
                current.session_hash, current.expires_at = session_hash, expires_at
                return BOUND
            conns = self._user_conns.get(user.user_id, set())
            counted = {cid for cid in conns if replacing is None or self._bindings[cid].session_hash != replacing}
            if max_per_user > 0 and len(counted) >= max_per_user:
                if not evict_oldest:
                    return REFUSED_LIMIT
                oldest = min(counted, key=lambda cid: self._bindings[cid].since)
                self._send(oldest, ReplyCord.event("session_revoked", {"reason": "too_many_connections"}).pack())
                self._unbind_locked(oldest, linger=False)
                conns = self._user_conns.get(user.user_id, set())
            self._bindings[conn_id] = _Binding(user.user_id, session_hash, expires_at, time.monotonic())
            # The profile is cached when the user comes online; later changes arrive
            # through update_profile(). A request that read the user row a moment
            # earlier must not put old colours back.
            self._profiles.setdefault(user.user_id, user)
            was_visible = bool(conns) or user.user_id in self._lingering
            self._lingering.pop(user.user_id, None)
            self._user_conns.setdefault(user.user_id, set()).add(conn_id)
            if not was_visible:
                self._push_presence_locked(self._profiles[user.user_id], True, exclude_conn=conn_id)
            return BOUND

    def unbind(self, conn_id: int) -> None:
        """Logs one connection out (the socket stays open)."""
        with self._lock:
            self._unbind_locked(conn_id, linger=False)

    def rotate(self, conn_id: int, user_id: UUID, old_hash: bytes, new_hash: bytes,
               expires_at: Optional[datetime], notify: Optional[ReplyCord] = None) -> List[int]:
        """A password change replaced the session token. The caller's connection moves
        to the new token (its old one keeps working there for ROTATION_GRACE_SECONDS,
        so requests the client sent before reading the reply still go through);
        every other connection on the old token is logged out, all under one lock."""
        data = notify.pack() if notify is not None else None
        with self._lock:
            binding = self._bindings.get(conn_id)
            # If a login with the new password already replaced the new token, the
            # caller is not moved onto that dead token: it is logged out with the rest.
            if binding is not None and binding.user_id == user_id and new_hash not in self._revoked:
                binding.session_hash, binding.expires_at = new_hash, expires_at
                binding.previous_hash = old_hash
                binding.previous_until = time.monotonic() + ROTATION_GRACE_SECONDS
            self._revoked.pop(old_hash, None)
            self._revoked[old_hash] = time.monotonic()
            affected = [cid for cid, b in self._bindings.items() if b.session_hash == old_hash]
            for cid in affected:
                if data is not None and cid != conn_id:
                    self._send(cid, data)
                self._unbind_locked(cid, linger=False)
            return affected

    def session_of(self, conn_id: int) -> Optional[bytes]:
        """The session hash this connection is bound to, if any."""
        with self._lock:
            b = self._bindings.get(conn_id)
            return b.session_hash if b else None

    def rotated_session(self, conn_id: int, token_hash: bytes) -> Optional[Tuple[UUID, bytes, Optional[datetime]]]:
        """If this connection's password change replaced `token_hash` a moment ago,
        the (user_id, new session hash, expiry) it moved to; otherwise None."""
        with self._lock:
            b = self._bindings.get(conn_id)
            if (b is None or b.previous_hash != token_hash or b.session_hash is None
                    or time.monotonic() > b.previous_until):
                return None
            return b.user_id, b.session_hash, b.expires_at

    def drop_if_bound(self, conn_id: int, token_hash: bytes, notify: Optional[ReplyCord] = None) -> bool:
        """Logs this connection out if it is bound to exactly this (dead) session.
        Unlike revoke_sessions(), it adds no fence entry, so a flood of bogus tokens
        cannot grow the fence."""
        with self._lock:
            b = self._bindings.get(conn_id)
            if b is None or b.session_hash != token_hash:
                return False
            if notify is not None:
                self._send(conn_id, notify.pack())
            self._unbind_locked(conn_id, linger=False)
            return True

    def revoke_sessions(self, session_hashes: Iterable[bytes], notify: Optional[ReplyCord] = None) -> List[int]:
        """Unbinds every connection using one of these sessions, and blocks them from
        binding again for a while. Pushes `notify` to each affected connection first."""
        hashes = set(session_hashes)
        if not hashes:
            return []
        data = notify.pack() if notify is not None else None
        with self._lock:
            now = time.monotonic()
            for h in hashes:
                self._revoked.pop(h, None)
                self._revoked[h] = now
            affected = [cid for cid, b in self._bindings.items() if b.session_hash in hashes]
            for cid in affected:
                if data is not None:
                    self._send(cid, data)
                self._unbind_locked(cid, linger=False)
            return affected

    def expire_sessions(self, now: datetime) -> None:
        """Housekeeping: log out bindings whose session ran out (telling the client);
        forget old revocations (oldest first)."""
        with self._lock:
            expired = [cid for cid, b in self._bindings.items() if b.expires_at is not None and b.expires_at <= now]
            if expired:
                data = ReplyCord.event("session_revoked", {"reason": "expired"}).pack()
                for cid in expired:
                    self._send(cid, data)
                    self._unbind_locked(cid, linger=False)
            cutoff = time.monotonic() - REVOKED_FENCE_SECONDS
            while self._revoked and next(iter(self._revoked.values())) < cutoff:
                self._revoked.popitem(last=False)

    def tick(self, now: float) -> None:
        """Called often by the networking thread: announce offline once the grace period ends."""
        if not self._lingering:
            return
        with self._lock:
            while self._lingering:
                user_id, deadline = next(iter(self._lingering.items()))
                if deadline > now:
                    break
                del self._lingering[user_id]
                who = self._profiles.pop(user_id, None) or OnlineUser(user_id, "")
                self._push_presence_locked(who, False)

    def _unbind_locked(self, conn_id: int, linger: bool) -> None:
        binding = self._bindings.pop(conn_id, None)
        if binding is None:
            return
        conns = self._user_conns.get(binding.user_id)
        if conns is None:
            return
        conns.discard(conn_id)
        if conns:
            return
        del self._user_conns[binding.user_id]
        if linger:
            self._lingering[binding.user_id] = time.monotonic() + LINGER_SECONDS
            return
        who = self._profiles.pop(binding.user_id, None) or OnlineUser(binding.user_id, "")
        self._push_presence_locked(who, False)

    def _push_presence_locked(self, who: OnlineUser, online: bool, exclude_conn: Optional[int] = None) -> None:
        self._seq += 1
        event = ReplyCord.event("presence", {"user": who.as_dict(online), "online": online, "seq": self._seq})
        data = event.pack()
        for cid in self._bindings:
            if cid != exclude_conn:
                self._send(cid, data)

    def update_profile(self, user: OnlineUser) -> None:
        with self._lock:
            if user.user_id in self._profiles:
                self._profiles[user.user_id] = user

    # ------------------------------------------------------------------ queries
    def user_of(self, conn_id: int) -> Optional[UUID]:
        with self._lock:
            b = self._bindings.get(conn_id)
            return b.user_id if b else None

    def is_online(self, user_id: UUID) -> bool:
        with self._lock:
            return user_id in self._user_conns or user_id in self._lingering

    def online_users(self) -> List[OnlineUser]:
        return self.online_snapshot()[0]

    def online_snapshot(self) -> Tuple[List[OnlineUser], int]:
        """(online users, presence seq at that moment): a client can ignore
        event/presence whose seq is not newer than the snapshot's."""
        with self._lock:
            users = [self._profiles[uid] for uid in (set(self._user_conns) | set(self._lingering))
                     if uid in self._profiles]
            return sorted(users, key=lambda u: u.username.lower()), self._seq

    def connections_of(self, user_id: UUID) -> Set[int]:
        with self._lock:
            return set(self._user_conns.get(user_id, ()))

    # ------------------------------------------------------------------ pushing
    def push(self, user_ids: Iterable[UUID], reply: ReplyCord, exclude_conn: Optional[int] = None) -> int:
        """Sends `reply` to every live connection of the given users. Returns how many got it."""
        data = reply.pack()                       # pack outside the lock
        wanted = set(user_ids)
        with self._lock:
            targets = {cid for uid in wanted for cid in self._user_conns.get(uid, ())}
            targets.discard(exclude_conn)
            for cid in targets:
                self._send(cid, data)
        return len(targets)

    def push_all(self, reply: ReplyCord, exclude_conn: Optional[int] = None) -> int:
        data = reply.pack()
        with self._lock:
            targets = [cid for cid in self._bindings if cid != exclude_conn]
            for cid in targets:
                self._send(cid, data)
        return len(targets)

    def push_conn(self, conn_id: int, reply: ReplyCord) -> None:
        self._send(conn_id, reply.pack())

    def close_conn(self, conn_id: int, reason: str) -> None:
        self._close(conn_id, reason)
