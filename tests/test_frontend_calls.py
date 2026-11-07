"""The calls the GUI needs beyond the first API: renaming yourself, another user's
groups, the "Add people" search, answering an invite by its group, the summary by
POST, and DMs that open without approval by default."""
from __future__ import annotations

import uuid

import pytest

from socketcord import scram
from socketcord.config import Config
from socketcord.protocol import GET, PATCH, POST
from socketcord_client import ApiFailure


def err(reply) -> tuple:
    return reply.status, reply.data.get("error")


def offline_user(server, name: str):
    """An account that exists but has never connected (so it is not online)."""
    v, _ = scram.make_verifier(f"pw-{name}", iterations=4096)
    return server.app.repo.create_user(name, v.salt, v.iterations, v.stored_key, v.server_key, None, None)


def no_credentials(server):
    return server.client()          # User ID and Session Token all zeros


def wrong_token(server, like):
    c = server.client()
    c.user_id, c.token = like.user_id, uuid.uuid4()
    return c


# ---------------------------------------------------------------------- PATCH users/profile: username

def test_rename_is_pushed_and_shows_in_the_online_list(server):
    a, b = server.user("alice"), server.user("bob")
    b.drain_events()
    out = a.patch("users/profile", {"username": "Alicia"})
    assert out["user"]["username"] == "Alicia" and out["user"]["user_id"] == str(a.user_id)
    assert set(out["user"]) == {"user_id", "username", "text_color", "background", "online", "created_at"}
    ev = b.wait_event("users/profile_changed")
    assert ev.data["user"]["user_id"] == str(a.user_id) and ev.data["user"]["username"] == "Alicia"
    online = {u["user_id"]: u["username"] for u in b.get("users/online")["users"]}
    assert online[str(a.user_id)] == "Alicia" and "alice" not in online.values()
    assert a.get("users/info")["user"]["username"] == "Alicia"
    found = b.get("users/search", {"query": "alic"})["users"]
    assert [u["username"] for u in found] == ["Alicia"]


def test_rename_then_log_in_with_the_new_name_only(server):
    a = server.user("oldname", "pw-renamer")
    a.patch("users/profile", {"username": "NewName"})
    again = server.client()
    done = again.login("newname", "pw-renamer")          # any case, the same password
    assert done["username"] == "NewName" and done["user_id"] == str(a.user_id)
    with pytest.raises(ApiFailure) as e:
        server.client().login("oldname", "pw-renamer")
    assert (e.value.status, e.value.data["error"]) == (251, "INVALID_CREDENTIALS")
    server.client().register("oldname", "pw-someone-else")   # the old name is free again


def test_login_after_a_password_change(server):
    a = server.user("quick", "pw-quick")
    a.patch("users/password", {"new_password": "pw-quicker"})
    assert server.client().login("quick", "pw-quicker")["user_id"] == str(a.user_id)
    reply = server.client().request(POST, "users/login", {"username": "quick", "password": "pw-quick"}, auth=False)
    assert err(reply) == (251, "INVALID_CREDENTIALS")               # the old password is gone


def test_rename_to_your_own_name_in_another_case(server):
    a = server.user("carol")
    assert a.patch("users/profile", {"username": "CAROL"})["user"]["username"] == "CAROL"
    assert a.patch("users/profile", {"username": "CAROL"})["user"]["username"] == "CAROL"   # no change is fine
    assert server.app.repo.get_user_by_username("carol").username == "CAROL"


