"""Regression tests for the defects found in the first code review."""
from __future__ import annotations

import random
import socket
import struct
import threading
import time
import uuid

import pytest

from socketcord import scram
from socketcord.presence import OnlineUser, PresenceHub
from socketcord.protocol import GET, PATCH, POST, Frame, ReplyCord, RequestCord
from socketcord.router import parse_iso8601
from socketcord.server import Peer, SocketCordServer
from socketcord_client import ApiFailure


def err(exc_info) -> tuple:
    return exc_info.value.status, exc_info.value.data.get("error")


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------- the real entry point

def test_main_entry_point_serves(monkeypatch):
    """python -m socketcord: parse args/env, build, serve, stop on SIGTERM-like shutdown."""
    import socketcord.__main__ as entry
    from socketcord_client import SocketCordClient
    built = {}
    real_build = entry.build

    def spy_build(config):
        server = real_build(config)
        built["server"] = server
        return server

    monkeypatch.setattr(entry, "build", spy_build)
    monkeypatch.setattr(entry.signal, "signal", lambda *a: None)   # leave the test runner's handlers alone
    monkeypatch.setenv("SCRAM_ITERATIONS", "4096")
    runner = threading.Thread(target=entry.main, daemon=True,
                              args=(["--host", "127.0.0.1", "--port", "0", "--db", "memory",
                                     "--no-reg-limit", "--log-level", "WARNING"],))
    runner.start()
    assert _wait_until(lambda: "server" in built and built["server"].address is not None)
    server = built["server"]
    try:
        c = SocketCordClient(*server.address, min_iterations=4096)
        c.register("entry", "pw")
        c.login("entry", "pw")
        assert c.get("users/info")["user"]["username"] == "entry"
        c.close()
    finally:
        server.shutdown()          # whatever happened above, never leave the server running
        runner.join(5)
    assert not runner.is_alive()


# ---------------------------------------------------------------------- 1. TCP reset during accept

def test_reset_before_accept_does_not_stop_the_server(server):
    for _ in range(30):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close() sends RST
        s.connect(server.address)
        s.close()
    time.sleep(0.2)
    c = server.user("after_rst")
    assert c.get("users/info")["user"]["username"] == "after_rst"


# ---------------------------------------------------------------------- 2. the verifier stays secret

def test_verifier_never_crosses_the_wire(server):
    c = server.client()
    sent = bytearray()
    original = c.send_raw

    def spy(data: bytes) -> None:
        sent.extend(data)
        original(data)

    c.send_raw = spy
    c.register("sniffme", "first-password")
    first = server.app.repo.get_user_by_username("sniffme")
    c.login("sniffme", "first-password")
    c.change_password("first-password", "second-password")
    second = server.app.repo.get_user(first.user_id)
    assert second.stored_key != first.stored_key
    for secret in (first.stored_key, first.server_key, second.stored_key, second.server_key):
        assert secret not in sent and scram.b64e(secret).encode() not in sent
    # Neither the keys nor a proof that could be XORed with them are sent at registration.
    assert b'"stored_key"' not in sent and b'"new_client_proof"' not in sent


# ---------------------------------------------------------------------- 3. dead sessions get nothing

def test_logout_unbinds_every_connection_of_that_session(server):
    a = server.user("multi")
    twin = server.client()
    twin.user_id, twin.token = a.user_id, a.token
    twin.get("chats")                                   # same session, second connection
    watcher = server.user("watcher1")
    a.post("users/logout")
    watcher.wait_event("presence", match=lambda d: d["user"]["username"] == "multi" and not d["online"])
    assert "multi" not in {u["username"] for u in watcher.get("users/online")["users"]}
    assert twin.request(GET, "chats").status == 251


def test_expired_session_goes_offline_by_itself(make_server):
    srv = make_server(session_ttl_seconds=1)
    a = srv.user("shortlived")
    assert srv.app.hub.is_online(a.user_id)
    assert _wait_until(lambda: not srv.app.hub.is_online(a.user_id), timeout=4)
    assert a.request(GET, "users/info").status == 251


def test_revoked_session_cannot_be_bound_again():
    hub = PresenceHub()
    who = OnlineUser(uuid.uuid4(), "late")
    hub.connection_opened(1)
    assert hub.bind(1, who, b"h" * 32) == "ok"
    hub.revoke_sessions([b"h" * 32])
    assert not hub.is_online(who.user_id)
    assert hub.bind(1, who, b"h" * 32) == "revoked"     # a request still in flight cannot re-bind it
    assert not hub.is_online(who.user_id)


