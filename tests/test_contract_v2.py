"""The wire contract v2 tables, checked against a running server.

Section 1 (one-step authentication), section 2 (the `path` of every reply),
section 3 (field names and formats) and section 4 (the pushes).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from socketcord.protocol import GET, PATCH, POST, PUSH_PATHS
from socketcord_client import ApiFailure


def err(reply) -> tuple:
    return reply.status, reply.data.get("error")


# ---------------------------------------------------------------------- 1. authentication

def test_register_then_login_with_the_right_password(server):
    c = server.client()
    created = c.register("newcomer", "correct horse battery staple")
    assert created["path"] == "login/created" and "session_token" not in created
    out = c.login("newcomer", "correct horse battery staple")
    assert out["path"] == "login/success"
    assert set(out) >= {"path", "method", "rid", "user_id", "session_token", "username",
                        "text_color", "background"}
    assert uuid.UUID(out["user_id"]) and uuid.UUID(out["session_token"])
    assert (out["username"], out["text_color"], out["background"]) == ("newcomer", "#171717", "#F2F2F2")
    assert c.get("users/info")["user"]["user_id"] == out["user_id"]


def test_login_with_the_wrong_password_or_an_unknown_name(server):
    server.client().register("gatekeeper", "let-me-in")
    for body in ({"username": "gatekeeper", "password": "let-me-out"},
                 {"username": "gatekeeper", "password": ""},
                 {"username": "nobody-at-all", "password": "let-me-in"}):
        reply = server.client().request(POST, "users/login", body, auth=False)
        assert reply.data["path"] == "users/login"       # an error echoes the URL, not login/success
        assert err(reply) in [(251, "INVALID_CREDENTIALS"), (250, "INVALID_FIELD")], body
    # A wrong name and a wrong password are the same answer, so nobody can probe for accounts.
    wrong_pw = server.client().request(POST, "users/login",
                                       {"username": "gatekeeper", "password": "nope"}, auth=False)
    unknown = server.client().request(POST, "users/login",
                                      {"username": "nobody-at-all", "password": "nope"}, auth=False)
    assert err(wrong_pw) == err(unknown) == (251, "INVALID_CREDENTIALS")
    assert wrong_pw.data["detail"] == unknown.data["detail"]


def test_register_and_login_field_errors(server):
    c = server.client()
    for body, expected in (
            ({"password": "pw"}, (250, "MISSING_FIELD")),
            ({"username": "fieldtest"}, (250, "MISSING_FIELD")),
            ({"username": "ab", "password": "pw"}, (250, "INVALID_USERNAME")),
            ({"username": "field test", "password": "pw"}, (250, "INVALID_USERNAME")),
            ({"username": "fieldtest", "password": 7}, (250, "INVALID_FIELD")),
            ({"username": "fieldtest", "password": ""}, (250, "INVALID_FIELD")),
            ({"username": "fieldtest", "password": "x" * 257}, (250, "INVALID_FIELD"))):
        assert err(c.request(POST, "users", body, auth=False)) == expected, body
        assert err(c.request(POST, "users/login", body, auth=False)) == expected, body
    c.register("fieldtest", "pw-fieldtest")
    assert err(c.request(POST, "users", {"username": "FIELDTEST", "password": "x"}, auth=False)) == \
        (252, "USERNAME_TAKEN")


def test_login_after_a_password_change(server):
    a = server.user("switcher", "first-pw")
    assert a.change_password("second-pw")["path"] == "profile/password"
    assert a.get("users/info")["user"]["username"] == "switcher"     # the session still works
    assert server.client().login("switcher", "second-pw")["username"] == "switcher"
    with pytest.raises(ApiFailure) as e:
        server.client().login("switcher", "first-pw")
    assert (e.value.status, e.value.data["error"]) == (251, "INVALID_CREDENTIALS")


def test_an_account_stored_by_the_old_flow_still_logs_in(server):
    """The challenge-response flow stored (salt, iterations, StoredKey, ServerKey)
    exactly like this, and schema.sql did not change, so accounts that are already
    in the database keep working: nobody has to wipe their volume."""
    from socketcord import scram
    v, _ = scram.make_verifier("legacy-password", iterations=4096)
    user = server.app.repo.create_user("legacy", v.salt, v.iterations, v.stored_key, v.server_key, None, None)
    assert user.password_hash == scram.encode_verifier(v.salt, v.iterations, v.stored_key, v.server_key)
    out = server.client().login("legacy", "legacy-password")
    assert out["user_id"] == str(user.user_id)
    with pytest.raises(ApiFailure):
        server.client().login("legacy", "legacy-passwort")


def test_a_password_change_needs_no_current_password(server):
    a = server.user("forgetful", "who-remembers")
    assert a.patch("users/password", {"new_password": "brand-new"})["changed"] is True
    # Sending the old password too is simply ignored, not an error.
    assert a.patch("users/password", {"new_password": "newer", "password": "brand-new"})["changed"] is True
    assert server.client().login("forgetful", "newer")["username"] == "forgetful"


def test_no_reply_contains_a_password_field(server):
    a = server.user("private", "my-secret-pw")
    b = server.user("watcher2")
    a.change_password("my-other-pw")
    replies = [a.get("users/info"), a.get("chats"), a.get("users/online"),
               a.patch("users/profile", {"text_color": "#123456"}),
               b.get(f"users/{a.user_id}/groups"), b.get("users/search", {"query": "pri"})]
    for data in replies + [e.data for e in b.drain_events()]:
        assert not {"password", "new_password", "old_password"} & set(data), data
        assert "my-secret-pw" not in str(data) and "my-other-pw" not in str(data)
    # Nor does the stored row hold the password.
    assert "my-other-pw" not in server.app.repo.get_user(a.user_id).password_hash


# ---------------------------------------------------------------------- 2. the path of every reply

def test_every_reply_path_in_the_contract_table(server):
    a, b = server.user("pather_a"), server.user("pather_b")
    gid = a.post("chats", {"name": "Paths"})["chat"]["chat_id"]
    b.post(f"chat/{gid}/join")
    mid = a.post(f"chat/{gid}/message", {"content": "hi"})["message"]["id"]
    other = server.user("pather_c")
    invited_gid = a.post("chats", {"name": "Invites"})["chat"]["chat_id"]
    a.post(f"chat/{invited_gid}/invite", {"user_id": str(other.user_id)})
    second = a.post("chats", {"name": "Rejects"})["chat"]["chat_id"]
    a.post(f"chat/{second}/invite", {"user_id": str(other.user_id)})

    table = [
        (GET, "users/info", None, "profile/info"),
        (PATCH, "users/profile", {"text_color": "#abcdef"}, "profile/updated"),
        (PATCH, "users/password", {"new_password": "pather-pw-2"}, "profile/password"),
        (PATCH, "users/profile/password", {"new_password": "pather-pw-3"}, "profile/password"),
        (GET, "chats", None, "chats/list"),
        (POST, "chats", {"name": "Another"}, "chats/created"),
        (GET, f"chat/{gid}/info", None, "chat/info"),
        (GET, f"chat/{gid}/messages", None, "chat/messages"),
        (POST, f"chat/{gid}/message", {"content": "again"}, "chat/sent"),
        (POST, f"chat/{gid}/read", {"message_id": mid}, "chat/read"),
        (GET, f"chat/{gid}/summary", {"count": 5}, "chat/summary"),
        (POST, f"chat/{gid}/summary", {"count": 5}, "chat/summary"),
        (POST, f"chat/{invited_gid}/invite", {"user_id": str(b.user_id)}, "chat/invited"),
        (POST, f"users/{b.user_id}/dm", {"message": "hello"}, "chats/dm"),
        (GET, "users/online", None, "users/online"),
        (GET, f"users/{b.user_id}/groups", None, "users/groups"),
    ]
    for method, url, body, expected in table:
        reply = a.request(method, url, body)
        assert (reply.status, reply.data["path"]) == (200, expected), (url, reply.data)
    assert other.post(f"chat/{invited_gid}/invite/accept")["path"] == "chat/joined"
    assert other.post(f"chat/{second}/invite/reject")["path"] == "chat/declined"
    assert a.post("users/logout")["path"] == "login/logout"


def test_paths_the_table_does_not_name_keep_their_url(server):
    a, b = server.user("url_a"), server.user("url_b")
    gid = a.post("chats", {"name": "Urls"})["chat"]["chat_id"]
    rid = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]["request_id"]
    for method, url, body in ((GET, "users/search", {"query": "url"}),
                              (GET, "chats/groups", None),
                              (GET, f"chat/{gid}/invitees", None),
                              (GET, "users/requests", None)):
        reply = a.request(method, url, body)
        assert (reply.status, reply.data["path"]) == (200, url), reply.data
    assert b.post(f"users/requests/{rid}/decline")["path"] == f"users/requests/{rid}/decline"
    assert b.post(f"chat/{gid}/join")["path"] == f"chat/{gid}/join"
    assert b.post(f"chat/{gid}/leave")["path"] == f"chat/{gid}/leave"
    # An unknown call still echoes what was asked for.
    assert a.request(GET, "no/such/call").data["path"] == "no/such/call"


def test_an_error_reply_echoes_the_url_not_the_success_path(server):
    """A failure must not claim the name of the call that succeeds: a wrong
    password answered with "login/success" would tell the client it had logged in."""
    server.client().register("errpath", "right-pw")
    a = server.user("errpath_a")
    gid = a.post("chats", {"name": "Errors"})["chat"]["chat_id"]
    cases = [
        (POST, "users/login", {"username": "errpath", "password": "wrong-pw"}, False, 251),
        (POST, "users", {"username": "errpath", "password": "right-pw"}, False, 252),
        (GET, "chats", {"count": 0}, True, 250),
        (POST, "chats", {}, True, 250),
        (PATCH, "users/profile", {"text_color": "not-a-colour"}, True, 250),
        (PATCH, "users/password", {}, True, 250),
        (POST, f"chat/{gid}/message", {}, True, 250),
        (POST, f"chat/{gid}/read", {"message_id": "not-a-uuid"}, True, 250),
        (GET, f"chat/{uuid.uuid4()}/info", None, True, 253),
        (POST, f"chat/{gid}/invite/accept", None, True, 253),
    ]
    for method, url, body, auth, status in cases:
        client = a if auth else server.client()
        reply = client.request(method, url, body, auth=auth)
        assert reply.status == status, (url, reply.data)
        assert reply.data["path"] == url, reply.data           # the URL, not the route's name
        assert reply.data["error"] and reply.data["detail"]
        assert reply.data["method"] == {GET: "GET", POST: "POST", PATCH: "PATCH"}[method]
    # The same calls still carry their semantic name when they succeed.
    assert server.client().login("errpath", "right-pw")["path"] == "login/success"
    assert a.get("chats")["path"] == "chats/list"


# ---------------------------------------------------------------------- 3. field names and formats

def _is_utc_iso(text: str) -> bool:
    return (isinstance(text, str) and text.endswith("+00:00") and "Z" not in text
            and datetime.fromisoformat(text).tzinfo == timezone.utc)


def test_message_chat_member_and_page_shapes(server):
    a, b = server.user("shape_a"), server.user("shape_b")
    gid = a.post("chats", {"name": "Shapes"})["chat"]["chat_id"]
    b.post(f"chat/{gid}/join")                      # writes a system line
    sent = a.post(f"chat/{gid}/message", {"content": "hello shapes"})["message"]
    assert set(sent) >= {"id", "sender_id", "sender", "text", "time"}
    assert (sent["text"], sent["sender"], sent["sender_id"]) == ("hello shapes", "shape_a", str(a.user_id))
    assert not {"message_id", "content", "created_at"} & set(sent)

    page = b.get(f"chat/{gid}/messages")
    assert set(page) >= {"chat_id", "first_unread_id", "messages"}
    assert "first_unread_message_id" not in page
    assert page["first_unread_id"] == sent["id"]                   # b has not read it yet
    assert [m["text"] for m in page["messages"]] == ["shape_b joined the group", "hello shapes"]
    system = page["messages"][0]
    assert system["sender_id"] is None and system["sender"] is None and system["kind"] == "system"
    assert page["messages"] == sorted(page["messages"], key=lambda m: m["id"])   # oldest first
    assert b.post(f"chat/{gid}/read", {"message_id": sent["id"]})
    assert b.get(f"chat/{gid}/messages")["first_unread_id"] is None

    row = next(c for c in b.get("chats")["chats"] if c["chat_id"] == gid)
    assert set(row) >= {"chat_id", "name", "is_dm", "pending", "unread", "last_activity"}
    assert (row["name"], row["is_dm"], row["pending"], row["unread"]) == ("Shapes", False, False, 0)
    assert _is_utc_iso(row["last_activity"]) and _is_utc_iso(sent["time"])

    member = next(m for m in a.get(f"chat/{gid}/info")["chat"]["members"] if m["username"] == "shape_b")
    assert set(member) >= {"user_id", "username", "online", "text_color", "background"}
    assert "text_bg_color" not in member and member["online"] is True

    dm = a.post(f"users/{b.user_id}/dm", {"message": "a dm"})
    dm_row = dm["chat"]
    assert dm_row["is_dm"] is True and dm_row["name"] == "shape_b"   # a DM is named after the other person
    assert dm_row["pending"] is False


def test_colours_are_upper_case_in_replies_and_either_case_in_requests(server):
    a, b = server.user("colour_a"), server.user("colour_b")
    for sent in ("#aabbcc", "#AABBCC", "#aAbBcC"):
        out = a.patch("users/profile", {"text_color": sent, "background": "#123456"})
        assert (out["user"]["text_color"], out["user"]["background"]) == ("#AABBCC", "#123456")
    group = a.post("chats", {"name": "Colours"})["chat"]["chat_id"]
    me = next(m for m in a.get(f"chat/{group}/info")["chat"]["members"] if m["username"] == "colour_a")
    assert (me["text_color"], me["background"]) == ("#AABBCC", "#123456")
    online = {u["username"]: u for u in b.get("users/online")["users"]}
    assert (online["colour_a"]["text_color"], online["colour_a"]["background"]) == ("#AABBCC", "#123456")
    found = b.get("users/search", {"query": "colour_a"})["users"][0]
    assert (found["text_color"], found["background"]) == ("#AABBCC", "#123456")


def test_every_timestamp_is_utc_with_a_plus_offset(server):
    a, b = server.user("time_a"), server.user("time_b")
    gid = a.post("chats", {"name": "Times"})["chat"]["chat_id"]
    a.post(f"chat/{gid}/message", {"content": "tick"})
    b.post(f"chat/{gid}/join")
    stamps = []

    def collect(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("time", "created_at", "last_activity", "joined_at", "expires_at",
                           "invited_at", "responded_at", "from_time", "to_time") and value is not None:
                    stamps.append(value)
                collect(value)
        elif isinstance(obj, list):
            for value in obj:
                collect(value)

    for data in (a.get("users/info"), a.get("chats"), a.get(f"chat/{gid}/info"),
                 a.get(f"chat/{gid}/messages"), a.get("chats/groups"), a.get(f"chat/{gid}/summary"),
                 a.get(f"users/{b.user_id}/groups"), server.client().login("time_a", "pw-time_a-secret")):
        collect(data)
    assert len(stamps) > 10
    for stamp in stamps:
        assert _is_utc_iso(stamp), stamp


# ---------------------------------------------------------------------- 4. the pushes

def test_the_four_pushes_of_the_table(server):
    a, b = server.user("push_a"), server.user("push_b")
    gid = a.post("chats", {"name": "Pushes"})["chat"]["chat_id"]
    b.post(f"chat/{gid}/join")
    a.drain_events(), b.drain_events()

    # chat/new_message
    a.post(f"chat/{gid}/message", {"content": "for you"})
    ev = b.wait_event("chat/new_message")
    assert set(ev.data) >= {"path", "chat_id", "message"} and ev.data["chat_id"] == gid
    assert ev.data["message"]["text"] == "for you" and "method" not in ev.data

    # chat/members_changed
    c = server.user("push_c")
    c.post(f"chat/{gid}/join")
    ev = b.wait_event("chat/members_changed", match=lambda d: d["chat_id"] == gid)
    assert ev.data["chat_id"] == gid
    c.patch("users/profile", {"username": "push_c2"})      # renamed, so the member lists changed
    b.wait_event("chat/members_changed", match=lambda d: d["chat_id"] == gid and d["change"] == "profile")

    # chats/new, for an invitation that is not accepted yet: a chat row with pending = true
    other = a.post("chats", {"name": "Invited Group"})["chat"]["chat_id"]
    a.post(f"chat/{other}/invite", {"user_id": str(b.user_id)})
    ev = b.wait_event("chats/new", match=lambda d: d["chat"]["chat_id"] == other)
    row = ev.data["chat"]
    assert set(row) >= {"chat_id", "name", "is_dm", "pending", "unread", "last_activity"}
    assert (row["name"], row["is_dm"], row["pending"], row["unread"]) == ("Invited Group", False, True, 0)
    b.post(f"chat/{other}/invite/accept")

    # chats/new, for a DM somebody opened with you
    dm = a.post(f"users/{b.user_id}/dm", {"message": "hello"})["chat_id"]
    ev = b.wait_event("chats/new", match=lambda d: d["chat"]["chat_id"] == dm)
    assert ev.data["chat"]["is_dm"] is True and ev.data["chat"]["pending"] is False

    # users/status
    newcomer = server.user("push_d")
    ev = b.wait_event("users/status", match=lambda d: d["user_id"] == str(newcomer.user_id))
    assert set(ev.data) >= {"path", "user_id", "online"} and ev.data["online"] is True
    newcomer.post("users/logout")
    ev = b.wait_event("users/status", match=lambda d: d["user_id"] == str(newcomer.user_id)
                      and d["online"] is False)
    assert ev.data["online"] is False


def test_the_other_pushes_keep_working(server):
    a, b = server.user("more_a"), server.user("more_b")
    b.drain_events()

    gid = a.post("chats", {"name": "More"})["chat"]["chat_id"]
    assert b.wait_event("chats/group_created").data["group"]["name"] == "More"

    rid = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]["request_id"]
    assert b.wait_event("users/request").data["request"]["request_id"] == rid
    b.post(f"users/requests/{rid}/decline")
    a.wait_event("users/request", match=lambda d: d["request"]["status"] == "declined")

    a.patch("users/profile", {"username": "more_a2"})
    assert b.wait_event("users/profile_changed").data["user"]["username"] == "more_a2"

    a.post(f"chat/{gid}/leave")                            # the last member leaves
    assert b.wait_event("chats/group_deleted").data["chat_id"] == gid

    # "you were logged out because this account signed in somewhere else"
    again = server.client()
    again.login("more_b", "pw-more_b-secret")
    assert b.wait_event("login/revoked").data["reason"] == "logged_in_elsewhere"


def test_an_invitation_is_a_pending_chat_row_in_get_chats(server):
    a, b = server.user("pend_a"), server.user("pend_b")
    gid = a.post("chats", {"name": "Pending Group"})["chat"]["chat_id"]
    a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})
    row = next(c for c in b.get("chats")["chats"] if c["chat_id"] == gid)
    assert (row["pending"], row["name"], row["is_dm"], row["unread"]) == (True, "Pending Group", False, 0)
    assert row["member_count"] == 1 and row["last_message"] is None
    # It is not a membership: reading the group is still refused until it is accepted.
    assert err(b.request(GET, f"chat/{gid}/messages")) == (252, "NOT_A_MEMBER")
    # A reconnecting client sees it too, even though the push was sent while it was away.
    again = server.client()
    again.login("pend_b", "pw-pend_b-secret")
    assert [c["pending"] for c in again.get("chats")["chats"] if c["chat_id"] == gid] == [True]
    again.post(f"chat/{gid}/invite/accept")
    row = next(c for c in again.get("chats")["chats"] if c["chat_id"] == gid)
    assert row["pending"] is False and row["member_count"] == 2

    # A declined invitation leaves no row at all.
    other = a.post("chats", {"name": "Declined Group"})["chat"]["chat_id"]
    a.post(f"chat/{other}/invite", {"user_id": str(again.user_id)})
    assert [c["chat_id"] for c in again.get("chats")["chats"] if c["chat_id"] == other] == [other]
    again.post(f"chat/{other}/invite/reject")
    assert [c["chat_id"] for c in again.get("chats")["chats"] if c["chat_id"] == other] == []


def test_a_cancelled_invitation_is_pushed_to_the_invitee(server):
    a, b = server.user("cancel_a"), server.user("cancel_b")
    gid = a.post("chats", {"name": "Cancelled"})["chat"]["chat_id"]
    rid = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]["request_id"]
    b.wait_event("chats/new", match=lambda d: d["chat"]["chat_id"] == gid)
    a.post(f"users/requests/{rid}/cancel")
    ev = b.wait_event("chat/members_changed", match=lambda d: d["chat_id"] == gid)
    assert ev.data["change"] == "invite_cancelled"
    assert [c["chat_id"] for c in b.get("chats")["chats"] if c["chat_id"] == gid] == []
    # Declining on one device tells the account's other connections the same way.
    rid = a.post(f"chat/{gid}/invite", {"user_id": str(b.user_id)})["request"]["request_id"]
    twin = server.client()
    twin.user_id, twin.token = b.user_id, b.token
    twin.get("chats")
    b.drain_events()
    twin.post(f"chat/{gid}/invite/reject")
    ev = b.wait_event("chat/members_changed", match=lambda d: d["chat_id"] == gid)
    assert ev.data["change"] == "invite_cancelled" and rid


def test_requests_accept_the_name_the_reply_uses(server):
    """The reply spells a message "text", so the request takes "text" as well as the
    older "content"; the same for the DM body and the read marker."""
    a, b = server.user("alias_a"), server.user("alias_b")
    gid = a.post("chats", {"name": "Aliases"})["chat"]["chat_id"]
    by_text = a.post(f"chat/{gid}/message", {"text": "sent as text"})["message"]
    assert by_text["text"] == "sent as text"
    by_content = a.post(f"chat/{gid}/message", {"content": "sent as content"})["message"]
    assert by_content["text"] == "sent as content"
    assert a.post(f"chat/{gid}/message", {"content": "both", "text": "ignored"})["message"]["text"] == "both"
    assert err(a.request(POST, f"chat/{gid}/message", {})) == (250, "MISSING_FIELD")
    assert err(a.request(POST, f"chat/{gid}/message", {"text": "  "})) == (250, "INVALID_FIELD")
    # The search filter and the read marker too.
    found = a.get(f"chat/{gid}/messages", {"text": "as text"})["messages"]
    assert [m["text"] for m in found] == ["sent as text"]
    b.post(f"chat/{gid}/join")
    newest = b.post(f"chat/{gid}/message", {"text": "from b"})["message"]
    assert a.post(f"chat/{gid}/read", {"id": newest["id"]})["last_read_message_id"] == newest["id"]
    reply = a.request(POST, f"chat/{gid}/read", {"id": "not-a-uuid"})
    assert err(reply) == (250, "INVALID_FIELD") and reply.data["field"] == "id"
    dm = a.post(f"users/{b.user_id}/dm", {"text": "a dm by text"})
    assert [m["text"] for m in dm["messages"]] == ["a dm by text"]


def test_pushes_only_use_documented_paths(server):
    a, b = server.user("doc_a"), server.user("doc_b")
    gid = a.post("chats", {"name": "Documented"})["chat"]["chat_id"]
    b.post(f"chat/{gid}/join")
    a.post(f"chat/{gid}/message", {"content": "one"})
    a.post(f"users/{b.user_id}/dm", {"message": "two"})
    a.patch("users/profile", {"text_color": "#0f0f0f"})
    a.post(f"chat/{gid}/leave")
    seen = {e.path for e in b.drain_events()} | {e.path for e in a.drain_events()}
    assert seen and seen <= PUSH_PATHS, seen - PUSH_PATHS