def test_rename_refuses_taken_and_bad_names(server):
    a, _ = server.user("dave"), server.user("Erin")
    for clash in ("Erin", "erin", "ERIN"):
        reply = a.request(PATCH, "users/profile", {"username": clash, "text_color": "#ff0000"})
        assert err(reply) == (252, "USERNAME_TAKEN") and reply.data["field"] == "username"
    for bad in ("ab", "has space", "x" * 33, "semi;colon", ""):
        reply = a.request(PATCH, "users/profile", {"username": bad})
        assert reply.status == 250 and reply.data["field"] == "username", bad
    assert err(a.request(PATCH, "users/profile", {"username": "ab"})) == (250, "INVALID_USERNAME")
    assert err(a.request(PATCH, "users/profile", {"username": 7})) == (250, "INVALID_FIELD")
    me = a.get("users/info")["user"]
    assert (me["username"], me["text_color"]) == ("dave", "#171717")          # nothing changed


def test_profile_takes_text_bg_color_as_another_name_for_background(server):
    a = server.user("painter")
    out = a.patch("users/profile", {"username": "Neo", "text_color": "#ffffff", "text_bg_color": "#aabbcc"})
    assert (out["user"]["username"], out["user"]["text_color"], out["user"]["background"]) == \
        ("Neo", "#FFFFFF", "#AABBCC")                                 # either case in, upper case out
    same = a.patch("users/profile", {"text_bg_color": "#112233", "background": "#112233"})
    assert same["user"]["background"] == "#112233".upper()
    assert "text_bg_color" not in same["user"]
    reply = a.request(PATCH, "users/profile", {"text_bg_color": "#112233", "background": "#445566"})
    assert err(reply) == (250, "INVALID_FIELD") and reply.data["field"] == "background"
    reply = a.request(PATCH, "users/profile", {"background": "#ffffff"})          # equals the text colour
    assert err(reply) == (250, "INVALID_FIELD") and reply.data["field"] == "background"
    reply = a.request(PATCH, "users/profile", {"background": "blue"})
    assert err(reply) == (250, "INVALID_FIELD") and reply.data["field"] == "background"


def test_profile_needs_a_field_and_credentials(server):
    a = server.user("fred")
    reply = a.request(PATCH, "users/profile", {})
    assert err(reply) == (250, "MISSING_FIELD")
    assert err(no_credentials(server).request(PATCH, "users/profile", {"username": "x_y_z"}, auth=False)) == \
        (253, "NOT_FOUND")
    assert err(wrong_token(server, a).request(PATCH, "users/profile", {"username": "x_y_z"})) == \
        (251, "INVALID_SESSION")


def test_renames_are_rate_limited(make_server):
    srv = make_server(profile_rate_per_minute=1)          # burst 5
    a = srv.user("hasty")
    for i in range(5):
        a.patch("users/profile", {"username": f"hasty{i}"})
    reply = a.request(PATCH, "users/profile", {"username": "hasty9"})
    assert err(reply) == (252, "RATE_LIMITED") and reply.data["retry_after"] >= 1


# ---------------------------------------------------------------------- GET users/{user_id}/groups

def test_user_groups_lists_their_groups_by_name(server):
    a, b, c = server.user("gina"), server.user("hank"), server.user("ivan")
    zeta = a.post("chats", {"name": "Zeta"})["chat"]["chat_id"]
    alpha = a.post("chats", {"name": "alpha", "description": "first"})["chat"]["chat_id"]
    b.post(f"chat/{zeta}/join")
    elsewhere = c.post("chats", {"name": "Elsewhere"})["chat"]["chat_id"]
    c.post(f"chat/{elsewhere}/invite", {"user_id": str(a.user_id)})          # pending: not one of a's groups

    seen_by_c = c.get(f"users/{a.user_id}/groups")
    assert seen_by_c["user_id"] == str(a.user_id) and seen_by_c["user"]["username"] == "gina"
    groups = seen_by_c["groups"]
    assert [(g["chat_id"], g["name"]) for g in groups] == [(alpha, "alpha"), (zeta, "Zeta")]
    assert seen_by_c["count"] == 2
    assert [(g["role"], g["member_count"], g["joined"]) for g in groups] == [("owner", 1, False), ("owner", 2, False)]
    assert groups[0]["description"] == "first" and groups[0]["kind"] == "group" and groups[0]["is_dm"] is False
    assert [g["joined"] for g in b.get(f"users/{a.user_id}/groups")["groups"]] == [False, True]
    assert [g["name"] for g in a.get(f"users/{b.user_id}/groups")["groups"]] == ["Zeta"]
    assert b.get(f"users/{b.user_id}/groups")["groups"][0]["role"] == "member"
    assert server.user("nobody_in").get(f"users/{c.user_id}/groups")["groups"][0]["name"] == "Elsewhere"


