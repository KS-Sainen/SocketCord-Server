"""End-to-end: a real server, real TCP sockets, the reference client."""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid

import pytest

from socketcord import scram
from socketcord.protocol import GET, PATCH, POST, ReplyCord, RequestCord
from socketcord_client import ApiFailure


def err(exc_info) -> tuple:
    return exc_info.value.status, exc_info.value.data.get("error")


# ---------------------------------------------------------------------- auth

def test_register_login_info(server):
    c = server.client()
    done = c.register("alice", "correct horse")
    assert done["step"] == "done" and done["user"]["username"] == "alice"
    login = c.login("alice", "correct horse")
    assert login["path"] == "users/login" and uuid.UUID(login["session_token"])
    info = c.get("users/info")
    assert info["user"]["user_id"] == login["user_id"] and info["user"]["online"] is True


def test_password_never_crosses_the_socket(server):
    c = server.client()
    sent = bytearray()
    original = c.send_raw

    def spy(data: bytes) -> None:
        sent.extend(data)
        original(data)

    c.send_raw = spy
    c.register("carol", "TopSecret-รหัสผ่าน")
    c.login("carol", "TopSecret-รหัสผ่าน")
    assert "TopSecret".encode() not in sent and "รหัสผ่าน".encode() not in sent


def test_plaintext_password_is_refused(server):
    """What the current frontend main.py sends: refused with a clear error, nothing stored."""
    c = server.client()
    for url in ("users", "users/login"):
        reply = c.request(POST, url, {"username": "zed", "password": "hunter2"}, auth=False)
        assert (reply.status, reply.data["error"]) == (250, "PLAINTEXT_PASSWORD")
    assert server.app.repo.get_user_by_username("zed") is None


def test_unique_names_case_insensitive(server):  # R3
    server.client().register("Dave", "pw-1")
    with pytest.raises(ApiFailure) as e:
        server.client().register("dave", "pw-2")
    assert err(e) == (252, "USERNAME_TAKEN")


def test_bad_usernames(server):
    for bad in ("ab", "has space", "x" * 40, "semi;colon"):
        with pytest.raises(ApiFailure) as e:
            server.client().register(bad, "pw")
        assert err(e) == (250, "INVALID_USERNAME")


def test_wrong_password_and_unknown_user_look_the_same(server):
    server.client().register("erin", "right-pw")
    c = server.client()
    with pytest.raises(ApiFailure) as e1:
        c.login("erin", "wrong-pw")
    with pytest.raises(ApiFailure) as e2:
        c.login("nobody-here", "whatever")
    assert err(e1) == err(e2) == (251, "INVALID_CREDENTIALS")


def test_challenge_must_be_answered_on_same_connection(server):
    server.client().register("frank", "pw-frank")
    a, b = server.client(), server.client()
    flow = scram.LoginFlow("frank", "pw-frank", min_iterations=4096)
    ch = a.call(POST, "users/login", flow.start(), auth=False)
    reply = b.request(POST, "users/login", flow.respond(ch), auth=False)
    assert (reply.status, reply.data["error"]) == (251, "CHALLENGE_INVALID")


def test_challenge_is_single_use(server):
    server.client().register("gina", "pw-gina")
    c = server.client()
    flow = scram.LoginFlow("gina", "pw-gina", min_iterations=4096)
    ch = c.call(POST, "users/login", flow.start(), auth=False)
    answer = flow.respond(ch)
    assert c.request(POST, "users/login", answer, auth=False).ok
    replay = c.request(POST, "users/login", answer, auth=False)
    assert (replay.status, replay.data["error"]) == (251, "CHALLENGE_INVALID")


def test_login_throttle(server):
    server.client().register("hank", "pw-hank")
    c = server.client()
    for _ in range(server.config.login_max_failures):
        with pytest.raises(ApiFailure):
            c.login("hank", "nope")
    with pytest.raises(ApiFailure) as e:
        c.login("hank", "pw-hank")
    assert err(e) == (252, "TOO_MANY_ATTEMPTS") and e.value.data["retry_after"] > 0


