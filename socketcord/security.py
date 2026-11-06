"""Short-lived security state kept in memory: challenges, throttles, the server's DH key."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional, Tuple

from . import scram
from .db.base import UUID


def client_key(ip: str) -> str:
    """What rate limits count per: an IPv4 address, or an IPv6 /64 (one home or site)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


@dataclass
class Challenge:
    """State the server keeps between step 1 and step 2 of a challenge-response flow."""

    challenge_id: str
    kind: str                       # "login" | "register" | "password"
    conn_id: int                    # the answer must come back on the same TCP connection
    expires_at: float
    server_nonce: str = field(default_factory=scram.new_nonce)
    client_nonce: str = ""
    username: str = ""
    user_id: Optional[UUID] = None
    salt: bytes = b""
    iterations: int = 0
    new_salt: bytes = b""
    new_iterations: int = 0
    device_id: Optional[str] = None
    session_hash: Optional[bytes] = None
    dh_private: int = 0             # the server's DH key (register / password change)
    dh_public: str = ""             # base64 of g^dh_private mod p, as sent to the client


class ChallengeStore:
    """Challenges waiting for step 2, indexed by connection.

    Bounded: at most MAX_PER_CONNECTION per connection and MAX_TOTAL overall
    (the oldest go first). A challenge is never issued for a connection that
    already closed, so a client that sends step 1 and hangs up leaves nothing
    behind. Every operation is O(1) or O(expired).
    """

    MAX_PER_CONNECTION = 4
    MAX_TOTAL = 20_000

    def __init__(self, ttl_seconds: int, is_open: Optional[Callable[[int], bool]] = None) -> None:
        self.ttl = ttl_seconds
        self._is_open = is_open or (lambda conn_id: True)
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, Challenge]" = OrderedDict()   # issue order = expiry order
        self._by_conn: Dict[int, "OrderedDict[str, None]"] = {}

    def issue(self, kind: str, conn_id: int, **fields) -> Optional[Challenge]:
        """Returns None if the connection is already closed."""
        ch = Challenge(challenge_id=str(uuid.uuid4()), kind=kind, conn_id=conn_id,
                       expires_at=time.monotonic() + self.ttl, **fields)
        with self._lock:
            if not self._is_open(conn_id):
                return None
            mine = self._by_conn.setdefault(conn_id, OrderedDict())
            while len(mine) >= self.MAX_PER_CONNECTION:
                self._remove_locked(next(iter(mine)))
            while len(self._items) >= self.MAX_TOTAL:
                self._remove_locked(next(iter(self._items)))
            self._items[ch.challenge_id] = ch
            mine[ch.challenge_id] = None
        return ch

    def _remove_locked(self, challenge_id: str) -> Optional[Challenge]:
        ch = self._items.pop(challenge_id, None)
        if ch is not None:
            mine = self._by_conn.get(ch.conn_id)
            if mine is not None:
                mine.pop(challenge_id, None)
                if not mine:
                    del self._by_conn[ch.conn_id]
        return ch

    def take(self, challenge_id: str, kind: str, conn_id: int) -> Optional[Challenge]:
        """Single use: the challenge is gone after this call, whether or not it matched."""
        with self._lock:
            ch = self._remove_locked(str(challenge_id))
        if ch is None or ch.kind != kind or ch.conn_id != conn_id or ch.expires_at < time.monotonic():
            return None
        return ch

    def drop_connection(self, conn_id: int) -> None:
        with self._lock:
            for cid in list(self._by_conn.get(conn_id, ())):
                self._remove_locked(cid)

    def purge_expired(self) -> None:
        now = time.monotonic()
        with self._lock:
            while self._items:
                first = next(iter(self._items.values()))
                if first.expires_at >= now:
                    break
                self._remove_locked(first.challenge_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class ServerDHKey:
    """The server's half of the verifier-wrap Diffie-Hellman, reused for a short time.

    A 2048-bit DH key costs about 3 ms of CPU while holding the GIL. Making a
    fresh one for every challenge would let anyone stall the whole server by
    asking for registration challenges. Every client still uses a fresh key,
    so every shared secret (and therefore every pad) is still unique.
    """

    def __init__(self, lifetime_seconds: float = 60.0, max_uses: int = 200) -> None:
        self.lifetime = lifetime_seconds
        self.max_uses = max_uses
        self._lock = threading.Lock()
        self._current: Optional[Tuple[int, str]] = None
        self._born = 0.0
        self._uses = 0

    def get(self) -> Tuple[int, str]:
        """Returns (private exponent, base64 public value)."""
        with self._lock:
            now = time.monotonic()
            if self._current is None or now - self._born >= self.lifetime or self._uses >= self.max_uses:
                private, public = scram.dh_keypair()
                self._current, self._born, self._uses = (private, scram.dh_encode(public)), now, 0
            self._uses += 1
            return self._current


class RateLimiter:
    """Token bucket per key: `per_minute` tokens a minute, at most `burst` saved up.

    take() returns 0 when allowed, otherwise the seconds to wait. A rate of 0
    turns the limiter off. Memory is bounded: the least recently used keys are
    forgotten beyond `max_keys`.
    """

    def __init__(self, per_minute: float, burst: int, max_keys: int = 50_000) -> None:
        self.rate = per_minute / 60.0
        self.burst = max(1, burst)
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets: "OrderedDict[object, Tuple[float, float]]" = OrderedDict()

    def take(self, key: object) -> float:
        if self.rate <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.pop(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                tokens -= 1.0
                wait = 0.0
            else:
                wait = (1.0 - tokens) / self.rate
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return wait


class LoginGuard:
    """Throttles password guessing.

    * Real accounts: at most `max_failures` failed proofs per (username, client)
      per window. Only that username is affected, so nobody behind a shared
      address (NAT, Docker on a Mac) can lock other people out.
    * Usernames that do not exist: at most `max_unknown_per_client` failures
      per client per window, counted per client, not per name, so made-up
      names cannot fill memory or push real records out of the table.

    reserve() checks the limit and records the attempt in one step, so answers
    sent in parallel cannot all slip under it; a correct answer is taken back
    with succeeded(). Tables are bounded and purged oldest-first.
    """

    def __init__(self, max_failures: int, window_seconds: int, max_unknown_per_client: int = 50,
                 max_keys: int = 50_000) -> None:
        self.max_failures = max_failures
        self.max_unknown = max_unknown_per_client
        self.window = window_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._real: "OrderedDict[Tuple[str, str], Deque[float]]" = OrderedDict()
        self._unknown: "OrderedDict[str, Deque[float]]" = OrderedDict()

    def _table(self, real: bool) -> OrderedDict:
        return self._real if real else self._unknown

    def _limit(self, real: bool) -> int:
        return self.max_failures if real else self.max_unknown

    def _peek(self, table: OrderedDict, key: object, limit: int, now: float) -> float:
        hits = table.get(key)
        if not hits or limit <= 0:
            return 0.0
        recent = [t for t in hits if t > now - self.window]
        if len(recent) < limit:
            return 0.0
        return max(0.0, recent[len(recent) - limit] + self.window - now)

    def retry_after(self, username: str, client: str, real: bool) -> float:
        """Check only (before handing out a challenge). Never creates entries."""
        if self.max_failures <= 0:
            return 0.0
        key = (username, client) if real else client
        with self._lock:
            return self._peek(self._table(real), key, self._limit(real), time.monotonic())

    def reserve(self, username: str, client: str, real: bool) -> float:
        """0 = go ahead (the attempt counts as a failure until succeeded()); else seconds to wait."""
        if self.max_failures <= 0:
            return 0.0
        key = (username, client) if real else client
        table, limit = self._table(real), self._limit(real)
        now = time.monotonic()
        with self._lock:
            wait = self._peek(table, key, limit, now)
            if wait > 0:
                return wait
            hits = table.pop(key, None) or deque()
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            hits.append(now)
            table[key] = hits                 # most recently used last
            while len(table) > self.max_keys:
                table.popitem(last=False)
            return 0.0

    def succeeded(self, username: str, client: str) -> None:
        with self._lock:
            self._real.pop((username, client), None)

    def purge(self) -> None:
        """Entries are kept in last-attempt order, so this stops at the first live one."""
        cutoff = time.monotonic() - self.window
        with self._lock:
            for table in (self._real, self._unknown):
                while table:
                    key, hits = next(iter(table.items()))
                    if hits and hits[-1] > cutoff:
                        break
                    del table[key]


def token_hash(token: uuid.UUID) -> bytes:
    """Sessions are stored by hash, so a database dump does not contain live tokens."""
    return hashlib.sha256(token.bytes).digest()


def fake_salt(secret: bytes, username: str) -> bytes:
    """Stable salt for usernames that do not exist, so a login attempt looks the same either way."""
    return hmac.new(secret, f"fake-salt:{scram.canonical_username(username)}".encode(), hashlib.sha256).digest()[:scram.SALT_LEN]
