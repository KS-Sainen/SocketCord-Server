"""Wire format and stream framing (no network)."""
import json
import uuid
import zlib

import pytest

from socketcord.protocol import (GET, POST, REQUEST_HEADER_SIZE, ReplyCord, ReplyParser, RequestCord,
                                 RequestParser, Status)


def _packet(url="users/info", data=None, method=GET):
    return RequestCord.from_dict(method, url, data, user_id=uuid.uuid4(), ses_token=uuid.uuid4())


def test_header_layout_matches_readme():
    req = _packet("chat/x/messages", {"count": 5})
    raw = req.pack()
    assert REQUEST_HEADER_SIZE == 4 + 16 + 16 + 2 + 2 + 4 + 4 == 48
    assert int.from_bytes(raw[0:4], "big") == GET
    assert raw[4:20] == req.user_id.bytes and raw[20:36] == req.ses_token.bytes
    url_sz, msg_sz = int.from_bytes(raw[36:38], "big"), int.from_bytes(raw[38:40], "big")
    assert url_sz == len(b"chat/x/messages") and msg_sz == len(req.msg.encode())
    assert int.from_bytes(raw[40:44], "big") == zlib.crc32(b"chat/x/messages")
    assert int.from_bytes(raw[44:48], "big") == zlib.crc32(req.msg.encode())
    assert len(raw) == 48 + url_sz + msg_sz


def test_round_trip():
    req = _packet("users/login", {"username": "ก้อง", "n": 1}, POST)
    (frame,) = RequestParser().feed(req.pack())
    assert frame.error is None
    got = frame.request
    assert (got.method, got.user_id, got.ses_token, got.url) == (POST, req.user_id, req.ses_token, "users/login")
    assert got.as_dict() == {"username": "ก้อง", "n": 1}


def test_fragmented_one_byte_at_a_time():
    raw = _packet("chats").pack()
    parser = RequestParser()
    frames = []
    for i in range(len(raw)):
        frames += parser.feed(raw[i:i + 1])
        if i < len(raw) - 1:
            assert frames == []
    assert len(frames) == 1 and frames[0].request.url == "chats"
    assert parser.buffered == 0


def test_coalesced_and_split_across_reads():
    a, b, c = (_packet(u).pack() for u in ("one", "two", "three"))
    stream = a + b + c
    cut = len(a) + len(b) // 2           # read 1 = a + half of b, read 2 = rest of b + c
    parser = RequestParser()
    first = parser.feed(stream[:cut])
    second = parser.feed(stream[cut:])
    assert [f.request.url for f in first] == ["one"]
    assert [f.request.url for f in second] == ["two", "three"]


def test_checksum_mismatch_keeps_stream_in_sync():
    bad = bytearray(_packet("users/info").pack())
    bad[REQUEST_HEADER_SIZE] ^= 0x01     # flip a bit in the URL
    good = _packet("chats").pack()
    frames = RequestParser().feed(bytes(bad) + good)
    assert frames[0].error == "URL_CHECKSUM_MISMATCH"
    assert frames[1].error is None and frames[1].request.url == "chats"


def test_message_checksum_mismatch():
    raw = bytearray(_packet("chats", {"a": 1}).pack())
    raw[-1] ^= 0x20
    (frame,) = RequestParser().feed(bytes(raw))
    assert frame.error == "MESSAGE_CHECKSUM_MISMATCH"


def test_empty_message_is_empty_object():
    (frame,) = RequestParser().feed(RequestCord(method=GET, url="chats").pack())
    assert frame.request.as_dict() == {}


def test_oversized_fields_rejected():
    with pytest.raises(ValueError):
        RequestCord(method=GET, url="x" * 70000).pack()
    with pytest.raises(ValueError):
        ReplyCord.from_dict(200, {"blob": "x" * (1 << 24)}).pack()


def frontend_proc_buffer(buf: bytearray):
    """The parsing logic of SocketCordNetwork.proc_buffer in the frontend's network.py."""
    out = []
    HEADER_SZ = 8
    while len(buf) >= HEADER_SZ:
        status_code = buf[0]
        reply_sz = int.from_bytes(buf[1:4], byteorder="big")
        checksum = int.from_bytes(buf[4:8], "big")
        packet_sz = HEADER_SZ + reply_sz
        if len(buf) < packet_sz:
            break
        raw_msg = bytes(buf[HEADER_SZ:packet_sz])
        del buf[:packet_sz]
        assert zlib.crc32(raw_msg) == checksum, "Reply Checksum Mismatch"
        out.append((status_code, json.loads(raw_msg.decode("utf-8"))))
    return out


def test_reply_is_readable_by_frontend_parser():
    r1 = ReplyCord.from_dict(Status.OK, {"path": "users/login", "msg": "สวัสดี"})
    r2 = ReplyCord.from_dict(Status.NOT_FOUND, {"path": "nope", "error": "NOT_FOUND"})
    buf = bytearray(r1.pack() + r2.pack()[:5])
    first = frontend_proc_buffer(buf)
    buf += r2.pack()[5:]
    second = frontend_proc_buffer(buf)
    assert first == [(200, {"path": "users/login", "msg": "สวัสดี"})]
    assert second == [(253, {"path": "nope", "error": "NOT_FOUND"})]


def test_reply_parser_round_trip():
    packets = [ReplyCord.from_dict(200, {"i": i}).pack() for i in range(3)]
    parser = ReplyParser()
    stream = b"".join(packets)
    got = parser.feed(stream[:10]) + parser.feed(stream[10:])
    assert [r.data["i"] for r in got] == [0, 1, 2]
