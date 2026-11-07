"""Multi-connection TCP server: the main networking thread.

One thread owns every socket and runs a `selectors` event loop over:

* the listening ("welcoming") socket -> accept() a new connection socket;
* each connection socket             -> recv() whatever arrived into that
                                        connection's RequestParser;
* a wake-up socketpair               -> worker threads poke it when a reply or
                                        a push is waiting to be sent.

Complete requests go to a thread pool, because handlers may block on the
database. A handler returns a ReplyCord; this thread packs it and writes it to
the connection the request came from. Requests from one connection run one at
a time and in order, so replies come back in the order the requests were sent.
Pushes (new message, someone came online, ...) can interleave between replies.

Protection, all per connection unless noted:

* Flow control: complete packets are taken out of the parser only while fewer
  than `max_pending` (or `max_pending_bytes`) wait, and the next request starts
  only while less than `outbuf_high_water` of reply is unsent. Otherwise the
  socket is not read: TCP's receive window fills and the client's send()
  blocks, so a flood slows its sender instead of filling the server's memory.
* Pushes waiting for a client that does not read are capped
  (`slow_consumer_limit`), and a client whose unsent data makes no progress
  for `send_stall_timeout` is dropped.
* Connections that never complete a request, sit on half a packet, or idle
  without logging in are closed; one address may hold at most
  `max_connections_per_ip` connections; when the server is full, the longest
  idle connection that never logged in makes room for the new one.
* A client that half-closes (shutdown(SHUT_WR)) still gets every reply.
"""
from __future__ import annotations

import errno
import ipaddress
import itertools
import logging
import queue
import selectors
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

from .protocol import METHOD_NAMES, Frame, ReplyCord, RequestParser, Status

log = logging.getLogger("socketcord.net")