def test_user_groups_errors(server):
    a = server.user("jill")
    assert err(a.request(GET, f"users/{uuid.uuid4()}/groups")) == (253, "USER_NOT_FOUND")
    assert err(a.request(GET, "users/not-a-uuid/groups")) == (253, "NOT_FOUND")
    assert a.get(f"users/{offline_user(server, 'loner').user_id}/groups")["groups"] == []
    assert err(no_credentials(server).request(GET, f"users/{a.user_id}/groups", auth=False)) == (253, "NOT_FOUND")
    assert err(wrong_token(server, a).request(GET, f"users/{a.user_id}/groups")) == (251, "INVALID_SESSION")


# ---------------------------------------------------------------------- GET chat/{chat_id}/invitees

def test_invitees_are_online_people_and_dm_peers_not_yet_in_the_group(server):
    repo = server.app.repo
    me = server.user("kate")
    online_b = server.user("Bea")
    member = server.user("member1")
    invited = server.user("invited1")
    peer = offline_user(server, "peer_offline")
    offline_user(server, "stranger")                       # offline, no DM with kate
    pending = offline_user(server, "pending_dm")           # only a DM request, not an open DM
    gid = me.post("chats", {"name": "Party"})["chat"]["chat_id"]
    member.post(f"chat/{gid}/join")
    me.post(f"chat/{gid}/invite", {"user_id": str(invited.user_id)})
    repo.create_dm(peer.user_id, me.user_id)
    repo.create_request("dm", me.user_id, pending.user_id, message="hi")

    out = me.get(f"chat/{gid}/invitees")
    assert out["chat_id"] == gid and out["query"] == "" and out["count"] == 2
    assert out["users"] == [
        {"user_id": str(online_b.user_id), "username": "Bea", "text_color": "#171717",
         "background": "#F2F2F2", "online": True},
        {"user_id": str(peer.user_id), "username": "peer_offline", "text_color": "#171717",
         "background": "#F2F2F2", "online": False},
    ]
    assert [u["username"] for u in me.get(f"chat/{gid}/invitees", {"query": "OFF"})["users"]] == ["peer_offline"]
    assert [u["username"] for u in me.get(f"chat/{gid}/invitees", {"query": " b "})["users"]] == ["Bea"]
    assert me.get(f"chat/{gid}/invitees", {"query": "zzz"})["users"] == []
    assert me.get(f"chat/{gid}/invitees", {"query": "member"})["users"] == []     # members are left out
    assert me.get(f"chat/{gid}/invitees", {"query": "invited"})["users"] == []    # so are pending invitees
    # Any member may ask; the asker is left out of their own list.
    names = [u["username"] for u in member.get(f"chat/{gid}/invitees")["users"]]
    assert names == ["Bea"]                                 # kate is a member; peer is kate's DM, not member1's


def test_invitees_list_stops_at_fifty(server):
    repo = server.app.repo
    me = server.user("lisa")
    for i in range(55):
        repo.create_dm(me.user_id, offline_user(server, f"friend{i:02d}").user_id)
    gid = me.post("chats", {"name": "Big"})["chat"]["chat_id"]
    out = me.get(f"chat/{gid}/invitees")
    assert [u["username"] for u in out["users"]] == [f"friend{i:02d}" for i in range(50)] and out["count"] == 50
    assert len(me.get(f"chat/{gid}/invitees", {"query": "friend5"})["users"]) == 5


