"""RequestCord wire format: request/reply packets and a stream parser.

Request packet (network byte order, 48-byte header):

    4  Method Code       uint32
    16 User ID           UUID, raw 16 bytes
    16 Session Token     UUID, raw 16 bytes
    2  URL Size          uint16
    2  Message Size      uint16
    4  URL Checksum      CRC-32 of the URL bytes
    4  Message Checksum  CRC-32 of the message bytes
    .. URL               UTF-8, e.g. "chat/<chat_id>/messages"
    .. Message           UTF-8 JSON object, or empty

Reply packet (8-byte header):

    1  Status Code       uint8
    3  Reply Size        uint24
    4  Reply Checksum    CRC-32 of the reply message
    .. Reply Message     UTF-8 JSON object

TCP is a byte stream, so one recv() can return part of a packet, several
packets, or the tail of one packet plus the head of the next. The parsers
below keep a buffer and only hand out packets once every byte has arrived.
"""
from __future__ import annotations

import json
import re
import struct
import sys
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, List, Optional, Tuple, Union

NIL_UUID = uuid.UUID(int=0)

# Method codes, as hard-coded in the frontend's main.py.
POST = 112233445
GET = 354984100
PATCH = 943742156
METHOD_NAMES = {POST: "POST", GET: "GET", PATCH: "PATCH"}


class Status:
    OK = 200
    BAD_REQUEST = 250      # malformed packet or payload (missing/invalid fields)
    UNAUTHORIZED = 251     # bad/expired session, wrong username or password
    FORBIDDEN = 252        # understood but refused (not a member, rate limited, name taken)
    NOT_FOUND = 253        # unknown method/URL, unknown resource, or no credentials on a protected call
    INTERNAL_ERROR = 254   # server-side failure (bug, database down)


# Every path the server pushes by itself (contract v2 section 4). A push carries
# neither "method" nor "rid", which is how a client tells one from a reply.
PUSH_PATHS = frozenset({
    "chat/new_message",       # someone posted in a chat you are in
    "chats/new",              # you were invited to a group, or a DM was opened with you
    "chat/members_changed",   # a member joined, left, renamed, changed colours, or an invite was cancelled
    "users/status",           # somebody went online or offline
    "chats/group_created",    # a new group exists (it shows in the all-groups list)
    "chats/group_deleted",    # its last member left
    "users/request",          # a DM request or invitation changed state
    "users/profile_changed",  # somebody changed their username or colours
    "login/revoked",          # your session ended (signed in elsewhere, logged out, expired)
})


REQUEST_HEADER = struct.Struct("!I16s16sHHII")
REQUEST_HEADER_SIZE = REQUEST_HEADER.size  # 48
REPLY_HEADER = struct.Struct("!B3sI")
REPLY_HEADER_SIZE = REPLY_HEADER.size      # 8
UINT16_MAX = (1 << 16) - 1
UINT24_MAX = (1 << 24) - 1
UINT32_MAX = (1 << 32) - 1