def test_change_password_rotates_the_session_token(server):
    server.client().register("ivy", "old-pw")
    a, b = server.client(), server.client()
    a.login("ivy", "old-pw")
    b.user_id, b.token = a.user_id, a.token          # a second connection on the same session
    assert b.get("users/info")["user"]["username"] == "ivy"
    old_token = a.token
    done = a.change_password("old-pw", "new-pw")
    assert done["logged_out_connections"] == 1 and uuid.UUID(done["session_token"]) != old_token
    ev = b.wait_event("session_revoked")
    assert ev.data["reason"] == "password_changed"
    stale = b.request(GET, "users/info")
    assert (stale.status, stale.data["error"]) == (251, "INVALID_SESSION")
    assert a.token != old_token and a.get("users/info")["user"]["username"] == "ivy"   # the new token works
    with pytest.raises(ApiFailure):
        server.client().login("ivy", "old-pw")
    server.client().login("ivy", "new-pw")


def test_change_password_needs_current_password(server):
    c = server.user("jack", "real-pw")
    with pytest.raises(ApiFailure) as e:
        c.change_password("guess", "new")
    assert err(e) == (252, "INVALID_CREDENTIALS")      # 252, not 251: the session stays valid
    assert c.get("users/info")["user"]["username"] == "jack"


def test_logout(server):
    c = server.user("kate")
    observer = server.user("leo")
    c.post("users/logout")                 # keep the old token on purpose
    reply = c.request(GET, "users/info")
    assert (reply.status, reply.data["error"]) == (251, "INVALID_SESSION")   # revoked server-side
    observer.wait_event("presence", match=lambda d: d["user"]["username"] == "kate" and not d["online"])
    c.logout = None
    c.user_id = c.token = uuid.UUID(int=0)
    assert c.request(GET, "users/info").status == 253                        # no credentials at all


# ---------------------------------------------------------------------- one account per day

def test_registration_limit_by_device(make_server):
    srv = make_server(reg_limit_enabled=True, reg_limit_device_max=1, reg_limit_ip_max=0)
    srv.client().register("dev1", "pw", device_id="device-aaaaaaaa")
    with pytest.raises(ApiFailure) as e:
        srv.client().register("dev2", "pw", device_id="device-aaaaaaaa")
    assert err(e) == (252, "RATE_LIMITED") and e.value.data["retry_after"] > 23 * 3600
    srv.client().register("dev3", "pw", device_id="device-bbbbbbbb")   # another machine is fine


def test_registration_limit_by_ip(make_server):
    srv = make_server(reg_limit_enabled=True, reg_limit_device_max=0, reg_limit_ip_max=1)
    srv.client().register("ip1", "pw", device_id="device-aaaaaaaa")
    with pytest.raises(ApiFailure) as e:
        srv.client().register("ip2", "pw", device_id="device-bbbbbbbb")   # same 127.0.0.1
    assert err(e) == (252, "RATE_LIMITED")


def test_registration_limit_exempt_network(make_server):
    import ipaddress
    srv = make_server(reg_limit_enabled=True, reg_limit_device_max=1, reg_limit_ip_max=1,
                      reg_limit_exempt=[ipaddress.ip_network("127.0.0.0/8")])
    srv.client().register("ex1", "pw", device_id="device-11111111")
    srv.client().register("ex2", "pw", device_id="device-22222222")   # IP rule skipped for loopback


def test_registration_limit_is_atomic(make_server):
    srv = make_server(reg_limit_enabled=True, reg_limit_device_max=1, reg_limit_ip_max=0)
    results = []

    def attempt(i):
        try:
            srv.client().register(f"race{i}", "pw", device_id="device-same-machine")
            results.append("ok")
        except ApiFailure as exc:
            results.append(exc.data["error"])

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("RATE_LIMITED") == 5


# ---------------------------------------------------------------------- presence (R4)

