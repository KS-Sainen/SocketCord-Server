"""Regression tests for the third review round (networking, presence, abuse limits)."""
from __future__ import annotations

import socket
import threading
import time
import uuid

import pytest

from socketcord.presence import OnlineUser, PresenceHub
from socketcord.protocol import GET, PATCH, POST, ReplyCord, ReplyParser, RequestCord
from socketcord.views import chat_info_view
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


def _closed_by_server(sock: socket.socket, timeout: float = 3.0) -> bool:
    sock.settimeout(timeout)
    try:
        while True:
            if not sock.recv(65536):
                return True
    except (ConnectionResetError, BrokenPipeError):
        return True
    except socket.timeout:
        return False


# ---------------------------------------------------------------------- connection slots

def test_silent_connections_are_closed(make_server):
    srv = make_server(server_kw={"first_request_timeout": 0.5})
    silent = socket.create_connection(srv.address)
    assert _closed_by_server(silent, timeout=4)
    half = socket.create_connection(srv.address)
    half.sendall(b"\x00" * 20)                                  # never finishes the 48-byte header
    assert _closed_by_server(half, timeout=4)


def test_one_address_cannot_take_every_slot(make_server):
    srv = make_server(server_kw={"max_connections_per_ip": 3})
    keep = [socket.create_connection(srv.address) for _ in range(3)]
    time.sleep(0.2)
    extra = socket.create_connection(srv.address)
    assert _closed_by_server(extra)
    for s in keep:
        s.close()


def test_full_server_drops_an_idle_stranger_for_a_real_client(make_server):
    srv = make_server(server_kw={"max_connections": 3})
    strangers = [socket.create_connection(srv.address) for _ in range(3)]
    time.sleep(0.2)
    c = srv.user("newcomer")                                   # still gets in
    assert c.get("users/info")["user"]["username"] == "newcomer"
    for s in strangers:
        s.close()


def test_half_close_still_gets_every_reply(server):
    c = server.user("halfcloser")
    raw = socket.create_connection(server.address)
    burst = b"".join(RequestCord.from_dict(GET, "users/info", {"rid": i}, user_id=c.user_id,
                                           ses_token=c.token).pack() for i in range(3))
    raw.sendall(burst)
    raw.shutdown(socket.SHUT_WR)
    parser, replies = ReplyParser(), []
    raw.settimeout(5)
    while True:
        data = raw.recv(65536)
        if not data:
            break
        replies += parser.feed(data)
    assert sorted(r.data["rid"] for r in replies if isinstance(r, ReplyCord)) == [0, 1, 2]


# ---------------------------------------------------------------------- presence

def test_quick_reconnect_does_not_flap_presence(server):
    watcher = server.user("watcher3")
    a = server.user("flapper")
    watcher.drain_events()
    uid, token = a.user_id, a.token
    # The socket is opened before the old one closes, so the reconnect itself is one
    # request: on a loaded machine, opening it afterwards can take the whole grace period.
    again = server.client()
    again.user_id, again.token = uid, token
    a.close()
    again.get("chats")                                         # back within the grace period
    time.sleep(3)
    assert [e for e in watcher.drain_events() if e.path == "users/status"] == []
    again.close()
    ev = watcher.wait_event("users/status", timeout=6, match=lambda d: not d["online"])
    assert ev.data["user"]["username"] == "flapper" and ev.data["seq"] > 0


def test_online_list_carries_the_presence_seq(server):
    a = server.user("seqa")
    first = a.get("users/online")["presence_seq"]
    server.user("seqb")
    ev = a.wait_event("users/status", match=lambda d: d["online"])
    assert ev.data["seq"] > first and a.get("users/online")["presence_seq"] >= ev.data["seq"]


def test_ordinary_requests_cannot_flip_a_connection_between_accounts(server):
    a, b = server.user("flip_a"), server.user("flip_b")
    mixed = server.client()
    mixed.user_id, mixed.token = a.user_id, a.token
    mixed.get("chats")
    mixed.user_id, mixed.token = b.user_id, b.token
    reply = mixed.request(GET, "chats")
    assert (reply.status, reply.data["error"]) == (252, "CONNECTION_IN_USE")
    mixed.login("flip_b", "pw-flip_b-secret")                  # an explicit login may switch
    assert mixed.get("users/info")["user"]["username"] == "flip_b"