def test_session_needs_the_verifier_it_was_checked_against(repo):
    user = repo.create_user("saltcheck", b"s" * 16, 4096, b"k" * 32, b"v" * 32, None, None)
    expires = user.created_at.replace(year=user.created_at.year + 1)
    stale = scram.encode_verifier(b"x" * 16, 4096, b"k" * 32, b"v" * 32)
    assert repo.create_session(b"a" * 32, user.user_id, expires, expected_verifier=stale) is None
    assert repo.create_session(b"b" * 32, user.user_id, expires, expected_verifier=user.password_hash) is not None


# ---------------------------------------------------------------------- 4. throttle with pre-fetched challenges

def test_prefetched_challenges_do_not_buy_extra_guesses(server):
    server.client().register("throttled", "right-pw")
    prepared = []
    for c in [server.client() for _ in range(3)]:
        for _ in range(3):
            flow = scram.LoginFlow("throttled", "wrong-pw", min_iterations=4096)
            prepared.append((c, flow, c.call(POST, "users/login", flow.start(), auth=False)))
    errors = [c.request(POST, "users/login", flow.respond(ch), auth=False).data["error"]
              for c, flow, ch in prepared]
    limit = server.config.login_max_failures
    assert errors[:limit] == ["INVALID_CREDENTIALS"] * limit
    assert set(errors[limit:]) == {"TOO_MANY_ATTEMPTS"}


# ---------------------------------------------------------------------- 5. strings that cannot be stored

def test_lone_surrogates_and_nul_are_refused(server):
    c = server.user("unicode")
    gid = c.post("chats", {"name": "ok"})["chat"]["chat_id"]
    for url, method, body in (("users/profile", PATCH, '{"text_color":"\\ud800"}'),
                              (f"chat/{gid}/message", POST, '{"content":"a\\u0000b"}'),
                              ("chats", POST, '{"name":"x","\\udfff":1}')):
        c.send_raw(RequestCord(method=method, url=url, msg=body, user_id=c.user_id, ses_token=c.token).pack())
        reply = c.wait_unmatched()
        assert (reply.status, reply.data["error"]) == (250, "INVALID_JSON")
    assert c.get("users/online")["users"][0]["username"] == "unicode"   # nothing got poisoned


def test_fallback_error_reply_keeps_rid():
    class BrokenApp:
        def attach(self, server):
            pass

        def dispatch(self, peer, frame):
            return ReplyCord.from_dict(200, {"path": "chats", "rid": 9, "oops": "\ud800"})

    srv = SocketCordServer(BrokenApp(), "127.0.0.1", 0)
    try:
        srv._handle(Peer(1, "127.0.0.1", 1), Frame(url_hint="chats"))
        _, _, data = srv._outq.get_nowait()
        reply = ReplyCord.unpack(data)
        assert reply.status == 254 and reply.data["rid"] == 9 and reply.data["path"] == "chats"
    finally:
        srv._teardown()


# ---------------------------------------------------------------------- 6. buffers and flow control

def test_request_flood_is_flow_controlled_not_refused(server):
    c = server.user("flooder")
    n = 1500
    burst = b"".join(RequestCord.from_dict(GET, "users/info", {"rid": i}, user_id=c.user_id,
                                           ses_token=c.token).pack() for i in range(n))
    sender = threading.Thread(target=c.send_raw, args=(burst,))
    sender.start()
    for i in range(n):
        assert c.wait_reply(i, timeout=30).ok
    sender.join()


def test_wake_up_is_not_lost_when_a_worker_races_the_drain(server):
    """A worker can call _wake() at the moment the networking thread drains the
    wake-up socket. If the flag were cleared before reading, that worker's byte
    would be read with the flag left set; every later _wake() would be skipped and
    each reply would wait for select()'s timeout (0.2 s here, 0.5 s in production)."""
    c = server.user("racer")
    srv = server.server
    real = srv._wake_r

    class RacyWakeSocket:
        def recv(self, n):
            srv._wake()             # a worker's wake-up, at the worst moment
            return real.recv(n)

        def __getattr__(self, name):
            return getattr(real, name)

    srv._wake_r = RacyWakeSocket()
    try:
        c.get("users/info")         # the next drain goes through the race
        started = time.monotonic()
        for _ in range(20):
            c.get("users/info")
        elapsed = time.monotonic() - started
    finally:
        srv._wake_r = real
    assert elapsed < 1.0, f"20 round trips took {elapsed:.2f} s: a wake-up was lost"