def test_online_list_includes_self_and_updates(server):
    a = server.user("amy")
    users = a.get("users/online")["users"]
    assert [(u["username"], u["is_self"]) for u in users] == [("amy", True)]
    b = server.user("ben")
    ev = a.wait_event("presence", match=lambda d: d["user"]["username"] == "ben")
    assert ev.data["online"] is True
    assert {u["username"] for u in a.get("users/online")["users"]} == {"amy", "ben"}
    b.close()
    ev = a.wait_event("presence", match=lambda d: d["user"]["username"] == "ben" and not d["online"])
    assert [u["username"] for u in a.get("users/online")["users"]] == ["amy"]


def test_closed_connection_is_never_bound():
    """A worker finishing a request after the client hung up must not mark it online again."""
    from socketcord.presence import OnlineUser, PresenceHub
    hub = PresenceHub()
    hub.connection_opened(7)
    hub.connection_closed(7)
    who = OnlineUser(uuid.uuid4(), "ghost", "ghost")
    assert hub.bind(7, who, None) == "closed"
    assert not hub.is_online(who.user_id) and hub.online_users() == []


def test_request_then_hang_up_leaves_no_ghost(server):
    ghost = server.user("ghost")
    watcher = server.user("watcher")
    uid, token = ghost.user_id, ghost.token
    ghost.close()
    for _ in range(20):
        s = socket.create_connection(server.address)
        s.sendall(RequestCord.from_dict(GET, "chats", {}, user_id=uid, ses_token=token).pack())
        s.close()
    # A user who drops lingers briefly (no offline/online flapping), then is gone for good.
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and len(watcher.get("users/online")["users"]) > 1:
        time.sleep(0.2)
    assert [u["username"] for u in watcher.get("users/online")["users"]] == ["watcher"]


def test_reconnect_with_saved_session_is_online_again(server):
    a = server.user("cat")
    observer = server.user("obs")
    uid, token = a.user_id, a.token
    a.close()
    observer.wait_event("presence", match=lambda d: d["user"]["username"] == "cat" and not d["online"])
    again = server.client()
    again.user_id, again.token = uid, token
    again.get("chats")
    assert "cat" in {u["username"] for u in observer.get("users/online")["users"]}


# ---------------------------------------------------------------------- groups (R5, R8-R11)

def test_group_lifecycle(server):
    a, b, c = server.user("ann"), server.user("bob"), server.user("cid")
    created = a.post("chats", {"name": "Network Project", "description": "term project"})
    gid = created["chat"]["chat_id"]
    assert created["chat"]["role"] == "owner" and created["chat"]["is_dm"] is False   # the GET chats shape
    assert [m["username"] for m in created["group"]["members"]] == ["ann"]   # R8
    b.wait_event("group_created")

    groups = b.get("chats/groups")["groups"]                                  # R9
    g = next(x for x in groups if x["chat_id"] == gid)
    assert g["joined"] is False and [m["username"] for m in g["members"]] == ["ann"]

    reply = b.request(GET, f"chat/{gid}/messages")                            # R11: members only
    assert (reply.status, reply.data["error"]) == (252, "NOT_A_MEMBER")
    reply = b.request(POST, f"chat/{gid}/message", {"content": "let me in"})
    assert reply.status == 252

    joined = b.post(f"chat/{gid}/join")                                       # R10: joins by itself
    assert joined["chat"]["chat_id"] == gid and joined["chat"]["is_dm"] is False   # the GET chats shape
    assert {m["username"] for m in joined["info"]["members"]} == {"ann", "bob"}
    a.wait_event("message", match=lambda d: d["message"]["kind"] == "system")

    a.post(f"chat/{gid}/message", {"content": "hello group"})
    ev = b.wait_event("message", match=lambda d: d["message"]["kind"] == "text")   # R6: new incoming
    assert ev.data["message"]["content"] == "hello group" and ev.data["chat_id"] == gid
    with pytest.raises(TimeoutError):
        c.wait_event("message", timeout=0.3)                                  # cid is not a member

    history = b.get(f"chat/{gid}/messages")["messages"]
    assert [m["content"] for m in history if m["kind"] == "text"] == ["hello group"]

    assert b.post(f"chat/{gid}/leave")["chat_deleted"] is False
    assert a.post(f"chat/{gid}/leave")["chat_deleted"] is True                # last one out
    assert gid not in {x["chat_id"] for x in c.get("chats/groups")["groups"]}