def _iso(dt: datetime) -> str:
    """ISO 8601 in UTC with a "+00:00" offset, never a "Z" suffix (contract v2 section 3)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def json_default(obj: Any) -> Any:
    """Lets UUIDs, datetimes and sets sit in a reply dict without manual conversion."""
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return _iso(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=json_default)


MAX_INT_DIGITS = 20        # longer JSON integers are refused
MAX_JSON_DEPTH = 16
MAX_MESSAGE_BYTES = 32 * 1024   # no call needs more (a 4000-character message is at most ~16 KB)
_HUGE_DIGITS = re.compile(r"\d{4300,}")   # stop absurd numbers before int() sees them (quadratic on 3.9)
_INT_LIMIT = 10 ** MAX_INT_DIGITS


class MessageError(ValueError):
    """A request message that cannot be used. `rid` is set if it could still be read."""

    def __init__(self, detail: str, rid: object = None) -> None:
        super().__init__(detail)
        self.rid = rid


def valid_rid(value: object) -> bool:
    """A request id the server echoes back: an integer, or a string of at most 64 characters."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) < _INT_LIMIT
    return isinstance(value, str) and len(value) <= 64 and "\x00" not in value and _encodable(value)


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _check_values(obj: Any, depth: int = 0) -> None:
    """One walk over the parsed message. Refuses: lone surrogates (cannot be encoded
    back to UTF-8), NUL characters (PostgreSQL text cannot hold them), integers with
    more than 20 digits, non-finite numbers (1e400), and nesting deeper than 16."""
    if depth > MAX_JSON_DEPTH:
        raise ValueError("message is nested too deeply")
    if isinstance(obj, str):
        if "\x00" in obj:
            raise ValueError("strings may not contain NUL characters")
        if not _encodable(obj):
            raise ValueError("strings must be valid Unicode (no lone surrogates)")
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, int):
        if abs(obj) >= _INT_LIMIT:
            raise ValueError("integer has too many digits")
    elif isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ValueError("number is out of range")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _check_values(key, depth + 1)
            _check_values(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _check_values(value, depth + 1)


_RID_RE = re.compile(r'"rid"\s*:\s*(-?\d{1,20}(?![\d.eE])|"(?:[^"\\\x00-\x1f]|\\.){0,64}")')


def peek_rid(text: str) -> object:
    """The rid of a message we will not parse (e.g. too large), found by a regex.
    An unescaped "rid" key cannot occur inside a JSON string value, so a match
    is a real key; the last one wins, as in json.loads."""
    found = None
    for m in _RID_RE.finditer(text):
        found = m.group(1)
    if found is None:
        return None
    try:
        value = json.loads(found)
    except ValueError:
        return None
    return value if valid_rid(value) else None


def parse_message(text: str) -> dict:
    """Parses a request message: a JSON object, or {} when empty. Raises MessageError."""
    if not text.strip():
        return {}
    if _HUGE_DIGITS.search(text):
        raise MessageError("a number has too many digits")
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise MessageError("message is nested too deeply") from None
    except ValueError as exc:
        raise MessageError(f"not valid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise MessageError("message must be a JSON object")
    rid = obj.get("rid")
    rid = rid if valid_rid(rid) else None
    try:
        _check_values(obj)
    except ValueError as exc:
        raise MessageError(str(exc), rid=rid) from None   # rejected, but the reply can still carry rid
    return obj


def _as_uuid(value: Any, name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, (bytes, bytearray)) and len(value) == 16:
        return uuid.UUID(bytes=bytes(value))
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            raise ValueError(f"{name} is not a valid UUID string: {value!r}") from None
    raise ValueError(f"{name} must be a UUID, got {type(value).__name__}")


@dataclass
class RequestCord:
    """One client request. Mirrors models.RequestCord in the frontend repo."""

    method: int = 0
    user_id: uuid.UUID = NIL_UUID
    ses_token: uuid.UUID = NIL_UUID
    url: str = ""
    msg: str = ""

    def __post_init__(self) -> None:
        self.user_id = _as_uuid(self.user_id, "user_id")
        self.ses_token = _as_uuid(self.ses_token, "ses_token")

    @classmethod
    def from_dict(cls, method: int, url: str, data: Optional[dict] = None,
                  user_id: Any = NIL_UUID, ses_token: Any = NIL_UUID) -> "RequestCord":
        req = cls(method=method, user_id=user_id, ses_token=ses_token, url=url)
        req.set_dict(data)
        return req

    def set_dict(self, data: Optional[dict]) -> None:
        if data is None:
            self.msg = ""
            return
        if not isinstance(data, dict):
            raise ValueError(f"message must be a dict, got {type(data).__name__}")
        self.msg = dumps(data)

    def as_dict(self) -> dict:
        """Parses the message as a JSON object. An empty message means {}."""
        return parse_message(self.msg)

    @property
    def has_auth(self) -> bool:
        return self.user_id != NIL_UUID and self.ses_token != NIL_UUID

    @property
    def method_name(self) -> str:
        return METHOD_NAMES.get(self.method, str(self.method))

    def _encoded(self) -> Tuple[bytes, bytes]:
        return self.url.encode("utf-8"), self.msg.encode("utf-8")

    @property
    def url_sz(self) -> int:
        return len(self._encoded()[0])

    @property
    def msg_sz(self) -> int:
        return len(self._encoded()[1])

    @property
    def url_cs(self) -> int:
        return zlib.crc32(self._encoded()[0])

    @property
    def msg_cs(self) -> int:
        return zlib.crc32(self._encoded()[1])

    def validate(self) -> Tuple[bytes, bytes]:
        if isinstance(self.method, bool) or not isinstance(self.method, int) \
                or not 0 <= self.method <= UINT32_MAX:
            raise ValueError(f"method must be an int in 0..{UINT32_MAX}")
        url_b, msg_b = self._encoded()
        if len(url_b) > UINT16_MAX:
            raise ValueError(f"url is {len(url_b)} bytes; the 16-bit size field allows {UINT16_MAX}")
        if len(msg_b) > UINT16_MAX:
            raise ValueError(f"msg is {len(msg_b)} bytes; the 16-bit size field allows {UINT16_MAX}")
        return url_b, msg_b

    def pack(self) -> bytes:
        url_b, msg_b = self.validate()
        header = REQUEST_HEADER.pack(
            self.method, self.user_id.bytes, self.ses_token.bytes,
            len(url_b), len(msg_b), zlib.crc32(url_b), zlib.crc32(msg_b),
        )
        return header + url_b + msg_b


@dataclass
class ReplyCord:
    """One server reply (or server push). Same helpers as RequestCord.

    `data` is the JSON object that becomes the reply message. The router adds
    "path" (the semantic name of the call, see contract v2 section 2) and
    "method", and echoes "rid" if the request carried one, so the client knows
    what the reply is for. A push has a "path" from PUSH_PATHS and neither
    "method" nor "rid".
    """

    status: int = Status.OK
    data: dict = field(default_factory=dict)

    MAX_SIZE: ClassVar[int] = UINT24_MAX

    @classmethod
    def from_dict(cls, status: int = Status.OK, data: Optional[dict] = None, **extra: Any) -> "ReplyCord":
        reply = cls(status=status)
        reply.set_dict(data)
        reply.data.update(extra)
        return reply

    @classmethod
    def push(cls, path: str, data: Optional[dict] = None) -> "ReplyCord":
        """A server push: one of PUSH_PATHS, with no "method" and no "rid"."""
        return cls.from_dict(Status.OK, data, path=path)

    def set_dict(self, data: Optional[dict]) -> None:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"reply data must be a dict, got {type(data).__name__}")
        self.data = dict(data)

    def as_dict(self) -> dict:
        return dict(self.data)

    @property
    def ok(self) -> bool:
        return self.status == Status.OK

    @property
    def path(self) -> str:
        return str(self.data.get("path", ""))

    @property
    def msg(self) -> str:
        return dumps(self.data)

    def _encoded(self) -> bytes:
        return self.msg.encode("utf-8")

    @property
    def msg_sz(self) -> int:
        return len(self._encoded())

    @property
    def msg_cs(self) -> int:
        return zlib.crc32(self._encoded())

    def validate(self) -> bytes:
        if isinstance(self.status, bool) or not isinstance(self.status, int) or not 0 <= self.status <= 255:
            raise ValueError("status must be an int in 0..255")
        body = self._encoded()
        if len(body) > self.MAX_SIZE:
            raise ValueError(f"reply is {len(body)} bytes; the 24-bit size field allows {self.MAX_SIZE}")
        return body

    def pack(self) -> bytes:
        body = self.validate()
        return REPLY_HEADER.pack(self.status, len(body).to_bytes(3, "big"), zlib.crc32(body)) + body

    @classmethod
    def unpack(cls, packet: bytes) -> "ReplyCord":
        """Parses one complete reply packet (header + body)."""
        if len(packet) < REPLY_HEADER_SIZE:
            raise ValueError("packet shorter than the reply header")
        status, size_b, checksum = REPLY_HEADER.unpack_from(packet)
        size = int.from_bytes(size_b, "big")
        body = bytes(packet[REPLY_HEADER_SIZE:REPLY_HEADER_SIZE + size])
        if len(body) != size:
            raise ValueError("incomplete reply packet")
        if zlib.crc32(body) != checksum:
            raise ValueError("reply checksum mismatch")
        data = json.loads(body.decode("utf-8")) if body else {}
        if not isinstance(data, dict):
            raise ValueError("reply message is not a JSON object")
        return cls(status=status, data=data)


@dataclass
class Frame:
    """One request cut out of the byte stream.

    `request` is set when the packet is well formed. Otherwise `error` names
    what was wrong; framing still holds because the sizes come from the
    header, so the stream stays in sync and the next packet parses normally.
    """

    request: Optional[RequestCord] = None
    error: Optional[str] = None
    detail: str = ""
    url_hint: str = ""
    method: int = 0
    size: int = 0          # bytes on the wire
    msg_size: int = 0      # bytes of the message part
    cost: int = 0          # estimated memory while it waits (Python strings can be 4x the wire size)


class RequestParser:
    """Incremental parser: append() whatever recv() returned, take complete frames
    with next_frame() (or both at once with feed())."""

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def append(self, data: bytes) -> None:
        self._buf.extend(data)

    def next_frame(self) -> Optional[Frame]:
        """The next complete packet, or None if the rest has not arrived yet."""
        if len(self._buf) < REQUEST_HEADER_SIZE:
            return None
        method, uid_b, tok_b, url_sz, msg_sz, url_cs, msg_cs = REQUEST_HEADER.unpack_from(self._buf)
        total = REQUEST_HEADER_SIZE + url_sz + msg_sz
        if len(self._buf) < total:
            return None  # wait for the rest of this packet
        url_b = bytes(self._buf[REQUEST_HEADER_SIZE:REQUEST_HEADER_SIZE + url_sz])
        msg_b = bytes(self._buf[REQUEST_HEADER_SIZE + url_sz:total])
        del self._buf[:total]
        frame = self._build(method, uid_b, tok_b, url_b, msg_b, url_cs, msg_cs, total)
        frame.msg_size = msg_sz
        if frame.request is not None:
            frame.cost = 512 + sys.getsizeof(frame.request.url) + sys.getsizeof(frame.request.msg)
        else:
            frame.cost = 512 + total
        return frame

    def feed(self, data: bytes) -> List[Frame]:
        self.append(data)
        frames: List[Frame] = []
        while True:
            frame = self.next_frame()
            if frame is None:
                return frames
            frames.append(frame)

    @staticmethod
    def _build(method: int, uid_b: bytes, tok_b: bytes, url_b: bytes, msg_b: bytes,
               url_cs: int, msg_cs: int, total: int) -> Frame:
        url_hint = url_b.decode("utf-8", errors="replace")
        if zlib.crc32(url_b) != url_cs:
            return Frame(error="URL_CHECKSUM_MISMATCH", detail="URL checksum does not match",
                         url_hint=url_hint, method=method, size=total)
        if zlib.crc32(msg_b) != msg_cs:
            return Frame(error="MESSAGE_CHECKSUM_MISMATCH", detail="message checksum does not match",
                         url_hint=url_hint, method=method, size=total)
        try:
            url = url_b.decode("utf-8")
            msg = msg_b.decode("utf-8")
        except UnicodeDecodeError:
            return Frame(error="INVALID_ENCODING", detail="URL and message must be UTF-8",
                         url_hint=url_hint, method=method, size=total)
        req = RequestCord(method=method, user_id=uuid.UUID(bytes=uid_b),
                          ses_token=uuid.UUID(bytes=tok_b), url=url, msg=msg)
        return Frame(request=req, url_hint=url, method=method, size=total)


class ReplyParser:
    """Client-side counterpart of RequestParser (same logic as the frontend's proc_buffer)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Union[ReplyCord, ValueError]]:
        self._buf.extend(data)
        out: List[Union[ReplyCord, ValueError]] = []
        while len(self._buf) >= REPLY_HEADER_SIZE:
            size = int.from_bytes(self._buf[1:4], "big")
            total = REPLY_HEADER_SIZE + size
            if len(self._buf) < total:
                break
            packet = bytes(self._buf[:total])
            del self._buf[:total]
            try:
                out.append(ReplyCord.unpack(packet))
            except ValueError as exc:
                out.append(exc)
        return out