def test_invitees_errors(server):
    a, b, c = server.user("mona"), server.user("nina"), server.user("omar")
    gid = a.post("chats", {"name": "Closed"})["chat"]["chat_id"]
    assert err(b.request(GET, f"chat/{gid}/invitees")) == (252, "NOT_A_MEMBER")
    dm_id = a.post(f"users/{b.user_id}/dm", {"message": "hi"})["chat_id"]
    assert err(a.request(GET, f"chat/{dm_id}/invitees")) == (250, "NOT_A_GROUP")
    assert err(c.request(GET, f"chat/{dm_id}/invitees")) == (253, "CHAT_NOT_FOUND")   # not her DM
    assert err(a.request(GET, f"chat/{uuid.uuid4()}/invitees")) == (253, "CHAT_NOT_FOUND")
    reply = a.request(GET, f"chat/{gid}/invitees", {"query": "x" * 33})
    assert err(reply) == (250, "INVALID_FIELD") and reply.data["field"] == "query"
    assert err(no_credentials(server).request(GET, f"chat/{gid}/invitees", auth=False)) == (253, "NOT_FOUND")
    assert err(wrong_token(server, a).request(GET, f"chat/{gid}/invitees")) == (251, "INVALID_SESSION")


# ---------------------------------------------------------------------- POST chat/{chat_id}/invite/accept|reject

def test_accept_invite_by_group_id(server):
    a, b, watcher = server.user("pam"), server.user("quin"), server.user("rita")
    gid = a.post("chats", {"name": "Book Club"})["chat"]["chat_id"]
    rid = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]["request_id"]
    out = b.post(f"chat/{gid}/invite/accept")
    assert out["path"] == "chat/joined"
    assert out["chat"]["chat_id"] == gid and out["chat"]["role"] == "member" and out["chat"]["unread"] == 0
    assert out["chat"]["pending"] is False and out["chat"]["last_message"]["text"] == "quin joined the group"
    assert {m["username"] for m in out["info"]["members"]} == {"pam", "quin"}
    assert out["info"]["joined"] is True and out["info"]["invited"] == []
    assert out["info"] == b.get(f"chat/{gid}/info")["chat"]                  # the same object as join's
    assert (out["request"]["request_id"], out["request"]["status"]) == (rid, "accepted")
    ev = a.wait_event("users/request", match=lambda d: d["request"]["status"] == "accepted")
    assert ev.data["request"]["request_id"] == rid
    a.wait_event("chat/new_message", match=lambda d: d["message"]["text"] == "quin joined the group")
    watcher.wait_event("chat/members_changed", match=lambda d: d["chat_id"] == gid and d["change"] == "joined")
    assert b.get("users/requests")["incoming"] == []
    again = b.request(POST, f"chat/{gid}/invite/accept")
    assert err(again) == (253, "REQUEST_NOT_FOUND")                          # nothing left to accept
    assert err(b.request(POST, f"users/requests/{rid}/accept")) == (252, "REQUEST_CLOSED")


def test_reject_invite_by_group_id(server):
    a, b = server.user("sara"), server.user("tom")
    gid = a.post("chats", {"name": "Not For Me"})["chat"]["chat_id"]
    rid = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]["request_id"]
    out = b.post(f"chat/{gid}/invite/reject")
    assert (out["path"], out["chat_id"], out["status"]) == ("chat/declined", gid, "declined")
    assert (out["request"]["request_id"], out["request"]["status"]) == (rid, "declined")
    ev = a.wait_event("users/request", match=lambda d: d["request"]["status"] == "declined")
    assert ev.data["request"]["request_id"] == rid
    assert {m["username"] for m in a.get(f"chat/{gid}/info")["chat"]["members"]} == {"sara"}
    assert b.get("users/requests")["incoming"] == []
    assert err(b.request(POST, f"users/requests/{rid}/accept")) == (253, "REQUEST_NOT_FOUND")   # deleted
    assert err(b.request(POST, f"chat/{gid}/invite/reject")) == (253, "REQUEST_NOT_FOUND")
    a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})                # may be invited again