def test_invite_needs_accept(server):  # R10
    a, b = server.user("amir"), server.user("bea")
    gid = a.post("chats", {"name": "Invite Only"})["chat"]["chat_id"]
    req = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]
    ev = b.wait_event("request")
    assert ev.data["request"]["request_id"] == req["request_id"]
    members = {m["username"] for m in a.get(f"chat/{gid}/info")["chat"]["members"]}
    assert members == {"amir"}                                                # not added yet
    incoming = b.get("users/requests")["incoming"]
    assert [r["kind"] for r in incoming] == ["invite"]
    b.post(f"users/requests/{req['request_id']}/accept")
    members = {m["username"] for m in a.get(f"chat/{gid}/info")["chat"]["members"]}
    assert members == {"amir", "bea"}
    a.wait_event("request", match=lambda d: d["request"]["status"] == "accepted")


def test_decline_invite(server):
    a, b = server.user("alma"), server.user("boris")
    gid = a.post("chats", {"name": "G"})["chat"]["chat_id"]
    rid = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]["request_id"]
    b.post(f"users/requests/{rid}/decline")
    again = b.request(POST, f"users/requests/{rid}/accept")
    assert (again.status, again.data["error"]) == (253, "REQUEST_NOT_FOUND")    # a declined invite is deleted
    assert {m["username"] for m in a.get(f"chat/{gid}/info")["chat"]["members"]} == {"alma"}


# ---------------------------------------------------------------------- DMs (R7)

def test_dm_request_accept_and_privacy(server):
    a, b, c = server.user("dana"), server.user("eli"), server.user("fay")
    pending = a.post(f"users/{b.user_id}/dm", {"message": "hi eli"})
    assert pending["status"] == "pending"
    ev = b.wait_event("request")
    rid = ev.data["request"]["request_id"]
    assert ev.data["request"]["message"] == "hi eli"
    accepted = b.post(f"users/requests/{rid}/accept")
    dm_id = accepted["chat"]["chat_id"]
    assert accepted["chat"]["kind"] == "dm" and accepted["chat"]["peer"]["username"] == "dana"
    a.wait_event("chat", match=lambda d: d["reason"] == "dm_opened")

    sent = a.post(f"users/{b.user_id}/dm", {"message": "second"})              # room exists now
    assert sent["status"] == "sent" and sent["chat_id"] == dm_id
    b.post(f"chat/{dm_id}/message", {"content": "reply from eli"})
    a.wait_event("message", match=lambda d: d["message"]["content"] == "reply from eli")

    texts = [m["content"] for m in a.get(f"chat/{dm_id}/messages")["messages"]]
    assert texts == ["hi eli", "second", "reply from eli"]
    for call in (c.request(GET, f"chat/{dm_id}/messages"), c.request(GET, f"chat/{dm_id}/info")):
        assert call.status == 253                                             # fay cannot see it
    with pytest.raises(TimeoutError):
        c.wait_event("message", timeout=0.3)


def test_dm_crossing_requests_open_the_room(server):
    a, b = server.user("gus"), server.user("hal")
    a.post(f"users/{b.user_id}/dm", {"message": "from gus"})
    crossed = b.post(f"users/{a.user_id}/dm", {"message": "from hal"})
    assert crossed["status"] == "sent"
    texts = [m["content"] for m in b.get(f"chat/{crossed['chat_id']}/messages")["messages"]]
    assert texts == ["from gus", "from hal"]


def test_dm_without_accept_when_configured(make_server):
    srv = make_server(dm_require_accept=False)
    a, b = srv.user("ira"), srv.user("jon")
    out = a.post(f"users/{b.user_id}/dm", {"message": "straight in"})
    assert out["status"] == "sent"
    ev = b.wait_event("message")
    assert ev.data["message"]["content"] == "straight in"


