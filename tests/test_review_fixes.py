"""Regression tests for problems the final review of the new schema found."""
from __future__ import annotations

import uuid

from socketcord.protocol import GET, POST, RequestCord
from socketcord_client import ApiFailure  # noqa: F401  (imported for parity with the other test files)


def test_dm_requester_does_not_see_their_own_first_message_as_unread(make_server):
    server = make_server(dm_require_accept=True)       # the first DM is a request
    a, b = server.user("own_a"), server.user("own_b")
    rid = a.post(f"users/{b.user_id}/dm", {"message": "first, from a"})["request"]["request_id"]
    b.post(f"users/requests/{rid}/accept")
    page = a.get(f"chat/{rid}/messages")
    assert page["first_unread_id"] is None
    assert page["last_read_message_id"] == page["messages"][0]["id"]
    item = next(c for c in a.get("chats")["chats"] if c["chat_id"] == rid)
    assert item["unread"] == 0
    assert a.summary(rid, mode="unread")["message_count"] == 0       # nothing unread to summarize
    b_page = b.get(f"chat/{rid}/messages")                            # for b it is new
    assert b_page["first_unread_id"] == page["messages"][0]["id"]


def test_time_filters_at_the_edges_of_the_calendar_are_bad_requests(server):
    a = server.user("edges")
    gid = a.post("chats", {"name": "edges"})["chat"]["chat_id"]
    for t in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
              {"after": "0001-01-01T00:00:00+05:00"}, {"before": "9999-12-31T22:00:00-03:00"}):
        reply = a.request(GET, f"chat/{gid}/messages", {"time": t})
        assert reply.status in (200, 250), (t, reply.status, reply.data)   # never 254 (a crash)


def test_join_reply_info_matches_get_info(server):
    a, b, c = server.user("inf_a"), server.user("inf_b"), server.user("inf_c")
    gid = a.post("chats", {"name": "info"})["chat"]["chat_id"]
    a.post(f"chat/{gid}/invite", {"user_id": str(c.user_id)})
    joined = b.post(f"chat/{gid}/join")
    assert joined["info"]["invited_count"] == 1
    assert [i["user"]["username"] for i in joined["info"]["invited"]] == ["inf_c"]
    assert joined["info"]["invited"] == b.get(f"chat/{gid}/info")["chat"]["invited"]


def test_broken_packets_echo_the_method_name(server):
    c = server.user("echo")
    pkt = bytearray(RequestCord.from_dict(POST, "chats", {"name": "x"}, user_id=c.user_id,
                                          ses_token=c.token).pack())
    pkt[-1] ^= 0xFF                                                  # break the message checksum
    c.send_raw(bytes(pkt))
    reply = c.wait_unmatched()
    assert reply.status == 250 and reply.data["method"] == "POST"


def test_read_marker_always_lands_on_a_message_of_that_chat(server):
    a = server.user("marker")
    g1 = a.post("chats", {"name": "one"})["chat"]["chat_id"]
    g2 = a.post("chats", {"name": "two"})["chat"]["chat_id"]
    m1 = a.post(f"chat/{g1}/message", {"content": "in one"})["message"]["id"]
    other = a.post(f"chat/{g2}/message", {"content": "in two"})["message"]["id"]
    # An id from another chat, or a random v4 id, never becomes this chat's marker.
    assert a.post(f"chat/{g1}/read", {"message_id": other})["last_read_message_id"] == m1
    assert a.post(f"chat/{g1}/read", {"message_id": str(uuid.uuid4())})["last_read_message_id"] in (m1,)
    assert a.post(f"chat/{g2}/read", {"message_id": "00000000-0000-7000-8000-000000000000"}) \
        ["last_read_message_id"] == other                             # never moves back