def test_connections_per_account_are_capped(make_server):
    srv = make_server(max_connections_per_user=2)
    first = srv.user("capper")
    second = srv.client()
    second.user_id, second.token = first.user_id, first.token
    second.get("chats")
    third = srv.client()
    third.user_id, third.token = first.user_id, first.token
    reply = third.request(GET, "chats")
    assert (reply.status, reply.data["error"]) == (252, "TOO_MANY_CONNECTIONS")
    third.login("capper", "pw-capper-secret")                  # a fresh login replaces the session:
    for old in (first, second):                                # every old connection goes, for that reason
        old.wait_event("login/revoked", match=lambda d: d["reason"] == "logged_in_elsewhere")
    assert third.get("chats") is not None


def test_shared_address_cannot_lock_out_real_users(server):
    server.client().register("realuser", "real-pw")
    c = server.client()
    for i in range(55):                                        # junk names from the same address
        reply = c.request(POST, "users/login", {"username": f"nobody{i}", "password": "x"}, auth=False)
        if reply.data.get("error") == "RATE_LIMITED":
            break
    stuck = c.request(POST, "users/login", {"username": "nobody-else", "password": "x"}, auth=False)
    assert (stuck.status, stuck.data["error"]) == (252, "RATE_LIMITED")
    server.client().login("realuser", "real-pw")               # the real account still works


# ---------------------------------------------------------------------- groups and messages

def test_group_updates_are_small_deltas(server):
    host, guest, watcher = server.user("dhost"), server.user("dguest"), server.user("dwatch")
    gid = host.post("chats", {"name": "delta"})["chat"]["chat_id"]
    watcher.drain_events()
    guest.post(f"chat/{gid}/join")
    ev = watcher.wait_event("chat/members_changed")
    assert ev.data["change"] == "joined" and ev.data["member_count"] == 2
    assert ev.data["user"]["username"] == "dguest" and "members" not in ev.data


def test_join_leave_messages_and_profile_are_rate_limited(make_server):
    srv = make_server(membership_rate_per_minute=60, message_rate_per_minute=60, profile_rate_per_minute=60)
    host, toggler = srv.user("rl_host"), srv.user("rl_toggler")
    gid = host.post("chats", {"name": "toggle"})["chat"]["chat_id"]
    statuses = []
    for i in range(12):
        statuses.append(toggler.request(POST, f"chat/{gid}/{'join' if i % 2 == 0 else 'leave'}").data.get("error"))
    assert "RATE_LIMITED" in statuses
    errors = [host.request(POST, f"chat/{gid}/message", {"content": f"m{i}"}).data.get("error") for i in range(32)]
    assert errors.count("RATE_LIMITED") >= 1
    errors = [host.request(PATCH, "users/profile", {"text_color": f"#00000{i}"}).data.get("error") for i in range(7)]
    assert "RATE_LIMITED" in errors



def test_search_text_is_short_and_literal(server):
    c = server.user("searcher")
    gid = c.post("chats", {"name": "search"})["chat"]["chat_id"]
    for text in ("100% sure", "snake_case", "plain"):
        c.post(f"chat/{gid}/message", {"content": text})
    assert [m["text"] for m in c.get(f"chat/{gid}/messages", {"content": "_"})["messages"]] == ["snake_case"]
    assert [m["text"] for m in c.get(f"chat/{gid}/messages", {"content": "%"})["messages"]] == ["100% sure"]
    assert c.request(GET, f"chat/{gid}/messages", {"content": "x" * 51}).status == 250


def test_member_lists_in_replies_are_capped():
    hub = PresenceHub()
    from socketcord.db.base import Chat, Member
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    chat = Chat(uuid.uuid4(), "group", "big", "", None, now)
    members = [Member(chat.chat_id, uuid.uuid4(), f"u{i}", "#171717", "#f2f2f2", "member", now) for i in range(5)]
    view = chat_info_view(chat, members, hub, None, max_members=2)
    assert len(view["members"]) == 2 and view["member_count"] == 5 and view["members_truncated"] is True


# ---------------------------------------------------------------------- DMs

