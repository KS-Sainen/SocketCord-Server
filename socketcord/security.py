"""Short-lived security state kept in memory: rate limits, login throttles, session hashing."""
from __future__ import annotations

import hashlib
import ipaddress
import threading
import time
import uuid
from collections import OrderedDict, deque
from typing import Deque, Tuple


def client_key(ip: str) -> str:
    """What rate limits count per: an IPv4 address, or an IPv6 /64 (one home or site)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


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
        """Check only, without recording an attempt. Never creates entries."""
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
