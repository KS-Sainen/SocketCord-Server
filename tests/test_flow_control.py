"""Flow control and wake-ups under awkward timing.

Timeouts are scaled down: partial_frame_timeout=1.0 s here stands for the
production 30 s, and the test harness runs housekeeping every 0.2 s
(production: 5 s).
"""
from __future__ import annotations

import errno
import logging
import os
import socket
import threading
import time

import pytest

from socketcord.protocol import GET, ReplyParser, RequestCord
from test_ai import FakeEngine, group_with


def _close_reasons(caplog):
    return [r.getMessage() for r in caplog.records if " closed (" in r.getMessage()]


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------- wake-ups

class FlakyWakeSocket:
    """The write end of the wake-up socketpair, failing once with ENOBUFS."""

    def __init__(self, real) -> None:
        self.real, self.failed = real, False

    def send(self, data, *flags):
        if not self.failed:
            self.failed = True
            raise OSError(errno.ENOBUFS, os.strerror(errno.ENOBUFS))
        return self.real.send(data, *flags)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_one_failed_wake_up_send_does_not_slow_every_later_reply(server):
    """If send() fails (ENOBUFS, ENOMEM, ...), nothing was queued: the flag must
    be cleared, or every later _wake() is skipped with nothing left to read."""
    c = server.user("nobufs")
    srv = server.server
    real = srv._wake_w
    srv._wake_w = FlakyWakeSocket(real)
    try:
        c.get("users/info")                 # this reply's wake-up fails; select()'s timeout delivers it
        started = time.monotonic()
        for _ in range(20):
            c.get("users/info")
        elapsed = time.monotonic() - started
    finally:
        srv._wake_w = real
    assert elapsed < 1.0, f"20 round trips took {elapsed:.2f} s after one failed wake-up send"


# ---------------------------------------------------------------------- the partial-frame timer

@pytest.mark.parametrize("idle", [0.0, 1.5])
def test_request_split_across_segments_after_idling(make_server, caplog, idle):
    """The timer starts when the half packet arrives, not at the last complete
    request: a client idle for a while whose next request comes in two TCP
    segments is answered, not dropped."""
    caplog.set_level(logging.INFO, logger="socketcord.net")
    srv = make_server(server_kw={"partial_frame_timeout": 1.0})
    c = srv.user(f"idler{int(idle * 10)}")
    time.sleep(idle)
    pkt = RequestCord.from_dict(GET, "users/info", {"rid": 777}, user_id=c.user_id, ses_token=c.token).pack()
    c.send_raw(pkt[:30])
    time.sleep(0.4)
    c.send_raw(pkt[30:])
    reply = c.wait_reply(777, timeout=3)
    assert reply.ok, _close_reasons(caplog)


@pytest.mark.parametrize("pipelined", [30, 40])
def test_pipelined_requests_behind_a_slow_one(make_server, caplog, pipelined):
    """While the server is not reading (flow control behind a slow request such
    as an AI summary), complete packets waiting in its buffer are not 'an
    incomplete packet for too long'."""
    caplog.set_level(logging.INFO, logger="socketcord.net")
    srv = make_server(server_kw={"partial_frame_timeout": 1.0})
    gate = threading.Event()
    fake = FakeEngine(gate=gate)
    srv.app.ai.engine = fake
    a = srv.user(f"piper{pipelined}")
    gid = group_with(a, ["hello"])
    pkts = [RequestCord.from_dict(GET, f"chat/{gid}/summary", {"rid": 0}, user_id=a.user_id,
                                  ses_token=a.token).pack()]
    pkts += [RequestCord.from_dict(GET, "users/info", {"rid": i}, user_id=a.user_id, ses_token=a.token).pack()
             for i in range(1, pipelined + 1)]
    a.send_raw(b"".join(pkts))
    assert fake.started.wait(5)
    time.sleep(2.0)                        # the summary takes 2 s (production: up to about 60 s)
    gate.set()
    for rid in range(pipelined + 1):
        a.wait_reply(rid, timeout=3)
    assert not _close_reasons(caplog)


@pytest.mark.parametrize("style", ["half-packet", "drip-feed"])
def test_partial_frame_holders_are_still_closed(make_server, caplog, style):
    caplog.set_level(logging.INFO, logger="socketcord.net")
    srv = make_server(server_kw={"partial_frame_timeout": 1.0})
    c = srv.user(f"slow{style[0]}")
    pkt = RequestCord.from_dict(GET, "users/info", {"rid": 5}, user_id=c.user_id, ses_token=c.token).pack()
    if style == "half-packet":
        c.send_raw(pkt[:40])
    else:
        def drip():
            for i in range(len(pkt) - 1):
                if c.closed:
                    return
                try:
                    c.send_raw(pkt[i:i + 1])
                except OSError:
                    return
                time.sleep(0.25)
        threading.Thread(target=drip, daemon=True).start()
    assert _wait_until(lambda: c.closed, 8.0)
    assert any("incomplete packet" in r for r in _close_reasons(caplog))


# ---------------------------------------------------------------------- no stalls

def test_half_close_after_a_flood_gets_every_reply_then_closes(server):
    c = server.user("halfcloser")
    n = 1500
    burst = b"".join(RequestCord.from_dict(GET, "users/info", {"rid": i}, user_id=c.user_id,
                                           ses_token=c.token).pack() for i in range(n))

    def send_then_half_close():
        c.send_raw(burst)
        c.sock.shutdown(socket.SHUT_WR)

    sender = threading.Thread(target=send_then_half_close)
    sender.start()
    for i in range(n):
        assert c.wait_reply(i, timeout=30).ok
    sender.join()
    assert _wait_until(lambda: c.closed, 5), "the server never closed the half-closed connection"


def test_flood_of_large_requests_hits_every_limit_and_still_finishes(server):
    c = server.user("bigflood")
    n = 400
    pad = "x" * 30000                      # ~30 KB each: the parser (256 KB) and pending-bytes (1 MB) limits
    burst = b"".join(RequestCord.from_dict(GET, "users/info", {"rid": i, "pad": pad}, user_id=c.user_id,
                                           ses_token=c.token).pack() for i in range(n))
    sender = threading.Thread(target=c.send_raw, args=(burst,))
    sender.start()
    for i in range(n):
        c.wait_reply(i, timeout=30)        # any reply proves nothing stalled
    sender.join()


def test_slow_reader_of_big_pipelined_replies(make_server):
    srv = make_server()
    c = srv.user("bigslow")
    gid = c.post("chats", {"name": "big"})["chat"]["chat_id"]
    for i in range(40):
        c.post(f"chat/{gid}/message", {"content": f"{i:04d}" + "x" * 3990})
    raw = socket.socket()
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
    raw.connect(srv.address)
    n = 14                                  # ~165 KB each, ~2.3 MB in all: more than outbuf_high_water
    raw.sendall(b"".join(RequestCord.from_dict(GET, f"chat/{gid}/messages", {"rid": i, "count": 200},
                                               user_id=c.user_id, ses_token=c.token).pack() for i in range(n)))
    parser, got = ReplyParser(), []
    raw.settimeout(10)
    while len(got) < n:
        chunk = raw.recv(16384)
        assert chunk, "the server closed the connection"
        got += [r for r in parser.feed(chunk) if not isinstance(r, Exception) and r.data.get("rid") is not None]
        time.sleep(0.002)
    raw.close()
    assert sorted(r.data["rid"] for r in got) == list(range(n))