def test_big_reply_is_not_treated_as_a_stuck_client(make_server):
    srv = make_server(server_kw={"slow_consumer_limit": 64 * 1024})
    c = srv.user("bigreader")
    gid = c.post("chats", {"name": "big"})["chat"]["chat_id"]
    for i in range(40):
        c.post(f"chat/{gid}/message", {"content": f"{i:04d}" + "x" * 3990})
    page = c.get(f"chat/{gid}/messages", {"count": 200})      # about 165 KB, more than the limit
    assert len(page["messages"]) == 40


def test_client_that_never_reads_is_dropped(make_server):
    srv = make_server(server_kw={"slow_consumer_limit": 64 * 1024})
    login = srv.user("never_reads")
    writer = srv.user("writer")
    gid = writer.post("chats", {"name": "firehose"})["chat"]["chat_id"]
    login.post(f"chat/{gid}/join")
    raw = socket.socket()
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    raw.connect(srv.address)
    raw.sendall(RequestCord.from_dict(GET, "chats", {}, user_id=login.user_id, ses_token=login.token).pack())
    time.sleep(0.2)
    login.close()                                        # now only the raw socket keeps it online
    assert _wait_until(lambda: len(srv.app.hub.connections_of(login.user_id)) == 1)
    for _ in range(400):
        writer.post(f"chat/{gid}/message", {"content": "y" * 4000})
        if not srv.app.hub.is_online(login.user_id):
            break
    assert _wait_until(lambda: not srv.app.hub.is_online(login.user_id))
    raw.close()


# ---------------------------------------------------------------------- 7. presence order