READ, WRITE = selectors.EVENT_READ, selectors.EVENT_WRITE
_ACCEPT_RESOURCE_ERRORS = {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM}
# Notice a peer that vanished (Wi-Fi drop, sleep) after ~2 minutes instead of ~2 hours.
_KEEPALIVE_OPTIONS = (("TCP_KEEPIDLE", 60), ("TCP_KEEPALIVE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4))


@dataclass(frozen=True)
class Peer:
    """Who sent a request: the connection id and the TCP source address."""

    conn_id: int
    ip: str
    port: int

    def __str__(self) -> str:
        return f"#{self.conn_id} {self.ip}:{self.port}"


def normalize_ip(raw: str) -> str:
    host = raw.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def fd_budget(requested: int) -> int:
    """How many connections this process can really hold.

    Raises the soft file-descriptor limit as far as allowed, keeps a margin for
    the database pool and the listener, and respects select()'s FD_SETSIZE on
    platforms (Windows) where DefaultSelector is SelectSelector.
    """
    limit = requested
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        for want in (hard, 65536, 10240, 4096):
            if want == resource.RLIM_INFINITY or want <= soft or (hard != resource.RLIM_INFINITY and want > hard):
                continue
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
                soft = want
                break
            except (ValueError, OSError):
                continue
        limit = min(limit, max(16, soft - 64))
    except ImportError:          # Windows has no resource module
        pass
    if selectors.DefaultSelector is selectors.SelectSelector:
        limit = min(limit, 500)
    return limit


class Connection:
    __slots__ = ("peer", "sock", "parser", "outbuf", "segments", "push_bytes", "pending", "pending_bytes",
                 "busy", "closing", "eof", "mask", "opened_at", "last_frame_at", "last_progress", "frames_seen",
                 "partial_since")

    def __init__(self, peer: Peer, sock: socket.socket, now: float) -> None:
        self.peer = peer
        self.sock = sock
        self.parser = RequestParser()
        self.outbuf = bytearray()
        self.segments: Deque[List] = deque()   # [is_push, bytes left] for what sits in outbuf
        self.push_bytes = 0                    # unsent push bytes (replies do not count)
        self.pending: Deque[Frame] = deque()
        self.pending_bytes = 0                 # estimated memory of the requests waiting in `pending`
        self.busy = False                      # a request from this connection is being handled
        self.closing = False                   # close once outbuf is flushed
        self.eof = False                       # the peer finished sending (half-close)
        self.mask = 0                          # events registered with the selector (0 = not registered)
        self.opened_at = self.last_frame_at = self.last_progress = now
        self.frames_seen = 0
        self.partial_since: Optional[float] = None   # when the bytes now waiting in the parser started to wait


class SocketCordServer:
    RECV_SIZE = 65536

    def __init__(self, app, host: str = "0.0.0.0", port: int = 8080, workers: int = 16,
                 max_connections: int = 1024, max_connections_per_ip: int = 64,
                 max_pending: int = 32, max_pending_bytes: int = 1 << 20, parser_buffer_limit: int = 256 << 10,
                 outbuf_high_water: int = 1 << 20, slow_consumer_limit: int = 8 << 20,
                 send_stall_timeout: float = 60.0, first_request_timeout: float = 30.0,
                 partial_frame_timeout: float = 30.0, idle_unauthenticated_timeout: float = 300.0,
                 housekeeping_interval: float = 5.0, drain_timeout: float = 5.0, backlog: int = 128) -> None:
        self.app = app
        self.host, self.port = host, port
        self.max_connections = fd_budget(max_connections)
        self.max_connections_per_ip = max_connections_per_ip
        self.max_pending = max_pending
        self.max_pending_bytes = max_pending_bytes
        self.parser_buffer_limit = parser_buffer_limit
        self.outbuf_high_water = outbuf_high_water
        self.slow_consumer_limit = slow_consumer_limit
        self.send_stall_timeout = send_stall_timeout
        self.first_request_timeout = first_request_timeout
        self.partial_frame_timeout = partial_frame_timeout
        self.idle_unauthenticated_timeout = idle_unauthenticated_timeout
        self.housekeeping_interval = housekeeping_interval
        self.drain_timeout = drain_timeout
        self.backlog = backlog
        self.address: Optional[Tuple[str, int]] = None

        self._sel = selectors.DefaultSelector()
        self._lsock: Optional[socket.socket] = None
        self._listening = False
        self._listener_paused_until = 0.0
        self._conns: Dict[int, Connection] = {}
        self._per_ip: Dict[str, int] = {}
        self._ids = itertools.count(1)
        self._outq: "queue.SimpleQueue[Tuple[str, int, object]]" = queue.SimpleQueue()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._wake_pending = False
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="handler")
        self._stop = False          # set by shutdown(); a plain flag, so a signal handler may set it
        self._stopping = False      # draining before exit: no new reads, no pushes
        self._warned: Dict[str, Tuple[float, int]] = {}
        self.stopped = threading.Event()
        app.attach(self)

    # ------------------------------------------------------------------ thread-safe API
    def send(self, conn_id: int, data: bytes) -> None:
        """Queue a push for a connection. Safe to call from any thread."""
        if self._stopping:
            return
        self._outq.put(("push", conn_id, data))
        self._wake()

    def close_connection(self, conn_id: int, reason: str = "closed by server") -> None:
        self._outq.put(("close", conn_id, reason))
        self._wake()

    def shutdown(self) -> None:
        """Stop serving. Takes no lock, so it is safe in a signal handler."""
        self._stop = True
        self._wake()

    def connection_count(self) -> int:
        return len(self._conns)

    # ------------------------------------------------------------------ setup
    def bind(self) -> Tuple[str, int]:
        family, socktype, proto, _, sockaddr = socket.getaddrinfo(
            self.host, self.port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[0]
        lsock = socket.socket(family, socktype, proto)
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
            try:
                lsock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # accept IPv4 too
            except OSError:
                pass
        lsock.bind(sockaddr)
        lsock.listen(self.backlog)
        lsock.setblocking(False)
        self._lsock = lsock
        self._sel.register(self._wake_r, READ, data="wake")
        self._start_listening()
        self.address = lsock.getsockname()[:2]
        return self.address

    def _start_listening(self) -> None:
        if self._lsock is not None and not self._listening:
            self._sel.register(self._lsock, READ, data="listen")
            self._listening = True

    def _stop_listening(self, pause: float = 0.0) -> None:
        if self._listening:
            self._sel.unregister(self._lsock)
            self._listening = False
        self._listener_paused_until = time.monotonic() + pause

    # ------------------------------------------------------------------ event loop
    def serve_forever(self) -> None:
        if self._lsock is None:
            self.bind()
        log.info("listening on %s:%d (up to %d connections)", self.address[0], self.address[1],
                 self.max_connections)
        last_housekeeping = time.monotonic()
        try:
            while not self._stop:
                self._loop_once(min(0.5, self.housekeeping_interval))
                now = time.monotonic()
                if now - last_housekeeping >= self.housekeeping_interval:
                    last_housekeeping = now
                    self._housekeeping(now)
            self._drain_before_exit()
        finally:
            self._teardown()

    def _loop_once(self, timeout: float) -> None:
        try:
            events = self._sel.select(timeout=timeout)
        except (OSError, ValueError) as exc:        # e.g. more sockets than select() can watch
            self._warn("select", "select() failed: %s", exc)
            time.sleep(0.1)
            events = []
        for key, mask in events:
            try:
                if key.data == "listen":
                    self._accept()
                elif key.data == "wake":
                    self._drain_wake()
                else:
                    self._service(key.data, mask)
            except Exception:
                # Whatever one socket does, the server keeps serving the others.
                log.exception("error while handling a socket event")
                if isinstance(key.data, Connection):
                    self._close(key.data, "internal error")
        self._drain_outq()
        if not self._listening and not self._stopping and time.monotonic() >= self._listener_paused_until:
            self._start_listening()
        try:
            self.app.tick()
        except Exception:
            log.exception("tick failed")

    def _accept(self) -> None:
        for _ in range(64):  # accept a burst, then go back to serving the others
            try:
                sock, addr = self._lsock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                if exc.errno in _ACCEPT_RESOURCE_ERRORS:
                    # Out of descriptors: the connection stays queued, so the listener
                    # would be "readable" forever. Stop watching it for a moment.
                    self._stop_listening(pause=1.0)
                    self._warn("accept", "accept() failed (%s); pausing new connections for 1 s", exc)
                else:
                    self._warn("accept-other", "accept() failed: %s", exc)
                return
            try:
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # small chat packets: skip Nagle
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for name, value in _KEEPALIVE_OPTIONS:
                    if hasattr(socket, name):
                        try:
                            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
                        except OSError:
                            pass
                ip = normalize_ip(addr[0])
                peer = Peer(next(self._ids), ip, addr[1])
            except OSError as exc:
                # The peer can reset the connection before we touch it (macOS then
                # fails setsockopt with EINVAL). Drop it and carry on.
                self._warn("setup", "dropping %s during setup: %s", addr, exc)
                sock.close()
                continue
            if self._per_ip.get(ip, 0) >= self.max_connections_per_ip:
                self._warn("per-ip", "refusing %s: %d connections from that address already",
                           addr, self.max_connections_per_ip)
                sock.close()
                continue
            if len(self._conns) >= self.max_connections and not self._evict_idle_stranger():
                self._warn("full", "refusing %s: %d connections already", addr, len(self._conns))
                sock.close()
                continue
            conn = Connection(peer, sock, time.monotonic())
            self._conns[peer.conn_id] = conn
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            self.app.on_connect(peer)
            self._update_interest(conn)
            log.info("conn %s opened", peer)

    def _evict_idle_stranger(self) -> bool:
        """Server full: drop the longest-idle connection that never logged in."""
        idle = [c for c in self._conns.values()
                if not c.busy and not c.pending and not c.outbuf and not self.app.is_authenticated(c.peer.conn_id)]
        if not idle:
            return False
        self._close(min(idle, key=lambda c: c.last_frame_at), "server full; dropped an idle connection")
        return True

    def _service(self, conn: Connection, mask: int) -> None:
        if mask & READ:
            self._read(conn)
        if mask & WRITE and conn.peer.conn_id in self._conns:
            self._flush(conn)

    def _read(self, conn: Connection) -> None:
        try:
            data = conn.sock.recv(self.RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            self._close(conn, f"recv failed: {exc}")
            return
        if not data:
            # The peer finished sending. It may still be reading: answer what it
            # already sent, then close (see _finish_if_done).
            conn.eof = True
            self._update_interest(conn)
            self._finish_if_done(conn)
            return
        # One recv() may hold a fragment, one packet, or several packets.
        conn.parser.append(data)
        self._fill_pending(conn)
        if conn.parser.buffered and conn.partial_since is None:
            conn.partial_since = time.monotonic()
        self._pump(conn)
        self._update_interest(conn)

    def _fill_pending(self, conn: Connection) -> None:
        """Move complete packets from the parser into the queue, up to the limits;
        the rest waits in the parser (and then in the kernel)."""
        now = None
        while len(conn.pending) < self.max_pending and conn.pending_bytes < self.max_pending_bytes:
            frame = conn.parser.next_frame()
            if frame is None:
                break
            conn.pending.append(frame)
            conn.pending_bytes += frame.cost
            conn.frames_seen += 1
            now = now or time.monotonic()
            conn.last_frame_at = now
        if now is not None:
            conn.partial_since = now if conn.parser.buffered else None

    def _pump(self, conn: Connection) -> None:
        """Hand the next queued request of this connection to a worker."""
        if (conn.busy or conn.closing or self._stopping or not conn.pending
                or conn.peer.conn_id not in self._conns or len(conn.outbuf) >= self.outbuf_high_water):
            return
        frame = conn.pending.popleft()
        conn.pending_bytes -= frame.cost
        conn.busy = True
        self._pool.submit(self._handle, conn.peer, frame)
        self._fill_pending(conn)

    def _handle(self, peer: Peer, frame: Frame) -> None:
        """Runs in a worker thread."""
        reply = None
        try:
            reply = self.app.dispatch(peer, frame)
            data = reply.pack()
        except Exception:
            log.exception("could not answer %s", peer)
            head = {"path": frame.url_hint.strip().strip("/")[:256],
                    "method": METHOD_NAMES.get(frame.method, str(frame.method))}
            if reply is not None:  # keep the context so the client can still match the reply
                # Not its "path": the reply this replaces never went out, and an error
                # does not claim the semantic name of a call that succeeded.
                head.update({k: reply.data[k] for k in ("method", "rid") if k in reply.data})
            head.update(error="INTERNAL_ERROR", detail="unexpected server error")
            data = ReplyCord.from_dict(Status.INTERNAL_ERROR, head).pack()
        self._outq.put(("reply", peer.conn_id, data))
        self._wake()

    def _drain_outq(self) -> None:
        while True:
            try:
                kind, conn_id, payload = self._outq.get_nowait()
            except queue.Empty:
                return
            conn = self._conns.get(conn_id)
            if conn is None:
                continue  # the connection closed while this was in flight
            try:
                if kind == "reply":
                    conn.busy = False
                    self._queue_bytes(conn, payload, is_push=False)
                    self._pump(conn)
                    self._finish_if_done(conn)
                elif kind == "push":
                    self._queue_bytes(conn, payload, is_push=True)
                elif kind == "close":
                    conn.closing = True
                    if not conn.outbuf:
                        self._close(conn, str(payload))
                if conn.peer.conn_id in self._conns:
                    self._update_interest(conn)
            except Exception:
                log.exception("error while sending to %s", conn.peer)
                self._close(conn, "internal error")

    def _queue_bytes(self, conn: Connection, data: bytes, is_push: bool) -> None:
        if conn.peer.conn_id not in self._conns:
            return
        # A reply is always accepted: there is at most one in flight per connection
        # and the next request waits until outbuf drains. Pushes are what pile up
        # when a client stops reading, so only they count toward the limit.
        if is_push:
            if self._stopping:
                return
            if conn.push_bytes + len(data) > self.slow_consumer_limit:
                self._close(conn, "client is not reading what it is sent")
                return
            conn.push_bytes += len(data)
        if not conn.outbuf:
            conn.last_progress = time.monotonic()
        conn.outbuf += data
        conn.segments.append([is_push, len(data)])
        self._flush(conn)

    def _flush(self, conn: Connection) -> None:
        while conn.outbuf:
            try:
                sent = conn.sock.send(conn.outbuf)
            except (BlockingIOError, InterruptedError):
                break  # kernel send buffer full; wait for EVENT_WRITE
            except OSError as exc:
                self._close(conn, f"send failed: {exc}")
                return
            if sent <= 0:
                break
            del conn.outbuf[:sent]
            self._consumed(conn, sent)
            conn.last_progress = time.monotonic()
        if conn.closing and not conn.outbuf:
            self._close(conn, "closed by server")
            return
        self._pump(conn)  # outbuf may have dropped below the high-water mark
        self._finish_if_done(conn)
        self._update_interest(conn)

    @staticmethod
    def _consumed(conn: Connection, n: int) -> None:
        while n > 0 and conn.segments:
            segment = conn.segments[0]
            take = min(n, segment[1])
            segment[1] -= take
            n -= take
            if segment[0]:
                conn.push_bytes -= take
            if segment[1] == 0:
                conn.segments.popleft()

    def _finish_if_done(self, conn: Connection) -> None:
        """After a half-close: once every request it sent is answered and flushed, close."""
        if not conn.eof or conn.peer.conn_id not in self._conns or conn.busy or conn.outbuf:
            return
        if not conn.pending:
            self._fill_pending(conn)
        if conn.pending:
            self._pump(conn)
            return
        self._close(conn, "peer closed the connection")

    def _update_interest(self, conn: Connection) -> None:
        if conn.peer.conn_id not in self._conns:
            return
        wants_read = (not conn.closing and not conn.eof and not self._stopping
                      and len(conn.pending) < self.max_pending and conn.pending_bytes < self.max_pending_bytes
                      and conn.parser.buffered < self.parser_buffer_limit)
        mask = (READ if wants_read else 0) | (WRITE if conn.outbuf else 0)
        if mask == conn.mask:
            return
        if mask & READ and not conn.mask & READ and conn.partial_since is not None:
            conn.partial_since = time.monotonic()   # it could not send while we were not reading
        if conn.mask == 0:
            self._sel.register(conn.sock, mask, data=conn)
        elif mask == 0:
            self._sel.unregister(conn.sock)   # flow control: leave the bytes in the kernel for now
        else:
            self._sel.modify(conn.sock, mask, data=conn)
        conn.mask = mask

    def _close(self, conn: Connection, reason: str) -> None:
        if self._conns.pop(conn.peer.conn_id, None) is None:
            return
        left = self._per_ip.get(conn.peer.ip, 1) - 1
        if left > 0:
            self._per_ip[conn.peer.ip] = left
        else:
            self._per_ip.pop(conn.peer.ip, None)
        if conn.mask:
            try:
                self._sel.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
        conn.mask = 0
        try:
            conn.sock.close()
        except OSError:
            pass
        log.info("conn %s closed (%s)", conn.peer, reason)
        try:
            self.app.on_disconnect(conn.peer)
        except Exception:
            log.exception("on_disconnect failed for %s", conn.peer)

    # ------------------------------------------------------------------ periodic work
    def _housekeeping(self, now: float) -> None:
        try:
            self.app.housekeeping()
        except Exception:
            log.exception("housekeeping failed")
        is_authenticated = getattr(self.app, "is_authenticated", lambda conn_id: False)
        for conn in list(self._conns.values()):
            reason = None
            if conn.outbuf and now - conn.last_progress > self.send_stall_timeout:
                reason = f"nothing could be sent for {self.send_stall_timeout:.0f} s"
            elif conn.frames_seen == 0 and not conn.busy and now - conn.opened_at > self.first_request_timeout:
                reason = "no complete request after connecting"
            elif (conn.partial_since is not None and conn.mask & READ
                  and now - conn.partial_since > self.partial_frame_timeout):
                reason = "an incomplete packet for too long"
            elif (not conn.busy and not conn.pending and not conn.outbuf
                  and now - conn.last_frame_at > self.idle_unauthenticated_timeout
                  and not is_authenticated(conn.peer.conn_id)):
                reason = "idle without logging in"
            if reason:
                self._close(conn, reason)

    def _warn(self, key: str, message: str, *args) -> None:
        """At most one warning per key every 5 s, so a flood cannot fill the log."""
        now = time.monotonic()
        last, suppressed = self._warned.get(key, (0.0, 0))
        if now - last < 5.0:
            self._warned[key] = (last, suppressed + 1)
            return
        if suppressed:
            message += f" ({suppressed} similar messages suppressed)"
        log.warning(message, *args)
        self._warned[key] = (now, 0)

    # ------------------------------------------------------------------ wake-ups and shutdown
    def _wake(self) -> None:
        if self._wake_pending:
            return  # a wake-up is already on its way
        self._wake_pending = True
        try:
            self._wake_w.send(b"\0")
        except BlockingIOError:
            pass  # buffer full: bytes are waiting, so the loop wakes and clears the flag
        except OSError:
            # Nothing was queued (ENOBUFS, ENOMEM, EINTR, closed at shutdown). Clear the
            # flag, or every later _wake() is skipped with nothing left to read.
            self._wake_pending = False

    def _drain_wake(self) -> None:
        # Read every byte first, then clear the flag. The other order loses
        # wake-ups: a worker that sets the flag and sends its byte in between has
        # that byte read here, the flag stays set with the socket empty, every
        # later _wake() is skipped, and replies then wait for select()'s timeout.
        # In this order, a worker that still sees the flag set put its item in
        # _outq before the flag is cleared, and _loop_once drains _outq after this.
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            pass  # shutting down
        self._wake_pending = False

    def _drain_before_exit(self) -> None:
        """Stop accepting and reading, let running handlers finish and their replies
        go out (bounded by drain_timeout), then return to _teardown."""
        self._stopping = True
        self._stop_listening()
        for conn in list(self._conns.values()):
            self._update_interest(conn)
        deadline = time.monotonic() + self.drain_timeout
        while time.monotonic() < deadline and any(c.busy or c.outbuf for c in self._conns.values()):
            self._loop_once(0.05)

    def _teardown(self) -> None:
        detach = getattr(self.app, "detach", None)
        if detach is not None:
            detach()              # closing N connections must not queue N*N presence pushes
        self._stopping = True
        for conn in list(self._conns.values()):
            self._close(conn, "server shutting down")
        self._pool.shutdown(wait=False, cancel_futures=True)
        for sock in (self._lsock, self._wake_r, self._wake_w):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._sel.close()
        log.info("server stopped")
        self.stopped.set()