def test_dm_to_self_or_unknown(server):
    a = server.user("kim")
    assert a.request(POST, f"users/{a.user_id}/dm", {"message": "me"}).status == 250
    assert a.request(POST, f"users/{uuid.uuid4()}/dm", {"message": "?"}).status == 253


# ---------------------------------------------------------------------- chat list, unread, filters

def test_chat_list_unread_and_mark_read(server):
    a, b = server.user("lea"), server.user("max")
    gid = a.post("chats", {"name": "Unread"})["chat"]["chat_id"]
    b.post(f"chat/{gid}/join")
    for i in range(3):
        a.post(f"chat/{gid}/message", {"content": f"m{i}"})
    chat = next(x for x in b.get("chats")["chats"] if x["chat_id"] == gid)
    assert chat["unread"] == 3 and chat["last_message"]["content"] == "m2"
    b.post(f"chat/{gid}/read")
    chat = next(x for x in b.get("chats")["chats"] if x["chat_id"] == gid)
    assert chat["unread"] == 0
    a_view = next(x for x in a.get("chats")["chats"] if x["chat_id"] == gid)
    assert a_view["unread"] == 0          # posting marks everything up to your own message as read
    b.post(f"chat/{gid}/message", {"content": "from max"})
    a_view = next(x for x in a.get("chats")["chats"] if x["chat_id"] == gid)
    assert a_view["unread"] == 1


def test_message_filters(server):
    a = server.user("ned")
    gid = a.post("chats", {"name": "Filters"})["chat"]["chat_id"]
    ids = [a.post(f"chat/{gid}/message", {"content": t})["message"]["message_id"]
           for t in ("apple pie", "banana", "apple tart", "cherry")]
    page = a.get(f"chat/{gid}/messages", {"count": 2})
    assert [m["content"] for m in page["messages"]] == ["apple tart", "cherry"] and page["has_more"]
    older = a.get(f"chat/{gid}/messages", {"count": 2, "before_id": ids[2]})
    assert [m["content"] for m in older["messages"]] == ["apple pie", "banana"]
    newer = a.get(f"chat/{gid}/messages", {"after_id": ids[1]})
    assert [m["content"] for m in newer["messages"]] == ["apple tart", "cherry"]
    found = a.get(f"chat/{gid}/messages", {"content": "APPLE"})
    assert [m["content"] for m in found["messages"]] == ["apple pie", "apple tart"]
    future = a.get(f"chat/{gid}/messages", {"time": {"after": "2999-01-01T00:00:00Z"}})
    assert future["messages"] == []
    past = a.get(f"chat/{gid}/messages", {"time": "2000-01-01T00:00:00Z"})
    assert past["messages"] == []
    wild = a.get(f"chat/{gid}/messages", {"content": "%"})
    assert wild["messages"] == []                                             # LIKE wildcards are literal
    bad = a.request(GET, f"chat/{gid}/messages", {"count": 0})
    assert bad.status == 250


def test_profile_colours(server):
    a, b = server.user("ola"), server.user("pip")
    assert a.get("users/info")["user"]["text_color"] == "#171717"          # the frontend's defaults
    assert a.get("users/info")["user"]["text_bg_color"] == "#f2f2f2"
    a.patch("users/profile", {"text_color": "#FFFFFF", "text_bg_color": "#0088ff"})
    ev = b.wait_event("profile")
    assert (ev.data["user"]["text_color"], ev.data["user"]["text_bg_color"]) == ("#ffffff", "#0088ff")
    online = {u["username"]: u for u in b.get("users/online")["users"]}
    assert online["ola"]["text_color"] == "#ffffff"
    for body, field in [({"text_color": "red"}, "text_color"), ({"text_color": "#0088FF"}, "text_color"),
                        ({}, "text_color")]:
        reply = a.request(PATCH, "users/profile", body)
        assert reply.status == 250 and reply.data["field"] == field, body       # bad, same as background, missing


