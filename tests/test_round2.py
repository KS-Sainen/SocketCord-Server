"""Regression tests for the second review round."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socketcord import scram
from socketcord.app import loggable
from socketcord.presence import OnlineUser, PresenceHub
from socketcord.protocol import GET, POST, RequestCord
from socketcord.security import LoginGuard, client_key
from socketcord_client import ApiFailure


def err(exc_info) -> tuple:
    return exc_info.value.status, exc_info.value.data.get("error")


# ---------------------------------------------------------------------- login throttle

def test_parallel_logins_cannot_exceed_the_failure_limit(server):
    server.client().register("parallel", "right-pw")
    clients = [server.client() for _ in range(10)]
    barrier = threading.Barrier(len(clients))
    errors = []

    def attempt(c):
        body = {"username": "parallel", "password": "wrong-pw"}
        barrier.wait()
        errors.append(c.request(POST, "users/login", body, auth=False).data["error"])

    threads = [threading.Thread(target=attempt, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors.count("INVALID_CREDENTIALS") == server.config.login_max_failures
    assert errors.count("RATE_LIMITED") == len(clients) - server.config.login_max_failures


def test_login_guard_limits_made_up_names_per_client_and_stays_small():
    guard = LoginGuard(max_failures=5, window_seconds=300, max_unknown_per_client=3, max_keys=10)
    assert [guard.reserve(f"nobody{i}", "10.0.0.1", real=False) for i in range(3)] == [0.0, 0.0, 0.0]
    assert guard.reserve("nobody99", "10.0.0.1", real=False) > 0     # 4th made-up name from that client
    assert guard.reserve("alice", "10.0.0.1", real=True) == 0.0      # a real account is not locked out
    assert guard.reserve("nobody99", "10.0.0.2", real=False) == 0.0  # another client is unaffected
    for i in range(500):
        guard.reserve(f"flood{i}", f"10.1.{i // 250}.{i % 250}", real=False)
    assert len(guard._real) <= 10 and len(guard._unknown) <= 10


def test_client_key_groups_ipv6_by_slash_64():
    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert client_key("203.0.113.7") == "203.0.113.7"


# ---------------------------------------------------------------------- sessions

def test_one_session_per_account_a_new_login_ends_the_old_one(server):
    server.client().register("single", "pw")
    first, second = server.client(), server.client()
    first.login("single", "pw")
    second.login("single", "pw")
    ev = first.wait_event("login/revoked")
    assert ev.data["reason"] == "logged_in_elsewhere"
    assert first.request(GET, "users/info").status == 251
    assert second.get("users/info")["user"]["username"] == "single"


def test_change_credentials_is_atomic(repo):
    user = repo.create_user("atomic", b"s" * 16, 4096, b"k" * 32, b"v" * 32, None, None)
    expires = datetime.now(timezone.utc) + timedelta(days=1)
    assert repo.create_session(b"1" * 32, user.user_id, expires, expected_verifier=user.password_hash)
    stale = scram.encode_verifier(b"w" * 16, 4096, b"k" * 32, b"v" * 32)
    assert repo.change_credentials(user.user_id, b"n" * 16, 4096, b"K" * 32, b"V" * 32, expected_verifier=stale,
                                   current_token_hash=b"1" * 32, new_token_hash=b"2" * 32,
                                   expires_at=expires) is None
    assert repo.get_session(b"1" * 32) is not None and repo.get_user(user.user_id).salt == b"s" * 16  # nothing happened
    assert repo.change_credentials(user.user_id, b"n" * 16, 4096, b"K" * 32, b"V" * 32,
                                   expected_verifier=user.password_hash, current_token_hash=b"9" * 32,
                                   new_token_hash=b"2" * 32, expires_at=expires) is None       # not the current session
    session = repo.change_credentials(user.user_id, b"n" * 16, 4096, b"K" * 32, b"V" * 32,
                                      expected_verifier=user.password_hash, current_token_hash=b"1" * 32,
                                      new_token_hash=b"2" * 32, expires_at=expires)
    assert session.token_hash == b"2" * 32
    assert repo.get_user(user.user_id).salt == b"n" * 16
    assert repo.get_session(b"1" * 32) is None and repo.get_session(b"2" * 32)[1].user_id == user.user_id


def test_bind_refuses_an_already_expired_session_and_keeps_newer_profile():
    hub = PresenceHub()
    hub.connection_opened(1)
    who = OnlineUser(uuid.uuid4(), "late")
    assert hub.bind(1, who, b"x" * 32, datetime.now(timezone.utc) - timedelta(seconds=1)) == "expired"
    assert hub.bind(1, who, b"x" * 32, datetime.now(timezone.utc) + timedelta(hours=1)) == "ok"
    hub.update_profile(OnlineUser(who.user_id, "late", "#ffffff", "#000000"))
    hub.bind(1, who, b"x" * 32, None)                     # a request that read the old row
    assert hub.online_users()[0].text_color == "#ffffff"


# ---------------------------------------------------------------------- groups, invites, DMs

def test_group_creation_is_rate_limited(make_server):
    srv = make_server(group_create_per_hour=60)           # burst of 5
    c = srv.user("creator")
    for i in range(5):
        c.post("chats", {"name": f"g{i}"})
    with pytest.raises(ApiFailure) as e:
        c.post("chats", {"name": "one too many"})
    assert err(e) == (252, "RATE_LIMITED")


def test_group_list_is_paged(server):
    c = server.user("pager")
    ids = [c.post("chats", {"name": f"page {i}"})["chat"]["chat_id"] for i in range(5)]
    first = c.get("chats/groups", {"count": 2})
    assert [g["chat_id"] for g in first["groups"]] == ids[:2] and first["has_more"] is True
    last = c.get("chats/groups", {"count": 2, "offset": 4})
    assert [g["chat_id"] for g in last["groups"]] == ids[4:] and last["has_more"] is False


def test_control_characters_are_refused_in_names_but_newlines_ok_in_messages(server):
    c = server.user("controls")
    assert c.request(POST, "chats", {"name": "bad\u0001name"}).status == 250
    assert c.request(POST, "chats", {"name": "two\nlines"}).status == 250
    gid = c.post("chats", {"name": "fine", "description": "line 1\nline 2"})["chat"]["chat_id"]
    assert c.post(f"chat/{gid}/message", {"content": "hello\nworld\t!"})["message"]["text"] == "hello\nworld\t!"
    assert c.request(POST, f"chat/{gid}/message", {"content": "bell\u0007"}).status == 250


def test_simultaneous_dms_end_in_one_room_with_both_messages(make_server):
    server = make_server(dm_require_accept=True)       # the first DM is a request
    a, b = server.user("cross_a"), server.user("cross_b")
    barrier = threading.Barrier(2)
    replies = {}

    def dm(me, other, text):
        barrier.wait()
        replies[text] = me.post(f"users/{other.user_id}/dm", {"message": text})

    threads = [threading.Thread(target=dm, args=(a, b, "from a")), threading.Thread(target=dm, args=(b, a, "from b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    room = server.app.repo.find_dm(a.user_id, b.user_id)
    assert room is not None
    texts = sorted(m["text"] for m in a.get(f"chat/{room.chat_id}/messages")["messages"])
    assert texts == ["from a", "from b"]
    for c in (a, b):
        pending = c.get("users/requests")
        assert pending["incoming"] == [] and pending["outgoing"] == []


def test_invite_is_settled_when_the_invitee_joins_by_themselves(server):
    a, b = server.user("inviter"), server.user("selfjoiner")
    gid = a.post("chats", {"name": "open door"})["chat"]["chat_id"]
    a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})
    b.post(f"chat/{gid}/join")
    assert b.get("users/requests")["incoming"] == []
    a.wait_event("users/request", match=lambda d: d["request"]["status"] == "accepted")


def test_non_members_cannot_tell_a_dm_from_a_missing_chat(make_server):
    server = make_server(dm_require_accept=True)       # the first DM is a request
    a, b, outsider = server.user("dm_a"), server.user("dm_b"), server.user("outsider")
    rid = a.post(f"users/{b.user_id}/dm", {"message": "hi"})["request"]["request_id"]
    dm_id = b.post(f"users/requests/{rid}/accept")["chat"]["chat_id"]
    for url, body in ((f"chat/{dm_id}/join", None), (f"chat/{dm_id}/leave", None),
                      (f"chat/{dm_id}/invite", {"user_id": str(outsider.user_id)})):
        real = outsider.request(POST, url, body)
        fake = outsider.request(POST, url.replace(dm_id, str(uuid.uuid4())), body)
        assert (real.status, real.data["error"]) == (fake.status, fake.data["error"]) == (253, "CHAT_NOT_FOUND")


def _arrival_order(client, wanted):
    with client._cond:
        return [e.path for e in client._events if e.path in wanted]


def test_new_dm_room_is_announced_before_its_first_message(make_server):
    server = make_server(dm_require_accept=True)       # the first DM is a request
    a, b = server.user("order_a"), server.user("order_b")
    rid = a.post(f"users/{b.user_id}/dm", {"message": "first"})["request"]["request_id"]
    a.drain_events()
    b.post(f"users/requests/{rid}/accept")
    wanted = ("chats/new", "chat/new_message")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and set(_arrival_order(a, wanted)) != set(wanted):
        time.sleep(0.02)
    order = _arrival_order(a, wanted)
    assert order.index("chats/new") < order.index("chat/new_message")


def test_pending_requests_are_capped(make_server):
    srv = make_server(max_pending_requests=2)
    host = srv.user("host")
    gid = host.post("chats", {"name": "capped invites"})["chat"]["chat_id"]
    guests = [srv.user(f"guest{i}") for i in range(3)]
    for g in guests[:2]:
        host.post(f"chat/{gid}/invite", {"user_id": str(g.user_id)})
    with pytest.raises(ApiFailure) as e:
        host.post(f"chat/{gid}/invite", {"user_id": str(guests[2].user_id)})
    assert err(e) == (252, "LIMIT_REACHED")


def test_log_lines_cannot_be_forged():
    assert "\n" not in loggable("chats\n2026-11-05 INFO socketcord.auth: 'admin' logged in")
    assert loggable("x" * 500).endswith("...") and len(loggable("x" * 500)) == 203


def test_non_finite_numbers_are_refused(server):
    c = server.user("floaty")
    c.send_raw(RequestCord(method=GET, url="chats", msg='{"time": 1e400}', user_id=c.user_id,
                           ses_token=c.token).pack())
    reply = c.wait_unmatched()
    assert (reply.status, reply.data["error"]) == (250, "INVALID_JSON")