def test_presence_events_end_in_the_true_state():
    hub = PresenceHub()
    sent = []
    hub.attach(lambda cid, data: sent.append((cid, data)), lambda cid, reason: None)
    hub.connection_opened(0)
    hub.bind(0, OnlineUser(uuid.uuid4(), "observer", "observer"), None)
    users = [OnlineUser(uuid.uuid4(), f"u{i}", f"u{i}") for i in range(3)]

    def churn(seed: int) -> None:
        rng = random.Random(seed)
        for n in range(400):
            cid = 10_000 * (seed + 1) + n
            hub.connection_opened(cid)
            hub.bind(cid, users[rng.randrange(len(users))], None)
            if rng.random() < 0.8:
                hub.connection_closed(cid)

    threads = [threading.Thread(target=churn, args=(s,)) for s in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    last = {}
    for cid, data in sent:
        reply = ReplyCord.unpack(data)
        if cid == 0 and reply.path == "event/presence":
            last[reply.data["user"]["user_id"]] = reply.data["online"]
    for u in users:
        assert last.get(str(u.user_id), False) == hub.is_online(u.user_id)


# ---------------------------------------------------------------------- 8. DM consent

def test_dm_is_not_opened_if_their_request_was_just_cancelled(server, monkeypatch):
    a, b = server.user("racea"), server.user("raceb")
    repo = server.app.repo
    a.post(f"users/{b.user_id}/dm", {"message": "hi"})
    stale = repo.find_dm(a.user_id, b.user_id)            # a's pending DM request
    a.post(f"users/requests/{stale.chat_id}/cancel")
    # b's handler read a's request a moment before the cancel landed:
    real, calls = repo.find_dm, []

    def first_call_stale(x, y):
        calls.append(1)
        return stale if len(calls) == 1 else real(x, y)

    monkeypatch.setattr(repo, "find_dm", first_call_stale)
    out = b.post(f"users/{a.user_id}/dm", {"message": "hey"})
    monkeypatch.undo()
    assert out["status"] == "pending" and out["request"]["from"]["username"] == "raceb"
    now = repo.find_dm(a.user_id, b.user_id)
    assert now.status == "pending" and now.initiator == b.user_id   # a new request from b, no room


# ---------------------------------------------------------------------- 9. device ids cannot dodge the IP rule

def test_random_device_ids_still_hit_the_ip_limit(make_server):
    srv = make_server(reg_limit_enabled=True, reg_limit_device_max=1, reg_limit_ip_max=2)
    srv.client().register("rand1", "pw", device_id=f"device-{uuid.uuid4().hex[:12]}")
    srv.client().register("rand2", "pw", device_id=f"device-{uuid.uuid4().hex[:12]}")
    with pytest.raises(ApiFailure) as e:
        srv.client().register("rand3", "pw", device_id=f"device-{uuid.uuid4().hex[:12]}")
    assert err(e) == (252, "RATE_LIMITED")


# ---------------------------------------------------------------------- 10. parsing that differs between Pythons

def test_iso8601_parsing():
    utc10 = parse_iso8601("2026-11-05T10:00:00Z")
    assert parse_iso8601("2026-11-05T17:00:00+0700") == utc10
    assert parse_iso8601("2026-11-05 17:00+07:00") == utc10
    assert parse_iso8601("2026-11-05T10:00:00.1Z").microsecond == 100000
    assert parse_iso8601("2026-11-05").hour == 0
    for bad in ("2026-13-01", "yesterday", "2026-11-05T10:00:00+24:00", ""):
        assert parse_iso8601(bad) is None


def test_absurd_json_is_refused_quickly(server):
    c = server.user("jsonfuzz")
    for body in ('{"rid": 1' + "0" * 40 + "}", '{"count": NaN}', "[" * 5000 + "]" * 5000,
                 '{"a":' * 50 + "1" + "}" * 50):
        started = time.perf_counter()
        c.send_raw(RequestCord(method=GET, url="chats", msg=body, user_id=c.user_id, ses_token=c.token).pack())
        reply = c.wait_unmatched()
        assert (reply.status, reply.data["error"]) == (250, "INVALID_JSON")
        assert time.perf_counter() - started < 1.0


def test_ids_beyond_bigint_are_bad_requests(server):
    c = server.user("bigint")
    gid = c.post("chats", {"name": "ids"})["chat"]["chat_id"]
    for body in ({"before_id": 2 ** 63}, {"after_id": 2 ** 63}):
        assert c.request(GET, f"chat/{gid}/messages", body).status == 250
    assert c.request(POST, f"chat/{gid}/read", {"message_id": 2 ** 63}).status == 250


# ---------------------------------------------------------------------- PostgreSQL races (run on both backends)

def test_last_two_members_leaving_together_delete_the_group(server):
    a, b = server.user("leaver_a"), server.user("leaver_b")
    for i in range(6):
        gid = a.post("chats", {"name": f"pair {i}"})["chat"]["chat_id"]
        b.post(f"chat/{gid}/join")
        barrier = threading.Barrier(2)
        results = []

        def leave(client):
            barrier.wait()
            results.append(client.post(f"chat/{gid}/leave")["chat_deleted"])

        threads = [threading.Thread(target=leave, args=(c,)) for c in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [False, True]
        assert gid not in {g["chat_id"] for g in a.get("chats/groups")["groups"]}


def test_catch_up_by_after_id_never_skips_a_message(server):
    owner = server.user("catchup")
    gid = owner.post("chats", {"name": "busy"})["chat"]["chat_id"]
    posters = [server.user(f"poster{i}") for i in range(4)]
    for p in posters:
        p.post(f"chat/{gid}/join")
    per_poster = 25
    posted, seen = [], []
    done = threading.Event()

    def post_all(client):
        for j in range(per_poster):
            posted.append(client.post(f"chat/{gid}/message", {"content": f"m{j}"})["message"]["message_id"])

    # Read the starting point BEFORE any poster runs (reading it inside the poller
    # raced with the first posts and made this test flaky).
    start = max(m["message_id"] for m in owner.get(f"chat/{gid}/messages")["messages"])

    def poll():
        last = start
        while True:
            finished = done.is_set()
            for m in owner.get(f"chat/{gid}/messages", {"after_id": last, "count": 200})["messages"]:
                seen.append(m["message_id"])
                last = max(last, m["message_id"])
            if finished:
                return

    poller = threading.Thread(target=poll)
    poller.start()
    workers = [threading.Thread(target=post_all, args=(p,)) for p in posters]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    done.set()
    poller.join(10)
    assert sorted(seen) == sorted(posted) and len(posted) == 4 * per_poster


def test_invite_to_a_deleted_group_is_not_found(server):
    a, b = server.user("gone_a"), server.user("gone_b")
    gid = a.post("chats", {"name": "temporary"})["chat"]["chat_id"]
    rid = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]["request_id"]
    assert a.post(f"chat/{gid}/leave")["chat_deleted"] is True
    assert b.request(POST, f"users/requests/{rid}/accept").status == 253