def test_dm_sent_while_the_request_is_being_accepted_lands_in_the_room(make_server, monkeypatch):
    server = make_server(dm_require_accept=True)       # the first DM is a request
    a, b = server.user("acc_a"), server.user("acc_b")
    rid = a.post(f"users/{b.user_id}/dm", {"message": "hi"})["request"]["request_id"]
    stale = server.app.repo.find_dm(a.user_id, b.user_id)     # still pending when b's handler read it
    b.post(f"users/requests/{rid}/accept")                     # the room opens
    real_find_dm = server.app.repo.find_dm
    calls = {"n": 0}

    def stale_find_dm(x, y):                                   # b's other handler read "pending" first
        calls["n"] += 1
        return stale if calls["n"] == 1 else real_find_dm(x, y)

    monkeypatch.setattr(server.app.repo, "find_dm", stale_find_dm)
    out = b.post(f"users/{a.user_id}/dm", {"message": "t2"})
    monkeypatch.undo()
    assert out["status"] == "sent"
    room = server.app.repo.find_dm(a.user_id, b.user_id)
    assert [m["text"] for m in a.get(f"chat/{room.chat_id}/messages")["messages"]] == ["hi", "t2"]
    assert a.get("users/requests")["incoming"] == []


def test_answering_a_request_closed_elsewhere_says_how(make_server):
    server = make_server(dm_require_accept=True)       # the first DM is a request
    a, b = server.user("closed_a"), server.user("closed_b")
    rid = a.post(f"users/{b.user_id}/dm", {"message": "hi"})["request"]["request_id"]
    a.post(f"users/requests/{rid}/cancel")
    reply = b.request(POST, f"users/requests/{rid}/accept")
    assert (reply.status, reply.data["error"]) == (253, "REQUEST_NOT_FOUND")     # cancelled requests are deleted
    room_rid = a.post(f"users/{b.user_id}/dm", {"message": "again"})["request"]["request_id"]
    b.post(f"users/requests/{room_rid}/accept")
    reply = b.request(POST, f"users/requests/{room_rid}/accept")
    assert (reply.status, reply.data["error"]) == (252, "REQUEST_CLOSED") and "accepted" in reply.data["detail"]


# ---------------------------------------------------------------------- protocol details

def test_rejected_messages_still_echo_rid_and_short_paths(server):
    c = server.user("echoer")
    c.send_raw(RequestCord(method=POST, url="chats", msg='{"rid": 41, "name": "a\\u0000b"}', user_id=c.user_id,
                           ses_token=c.token).pack())
    reply = c.wait_reply(41)
    assert (reply.status, reply.data["error"]) == (250, "INVALID_JSON")
    long_url = "nope/" + "x" * 1000
    reply = c.request(GET, long_url)
    assert reply.status == 253 and len(reply.data["path"]) == 256 and "x" * 50 not in reply.data["detail"]
    big = c.request(POST, "chats", {"name": "x", "description": "y" * 40000})
    assert (big.status, big.data["error"]) == (250, "MESSAGE_TOO_LARGE")      # and its rid came back


def test_keepalive_is_tuned_on_accepted_sockets(server):
    c = server.user("keepalive")
    conns = list(server.server._conns.values())
    name = "TCP_KEEPIDLE" if hasattr(socket, "TCP_KEEPIDLE") else "TCP_KEEPALIVE"
    if not hasattr(socket, name):
        pytest.skip("no keepalive tuning on this platform")
    assert all(conn.sock.getsockopt(socket.IPPROTO_TCP, getattr(socket, name)) == 60 for conn in conns)
    del c


def test_shutdown_lets_running_requests_finish(server, monkeypatch):
    c = server.user("drainer")
    real = server.app.repo.list_user_chats

    def slow(user_id):
        time.sleep(0.6)
        return real(user_id)

    monkeypatch.setattr(server.app.repo, "list_user_chats", slow)
    rid = c.next_rid()
    c.send_raw(RequestCord.from_dict(GET, "chats", {"rid": rid}, user_id=c.user_id, ses_token=c.token).pack())
    time.sleep(0.2)
    server.server.shutdown()
    assert c.wait_reply(rid, timeout=5).ok
    assert server.server.stopped.wait(5)


def test_shutdown_before_serving_is_not_lost(repo):
    from conftest import make_config
    from socketcord.app import Application
    from socketcord.server import SocketCordServer
    cfg = make_config()
    srv = SocketCordServer(Application(cfg, repo), "127.0.0.1", 0)
    srv.bind()
    srv.shutdown()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    t.join(3)
    assert not t.is_alive()