# ---------------------------------------------------------------------- protocol-level behaviour

def test_reply_context_path_and_rid(server):
    c = server.user("quinn")
    r = c.request(GET, "/chats/")
    assert r.data["path"] == "chats" and isinstance(r.data["rid"], int)


def test_error_statuses(server):
    c = server.user("ray")
    assert c.request(GET, "no/such/call").status == 253
    assert c.request(12345, "chats").status == 253                            # unknown method code
    assert c.request(POST, "users/info").status == 253                        # wrong method for URL
    assert c.request(GET, "chat/not-a-uuid/info").status == 253
    anon = server.client()
    assert anon.request(GET, "chats", auth=False).status == 253               # README: no creds -> Not Found
    anon.user_id, anon.token = c.user_id, uuid.uuid4()
    reply = anon.request(GET, "chats")
    assert (reply.status, reply.data["error"]) == (251, "INVALID_SESSION")   # wrong token
    bad = c.request(POST, "chats", {"name": ""})
    assert (bad.status, bad.data["error"]) == (250, "INVALID_FIELD")


def test_invalid_json_and_checksum(server):
    c = server.client()
    raw = RequestCord(method=GET, url="chats", msg="{not json").pack()
    c.send_raw(raw)
    assert c.wait_unmatched().status == 250
    corrupt = bytearray(RequestCord.from_dict(GET, "chats", {"rid": 7}).pack())
    corrupt[-2] ^= 0x01
    c.send_raw(bytes(corrupt))
    reply = c.wait_unmatched()
    assert (reply.status, reply.data["error"]) == (250, "MESSAGE_CHECKSUM_MISMATCH")
    assert c.request(GET, "chats", auth=False).status == 253                  # stream still in sync


def test_fragmented_and_coalesced_over_tcp(server):
    c = server.user("sam")
    one = RequestCord.from_dict(GET, "users/info", {"rid": "frag"}, user_id=c.user_id, ses_token=c.token).pack()
    for i in range(0, len(one), 3):                                           # 3 bytes per send()
        c.send_raw(one[i:i + 3])
        time.sleep(0.001)
    assert c.wait_reply("frag").ok
    many = b"".join(RequestCord.from_dict(GET, "chats", {"rid": f"c{i}"}, user_id=c.user_id,
                                          ses_token=c.token).pack() for i in range(5))
    c.send_raw(many)                                                          # 5 packets, one send()
    assert all(c.wait_reply(f"c{i}").ok for i in range(5))


def test_replies_keep_request_order(server):
    c = server.user("tia")
    order = []
    c.on_event = None
    rids = [f"o{i}" for i in range(10)]
    burst = b"".join(RequestCord.from_dict(GET, "users/info", {"rid": r}, user_id=c.user_id,
                                           ses_token=c.token).pack() for r in rids)
    c.send_raw(burst)
    for r in rids:
        c.wait_reply(r)
        order.append(r)
    assert order == rids


def test_server_survives_garbage_and_abrupt_disconnects(server):
    s = socket.create_connection(server.address)
    s.sendall(os.urandom(5000))
    s.close()
    s2 = socket.create_connection(server.address)
    s2.sendall(RequestCord(method=GET, url="chats").pack()[:20])              # half a header, then vanish
    s2.close()
    c = server.user("uma")
    assert c.get("users/info")["user"]["username"] == "uma"


def test_many_clients_at_once(server):
    owner = server.user("owner")
    gid = owner.post("chats", {"name": "Crowd"})["chat"]["chat_id"]
    clients = []
    errors = []

    def join(i):
        try:
            c = server.user(f"user{i:02d}")
            c.post(f"chat/{gid}/join")
            clients.append(c)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=join, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for c in clients:
        c.drain_events()
    owner.post(f"chat/{gid}/message", {"content": "broadcast"})
    for c in clients:
        c.wait_event("message", match=lambda d: d["message"]["content"] == "broadcast")
    assert owner.get("users/online")["count"] == 16