def test_answering_an_invite_that_is_not_there(server):
    a, b, c = server.user("uma"), server.user("vic"), server.user("walt")
    gid = a.post("chats", {"name": "Members Only"})["chat"]["chat_id"]
    for action in ("accept", "reject"):
        assert err(b.request(POST, f"chat/{gid}/invite/{action}")) == (253, "REQUEST_NOT_FOUND")   # never invited
        assert err(a.request(POST, f"chat/{gid}/invite/{action}")) == (253, "REQUEST_NOT_FOUND")   # a member
        assert err(b.request(POST, f"chat/{uuid.uuid4()}/invite/{action}")) == (253, "CHAT_NOT_FOUND")
        assert err(no_credentials(server).request(POST, f"chat/{gid}/invite/{action}", auth=False)) == \
            (253, "NOT_FOUND")
        assert err(wrong_token(server, b).request(POST, f"chat/{gid}/invite/{action}")) == (251, "INVALID_SESSION")
    dm_id = a.post(f"users/{b.user_id}/dm", {"message": "hello"})["chat_id"]
    assert err(b.request(POST, f"chat/{dm_id}/invite/accept")) == (253, "REQUEST_NOT_FOUND")    # her own DM
    assert err(c.request(POST, f"chat/{dm_id}/invite/accept")) == (253, "CHAT_NOT_FOUND")      # someone else's
    # An invite cancelled by the inviter is gone.
    rid = a.post(f"chat/{gid}/invite", {"user_id": str(c.user_id)})["request"]["request_id"]
    a.post(f"users/requests/{rid}/cancel")
    assert err(c.request(POST, f"chat/{gid}/invite/accept")) == (253, "REQUEST_NOT_FOUND")
    assert {m["username"] for m in a.get(f"chat/{gid}/info")["chat"]["members"]} == {"uma"}


def test_accepting_an_invite_counts_as_a_join_for_the_rate_limit(make_server):
    srv = make_server(membership_rate_per_minute=1)       # burst 10
    a, b = srv.user("limit_a"), srv.user("limit_b")
    gids = [a.post("chats", {"name": f"group {i}"})["chat"]["chat_id"] for i in range(11)]
    for gid in gids[:10]:
        b.post(f"chat/{gid}/join")
    a.post(f"chat/{gids[10]}/invite", {"user_id": str(b.user_id)})
    reply = b.request(POST, f"chat/{gids[10]}/invite/accept")
    assert err(reply) == (252, "RATE_LIMITED") and reply.data["retry_after"] >= 1
    assert [r["status"] for r in b.get("users/requests")["incoming"]] == ["pending"]   # nothing changed
    assert b.post(f"chat/{gids[10]}/invite/reject")["status"] == "declined"           # declining is not limited


# ---------------------------------------------------------------------- POST chat/{chat_id}/summary

def test_summary_by_post_is_the_same_call(server):
    a, b = server.user("xena"), server.user("yuri")
    gid = a.post("chats", {"name": "Plans"})["chat"]["chat_id"]
    for text in ("meet at nine", "bring the slides", "room 401"):
        a.post(f"chat/{gid}/message", {"content": text})
    by_get = a.get(f"chat/{gid}/summary", {"count": 10})
    by_post = a.post(f"chat/{gid}/summary", {"count": 10})
    assert by_post["method"] == "POST" and by_get["method"] == "GET"
    drop = ("method", "rid", "cached")
    assert {k: v for k, v in by_post.items() if k not in drop} == {k: v for k, v in by_get.items() if k not in drop}
    assert by_post["summary"] and by_post["message_count"] == 3
    assert err(b.request(POST, f"chat/{gid}/summary")) == (252, "NOT_A_MEMBER")
    assert err(a.request(POST, f"chat/{uuid.uuid4()}/summary")) == (253, "CHAT_NOT_FOUND")
    assert err(a.request(POST, f"chat/{gid}/summary", {"mode": "everything"})) == (250, "INVALID_FIELD")
    assert err(no_credentials(server).request(POST, f"chat/{gid}/summary", auth=False)) == (253, "NOT_FOUND")


# ---------------------------------------------------------------------- DMs open without approval by default

def test_dm_require_accept_defaults_to_false(monkeypatch):
    assert Config().dm_require_accept is False
    monkeypatch.delenv("DM_REQUIRE_ACCEPT", raising=False)
    assert Config.from_env().dm_require_accept is False
    monkeypatch.setenv("DM_REQUIRE_ACCEPT", "true")
    assert Config.from_env().dm_require_accept is True


def test_first_dm_opens_the_room_at_once_by_default(server):
    assert server.config.dm_require_accept is False
    a, b = server.user("zack"), server.user("ziva")
    out = a.post(f"users/{b.user_id}/dm", {"message": "hey ziva"})
    assert out["path"] == "chats/dm" and out["status"] == "sent" and out["request"] is None
    assert out["chat"]["peer"]["username"] == "ziva"
    dm_id = out["chat_id"]
    assert [m["text"] for m in out["messages"]] == ["hey ziva"]
    opened = b.wait_event("chats/new", match=lambda d: d["chat"]["chat_id"] == dm_id)
    assert opened.data["chat"]["name"] == "zack" and opened.data["chat"]["pending"] is False
    assert opened.data["chat"]["is_dm"] is True
    ev = b.wait_event("chat/new_message")
    assert (ev.data["chat_id"], ev.data["message"]["text"]) == (dm_id, "hey ziva")
    assert b.get("users/requests")["incoming"] == []
    again = a.post(f"users/{b.user_id}/dm", {"message": "still there?"})        # the same room is reused
    back = b.post(f"users/{a.user_id}/dm", {"message": "yes"})
    assert again["chat_id"] == back["chat_id"] == dm_id
    assert [m["text"] for m in a.get(f"chat/{dm_id}/messages")["messages"]] == ["hey ziva", "still there?", "yes"]


def test_dm_still_needs_accepting_when_configured(make_server):
    srv = make_server(dm_require_accept=True)
    a, b = srv.user("adam"), srv.user("bree")
    out = a.post(f"users/{b.user_id}/dm", {"message": "may I?"})
    assert (out["status"], out["chat_id"]) == ("pending", None)
    ev = b.wait_event("users/request")
    assert ev.data["request"]["message"] == "may I?"
    reply = a.request(GET, f"chat/{out['request']['request_id']}/messages")
    assert err(reply) == (253, "CHAT_NOT_FOUND")                           # no room until bree accepts
    accepted = b.post(f"users/requests/{out['request']['request_id']}/accept")
    assert accepted["chat"]["kind"] == "dm"


def test_a_request_left_over_from_accept_mode_opens_with_the_next_dm(server):
    a, b = server.user("cleo"), server.user("dina")
    old = server.app.repo.create_request("dm", a.user_id, b.user_id, message="sent while approval was on")
    b.drain_events()
    out = a.post(f"users/{b.user_id}/dm", {"message": "hello again"})
    assert out["status"] == "sent" and out["chat_id"] == str(old.chat_id)
    assert [m["text"] for m in out["messages"]] == ["sent while approval was on", "hello again"]
    assert out["request"]["status"] == "accepted"
    b.wait_event("chats/new", match=lambda d: d.get("reason") == "dm_opened")
    b.wait_event("users/request", match=lambda d: d["request"]["status"] == "accepted")
    texts = [m["text"] for m in b.get(f"chat/{old.chat_id}/messages")["messages"]]
    assert texts == ["sent while approval was on", "hello again"]
    assert b.get("users/requests")["incoming"] == [] and a.get("users/requests")["outgoing"] == []
