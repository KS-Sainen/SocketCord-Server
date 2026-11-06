"""In-memory repository: the same observable behaviour as PostgresRepository, nothing persisted.

Used by the test suite and by `python -m socketcord --db memory`, so the
frontend can be developed against a backend with zero setup.

The state is held as the rows of schema.sql's six tables (users, chats, groups,
dms, group_memberships, messages), and every read builds fresh records from
those rows the way the SQL joins do: usernames and colours are always the
current ones, and nothing a caller does to a returned object reaches the stored
state. One re-entrant lock guards everything, so each method is one atomic
"transaction" that sees one consistent snapshot, and message ids are made under
it (above the chat's newest id, as base.py requires).

Values that schema.sql's CHECK constraints (or its varchar/inet/NOT NULL types)
would refuse raise ValueError here, so code tested only against memory cannot
store something the real database rejects. The order of the checks follows the
order PostgreSQL evaluates them in (row lock, then the row's own constraints,
then unique keys, then foreign keys).
"""
from __future__ import annotations

import ipaddress
import re
import threading
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .. import scram
from ..security import client_key
from .base import (DEFAULT_TEXT_BG_COLOR, DEFAULT_TEXT_COLOR, NO_MESSAGE, AlreadyMember,
                   AlreadyRequested, Chat, ChatListItem, ChatRequest, GroupListItem, JoinResult,
                   Member, Message, MessageQuery, NotFoundError, RateLimited, RegistrationLimit,
                   Repository, SameColors, Session, User, UserRef, UsernameTaken, UUID,
                   message_time, new_message_id)

# The CHECK constraints of schema.sql, as Python sees them (re.fullmatch = PostgreSQL's ^...$).
_USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,32}")
_COLOR_RE = re.compile(r"#[0-9a-f]{6}")
_VERIFIER_RE = re.compile(r"SCRAM-SHA-256\$[1-9][0-9]{3,7}:[A-Za-z0-9+/]{22}==\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=")
_MIN_ITERATIONS, _MAX_ITERATIONS = 4096, 10_000_000
_TOKEN_HASH_LEN = 32
_MAX_CONTENT = 4000
_ANSWERS = ("accepted", "declined", "cancelled")


# ---------------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    """A timestamptz round trip: a naive value is read in the server's TimeZone (UTC)."""
    if not isinstance(value, datetime):
        raise ValueError("expected a datetime")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _lower(text: str) -> str:
    """PostgreSQL's lower(): one character at a time with the simple case mapping
    (no final-sigma rule; U+0130 becomes a plain "i")."""
    if text.isascii():
        return text.lower()
    return "".join(ch.lower()[:1] for ch in text)


def _pair(a: UUID, b: UUID) -> Tuple[UUID, UUID]:
    """The dms_one_per_pair key: (LEAST, GREATEST) in uuid order."""
    return (a, b) if a <= b else (b, a)


def _invalid(what: str) -> ValueError:
    return ValueError(f"rejected by the schema: {what}")


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{what} must be a string")
    if "\x00" in value:
        raise _invalid(f"{what} contains a NUL character")
    return value


def _varchar(value: object, length: int, what: str) -> str:
    """What a varchar(n) column stores: too long is an error, unless the excess is
    all spaces, which PostgreSQL cuts off."""
    text = _text(value, what)
    if len(text) > length:
        if text[length:].strip(" "):
            raise _invalid(f"{what} is longer than {length} characters")
        text = text[:length]
    return text


def _check_verifier(password_hash: str) -> None:
    if _VERIFIER_RE.fullmatch(password_hash) is None:
        raise _invalid("password_hash must be a SCRAM-SHA-256 verifier with a 16-byte salt and 32-byte keys")
    iterations = int(password_hash.split("$")[1].split(":")[0])
    if not _MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS:
        raise _invalid(f"iterations must be in {_MIN_ITERATIONS}..{_MAX_ITERATIONS}")


def _as_bytes(value: object) -> Optional[bytes]:
    """A bytea parameter as bytes; None for anything that is not bytes-like (never matches a row)."""
    return bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else None


def _token_hash(value: object) -> bytes:
    data = _as_bytes(value)
    if data is None or len(data) != _TOKEN_HASH_LEN:
        raise _invalid(f"a session token hash is {_TOKEN_HASH_LEN} bytes")
    return data


def _check_message(content: object, kind: str, sender_id: Optional[UUID]) -> None:
    if kind not in ("text", "system"):
        raise _invalid("message kind must be 'text' or 'system'")
    text = _text(content, "content")
    if not 1 <= len(text) <= _MAX_CONTENT:
        raise _invalid(f"message content must be 1..{_MAX_CONTENT} characters")
    if kind != "text" and sender_id is not None:
        raise _invalid("a system message has no sender")


def _page(offset: int, limit: int) -> Tuple[int, int]:
    """Negative paging values count as 0 (PostgresRepository clamps them the same way)."""
    return max(0, offset), max(0, limit)


# ---------------------------------------------------------------------- rows (schema.sql)

@dataclass
class _UserRow:
    user_id: UUID
    username: str
    password_hash: str
    text_color: str
    text_bg_color: str
    created_at: datetime
    created_ip: Optional[str]
    created_device: Optional[str]
    session_token_hash: Optional[bytes] = None
    session_expires_at: Optional[datetime] = None


@dataclass
class _MessageRow:
    message_id: UUID
    chat_id: UUID
    sender_id: Optional[UUID]
    kind: str
    content: str


@dataclass
class _ChatRow:
    chat_id: UUID
    kind: str                                   # "group" | "dm"
    created_at: datetime
    # The chat's messages (ON DELETE CASCADE): ascending ids, and the rows in the same order.
    message_ids: List[UUID] = field(default_factory=list)
    messages: List[_MessageRow] = field(default_factory=list)


@dataclass
class _MembershipRow:
    user_id: UUID
    group_id: UUID
    status: str                                 # "pending" (invited) | "accepted" (member)
    membership_id: UUID
    invited_by: Optional[UUID]
    created_at: datetime                        # invited or joined
    joined_at: Optional[datetime] = None        # set iff accepted
    last_read_message_id: UUID = NO_MESSAGE


@dataclass
class _GroupRow:
    group_id: UUID
    group_name: str
    description: str
    created_by: Optional[UUID]
    memberships: Dict[UUID, _MembershipRow] = field(default_factory=dict)   # by user_id


@dataclass
class _DmRow:
    dm_id: UUID
    initiator: Optional[UUID]
    receiver: Optional[UUID]
    status: str                                 # "pending" (a request) | "accepted" (open room)
    accepted_at: Optional[datetime] = None      # set iff accepted
    initiator_last_read: UUID = NO_MESSAGE
    receiver_last_read: UUID = NO_MESSAGE


# ---------------------------------------------------------------------- the repository

class MemoryRepository(Repository):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_tick: Optional[datetime] = None
        self._users: Dict[UUID, _UserRow] = {}
        self._by_name: Dict[str, UUID] = {}                    # lower(username) -> user_id
        self._by_token: Dict[bytes, UUID] = {}                 # session_token_hash -> user_id
        self._chats: Dict[UUID, _ChatRow] = {}
        self._groups: Dict[UUID, _GroupRow] = {}
        self._dms: Dict[UUID, _DmRow] = {}
        self._dm_pairs: Dict[Tuple[UUID, UUID], UUID] = {}     # (least, greatest) -> dm_id
        self._membership_ids: Dict[UUID, Tuple[UUID, UUID]] = {}   # membership_id -> (group_id, user_id)

    def _tick(self) -> datetime:
        """now() for one call: strictly increasing, like the start times of successive
        transactions, and one value for every row the call writes."""
        now = _now()
        if self._last_tick is not None and now <= self._last_tick:
            now = self._last_tick + timedelta(microseconds=1)
        self._last_tick = now
        return now

    # ------------------------------------------------------------------ record builders
    @staticmethod
    def _user(row: _UserRow) -> User:
        return User(user_id=row.user_id, username=row.username, password_hash=row.password_hash,
                    text_color=row.text_color, text_bg_color=row.text_bg_color, created_at=row.created_at)

    def _ref(self, user_id: Optional[UUID]) -> Optional[UserRef]:
        row = self._users.get(user_id) if user_id is not None else None
        if row is None:
            return None
        return UserRef(user_id=row.user_id, username=row.username,
                       text_color=row.text_color, text_bg_color=row.text_bg_color)

    def _chat(self, chat_id: UUID) -> Chat:
        row = self._chats[chat_id]
        if row.kind == "group":
            g = self._groups[chat_id]
            return Chat(chat_id=chat_id, kind="group", name=g.group_name, description=g.description,
                        created_by=g.created_by, created_at=row.created_at)
        d = self._dms[chat_id]
        return Chat(chat_id=chat_id, kind="dm", name=None, description="", created_by=d.initiator,
                    created_at=row.created_at, initiator=d.initiator, receiver=d.receiver, status=d.status)

    def _message(self, row: _MessageRow) -> Message:
        return Message(message_id=row.message_id, chat_id=row.chat_id, sender=self._ref(row.sender_id),
                       kind=row.kind, content=row.content, created_at=message_time(row.message_id))

    def _group_member(self, group: _GroupRow, m: _MembershipRow) -> Optional[Member]:
        user = self._users.get(m.user_id)
        if user is None:
            return None
        return Member(chat_id=group.group_id, user_id=m.user_id, username=user.username,
                      text_color=user.text_color, text_bg_color=user.text_bg_color,
                      role="owner" if group.created_by == m.user_id else "member",
                      joined_at=m.joined_at, last_read_message_id=m.last_read_message_id)  # type: ignore[arg-type]

    def _dm_member(self, dm: _DmRow, user_id: Optional[UUID]) -> Optional[Member]:
        user = self._users.get(user_id) if user_id is not None else None
        if user is None:
            return None
        return Member(chat_id=dm.dm_id, user_id=user.user_id, username=user.username,
                      text_color=user.text_color, text_bg_color=user.text_bg_color, role="member",
                      joined_at=dm.accepted_at, last_read_message_id=self._dm_marker(dm, user.user_id))  # type: ignore[arg-type]

    @staticmethod
    def _dm_marker(dm: _DmRow, user_id: UUID) -> UUID:
        return dm.initiator_last_read if user_id == dm.initiator else dm.receiver_last_read

    def _group_members(self, group: _GroupRow) -> List[Member]:
        rows = sorted((m for m in group.memberships.values() if m.status == "accepted"),
                      key=lambda m: (m.joined_at, m.user_id))
        return [member for member in (self._group_member(group, m) for m in rows) if member is not None]

    def _dm_request(self, dm: _DmRow) -> ChatRequest:
        chat = self._chats[dm.dm_id]
        return ChatRequest(request_id=dm.dm_id, kind="dm", sender=self._ref(dm.initiator),
                           receiver=self._ref(dm.receiver), chat_id=dm.dm_id,  # type: ignore[arg-type]
                           chat_name=None, message=chat.messages[0].content if chat.messages else None,
                           status=dm.status, created_at=chat.created_at, responded_at=dm.accepted_at)

    def _invite_request(self, group: _GroupRow, m: _MembershipRow) -> ChatRequest:
        return ChatRequest(request_id=m.membership_id, kind="invite", sender=self._ref(m.invited_by),
                           receiver=self._ref(m.user_id), chat_id=group.group_id,  # type: ignore[arg-type]
                           chat_name=group.group_name, message=None, status=m.status,
                           created_at=m.created_at, responded_at=m.joined_at)

    def _item(self, chat_id: UUID, user_id: UUID) -> Optional[ChatListItem]:
        """The list_user_chats() row of one chat, or None if the user is not in it."""
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        peer = None
        if chat.kind == "group":
            group = self._groups[chat_id]
            me = group.memberships.get(user_id)
            if me is None or me.status != "accepted":
                return None
            role = "owner" if group.created_by == user_id else "member"
            marker = me.last_read_message_id
            member_count = sum(1 for m in group.memberships.values() if m.status == "accepted")
        else:
            dm = self._dms[chat_id]
            if dm.status != "accepted" or user_id not in (dm.initiator, dm.receiver):
                return None
            role = "member"
            marker = self._dm_marker(dm, user_id)
            member_count = sum(1 for uid in (dm.initiator, dm.receiver) if uid is not None)
            peer = self._ref(dm.receiver if user_id == dm.initiator else dm.initiator)
        # Unread = messages above the marker that someone else sent (system lines included).
        start = bisect_right(chat.message_ids, marker)
        unread = sum(1 for m in chat.messages[start:] if m.sender_id != user_id)
        last = self._message(chat.messages[-1]) if chat.messages else None
        return ChatListItem(chat=self._chat(chat_id), role=role, last_read_message_id=marker, unread=unread,
                            member_count=member_count, last_message=last, peer=peer)

    @staticmethod
    def _activity_key(item: ChatListItem) -> Tuple[datetime, UUID, UUID]:
        # Same order as PostgreSQL: latest activity, then the later message (ids are
        # time-ordered; a chat with no message sorts last on a tie), then chat_id.
        last = item.last_message
        when = last.created_at if last is not None else item.chat.created_at
        return when, (last.message_id if last is not None else NO_MESSAGE), item.chat.chat_id

    # ------------------------------------------------------------------ row writers
    @staticmethod
    def _newest_id(chat: _ChatRow) -> UUID:
        return chat.message_ids[-1] if chat.message_ids else NO_MESSAGE

    def _insert_message(self, chat: _ChatRow, sender_id: Optional[UUID], kind: str, content: str) -> _MessageRow:
        # Under the repository lock and above the chat's newest id (base.py's id rule).
        message_id = new_message_id(after=chat.message_ids[-1] if chat.message_ids else None)
        row = _MessageRow(message_id=message_id, chat_id=chat.chat_id, sender_id=sender_id, kind=kind,
                          content=content)
        chat.message_ids.append(message_id)
        chat.messages.append(row)
        return row

    def _insert_dm(self, initiator: UUID, receiver: UUID, status: str, now: datetime) -> _DmRow:
        dm_id = uuid.uuid4()
        self._chats[dm_id] = _ChatRow(chat_id=dm_id, kind="dm", created_at=now)
        dm = _DmRow(dm_id=dm_id, initiator=initiator, receiver=receiver, status=status,
                    accepted_at=now if status == "accepted" else None)
        self._dms[dm_id] = dm
        self._dm_pairs[_pair(initiator, receiver)] = dm_id
        return dm

    def _add_membership(self, group: _GroupRow, user_id: UUID, status: str, invited_by: Optional[UUID],
                        now: datetime, last_read: UUID = NO_MESSAGE) -> _MembershipRow:
        m = _MembershipRow(user_id=user_id, group_id=group.group_id, status=status, membership_id=uuid.uuid4(),
                           invited_by=invited_by, created_at=now,
                           joined_at=now if status == "accepted" else None, last_read_message_id=last_read)
        group.memberships[user_id] = m
        self._membership_ids[m.membership_id] = (group.group_id, user_id)
        return m

    def _drop_membership(self, group: _GroupRow, m: _MembershipRow) -> None:
        group.memberships.pop(m.user_id, None)
        self._membership_ids.pop(m.membership_id, None)

    def _delete_chat(self, chat_id: UUID) -> None:
        """DELETE FROM chats: the groups / dms row, the memberships and the messages go with it."""
        self._chats.pop(chat_id, None)
        group = self._groups.pop(chat_id, None)
        if group is not None:
            for m in group.memberships.values():
                self._membership_ids.pop(m.membership_id, None)
        dm = self._dms.pop(chat_id, None)
        if dm is not None and dm.initiator is not None and dm.receiver is not None:
            key = _pair(dm.initiator, dm.receiver)
            if self._dm_pairs.get(key) == chat_id:
                del self._dm_pairs[key]

    def _set_session(self, row: _UserRow, token_hash: Optional[bytes], expires_at: Optional[datetime]) -> None:
        if row.session_token_hash is not None and self._by_token.get(row.session_token_hash) == row.user_id:
            del self._by_token[row.session_token_hash]
        row.session_token_hash, row.session_expires_at = token_hash, expires_at
        if token_hash is not None:
            self._by_token[token_hash] = row.user_id

    def _check_token_free(self, token_hash: bytes, user_id: UUID) -> None:
        holder = self._by_token.get(token_hash)
        if holder is not None and holder != user_id:   # users.session_token_hash is UNIQUE
            raise _invalid("session token hash is already in use")

    # ------------------------------------------------------------------ users
    def _retry_after_locked(self, limit: RegistrationLimit, now: datetime) -> float:
        window = timedelta(seconds=limit.window_seconds)
        worst = 0.0
        for rule in limit.rules:
            if rule.kind == "ip":   # the value is an IPv4 address or an IPv6 /64
                def match(u: _UserRow, value: str = rule.value) -> bool:
                    return u.created_ip is not None and client_key(u.created_ip) == value
            else:
                def match(u: _UserRow, value: str = rule.value) -> bool:
                    return u.created_device == value
            hits = sorted((u.created_at for u in self._users.values() if u.created_at > now - window and match(u)),
                          reverse=True)
            if len(hits) >= rule.max_count:
                oldest_counted = hits[rule.max_count - 1]
                worst = max(worst, (oldest_counted + window - now).total_seconds())
        return max(0.0, worst)

    def registration_retry_after(self, limit: RegistrationLimit) -> float:
        if limit is None or not limit.rules:
            return 0.0
        with self._lock:
            return self._retry_after_locked(limit, _now())

    def create_user(self, username: str, salt: bytes, iterations: int, stored_key: bytes,
                    server_key: bytes, created_ip: Optional[str], created_device: Optional[str],
                    limit: Optional[RegistrationLimit] = None) -> User:
        password_hash = scram.encode_verifier(salt, iterations, stored_key, server_key)
        with self._lock:
            now = self._tick()
            # Same order as PostgreSQL: the limit first, then the row's checks, then the unique name.
            if limit is not None and limit.rules:
                retry = self._retry_after_locked(limit, now)
                if retry > 0:
                    raise RateLimited(retry)
            username = _varchar(username, 32, "username")
            if _USERNAME_RE.fullmatch(username) is None:
                raise _invalid("username must be 3..32 of A-Z a-z 0-9 _ . -")
            _check_verifier(password_hash)
            if created_ip is not None:
                try:   # what the inet type accepts (it has no IPv6 scope ids such as %lo0)
                    if "%" in _text(created_ip, "created_ip"):
                        raise ValueError(created_ip)
                    ipaddress.ip_interface(created_ip)
                except ValueError:
                    raise _invalid("created_ip is not an IP address") from None
            if created_device is not None:
                created_device = _varchar(created_device, 128, "created_device")
            key = username.lower()
            if key in self._by_name:
                raise UsernameTaken(username)
            row = _UserRow(user_id=uuid.uuid4(), username=username, password_hash=password_hash,
                           text_color=DEFAULT_TEXT_COLOR, text_bg_color=DEFAULT_TEXT_BG_COLOR, created_at=now,
                           created_ip=created_ip, created_device=created_device)
            self._users[row.user_id] = row
            self._by_name[key] = row.user_id
            return self._user(row)

    def get_user(self, user_id: UUID) -> Optional[User]:
        with self._lock:
            row = self._users.get(user_id)
            return self._user(row) if row is not None else None

    def get_user_by_username(self, username: str) -> Optional[User]:
        with self._lock:
            uid = self._by_name.get(_lower(username.strip()))
            return self._user(self._users[uid]) if uid is not None else None

    def get_users(self, user_ids: Iterable[UUID]) -> Dict[UUID, User]:
        with self._lock:
            return {uid: self._user(self._users[uid]) for uid in user_ids if uid in self._users}

    def search_users(self, prefix: str, offset: int = 0, limit: int = 20) -> Tuple[List[User], bool]:
        offset, limit = _page(offset, limit)
        needle = _lower(prefix)
        with self._lock:
            found = [row for row in self._users.values() if row.username.lower().startswith(needle)]
            # Exact match first, then lower(username) in code-point order (= COLLATE "C").
            found.sort(key=lambda row: (row.username.lower() != needle, row.username.lower()))
            page = found[offset:offset + limit]
            return [self._user(row) for row in page], len(found) > offset + limit

    def update_profile(self, user_id: UUID, text_color: Optional[str],
                       text_bg_color: Optional[str]) -> Optional[User]:
        with self._lock:
            row = self._users.get(user_id)
            if row is None:
                return None
            color = row.text_color if text_color is None else _varchar(text_color, 7, "text_color")
            bg = row.text_bg_color if text_bg_color is None else _varchar(text_bg_color, 7, "text_bg_color")
            if color == bg or _COLOR_RE.fullmatch(color) is None or _COLOR_RE.fullmatch(bg) is None:
                # A CHECK fails. As in PostgresRepository: with well-formed colours given,
                # the one that failed is text_color <> text_bg_color.
                if all(c is None or _COLOR_RE.fullmatch(c) for c in (text_color, text_bg_color)):
                    raise SameColors("the text and background colours would be the same")
                raise _invalid("colours must be '#rrggbb' in lowercase")
            row.text_color, row.text_bg_color = color, bg
            return self._user(row)

    # ------------------------------------------------------------------ sessions
    def create_session(self, token_hash: bytes, user_id: UUID, expires_at: datetime,
                       expected_verifier: str) -> Optional[Tuple[Session, Optional[bytes]]]:
        with self._lock:
            row = self._users.get(user_id)
            if row is None or row.password_hash != expected_verifier:
                return None
            token_hash = _token_hash(token_hash)
            expires_at = _utc(expires_at)
            self._check_token_free(token_hash, user_id)
            replaced = row.session_token_hash
            self._set_session(row, token_hash, expires_at)
            return Session(token_hash=token_hash, user_id=user_id, expires_at=expires_at), replaced

    def get_session(self, token_hash: bytes) -> Optional[Tuple[Session, User]]:
        key = _as_bytes(token_hash)
        with self._lock:
            uid = self._by_token.get(key) if key is not None else None
            if uid is None:
                return None
            row = self._users[uid]
            session = Session(token_hash=row.session_token_hash, user_id=uid,  # type: ignore[arg-type]
                              expires_at=row.session_expires_at)  # type: ignore[arg-type]
            return session, self._user(row)

    def revoke_session(self, token_hash: bytes) -> bool:
        key = _as_bytes(token_hash)
        with self._lock:
            uid = self._by_token.get(key) if key is not None else None
            if uid is None:
                return False
            self._set_session(self._users[uid], None, None)
            return True

    def change_credentials(self, user_id: UUID, salt: bytes, iterations: int, stored_key: bytes,
                           server_key: bytes, expected_verifier: str, current_token_hash: bytes,
                           new_token_hash: bytes, expires_at: datetime) -> Optional[Session]:
        password_hash = scram.encode_verifier(salt, iterations, stored_key, server_key)
        with self._lock:
            row = self._users.get(user_id)
            if row is None or row.password_hash != expected_verifier or row.session_token_hash is None \
                    or row.session_token_hash != _as_bytes(current_token_hash):
                return None
            _check_verifier(password_hash)
            new_token_hash = _token_hash(new_token_hash)
            expires_at = _utc(expires_at)
            self._check_token_free(new_token_hash, user_id)
            row.password_hash = password_hash
            self._set_session(row, new_token_hash, expires_at)
            return Session(token_hash=new_token_hash, user_id=user_id, expires_at=expires_at)

    # ------------------------------------------------------------------ chats
    def create_group(self, name: str, description: str, owner_id: UUID,
                     chat_id: Optional[UUID] = None) -> Chat:
        with self._lock:
            name = _varchar(name, 64, "group name")
            if not name.strip(" "):   # btrim(group_name) <> '' (btrim strips spaces only)
                raise _invalid("the group name is blank")
            description = _varchar(description, 500, "description")   # NOT NULL: None is refused too
            if owner_id not in self._users:
                raise NotFoundError("user")
            now = self._tick()
            chat_id = chat_id or uuid.uuid4()
            if chat_id in self._chats:
                raise _invalid("that chat id is taken")
            self._chats[chat_id] = _ChatRow(chat_id=chat_id, kind="group", created_at=now)
            group = _GroupRow(group_id=chat_id, group_name=name, description=description, created_by=owner_id)
            self._groups[chat_id] = group
            self._add_membership(group, owner_id, "accepted", None, now)
            return self._chat(chat_id)

    def get_chat(self, chat_id: UUID) -> Optional[Chat]:
        with self._lock:
            return self._chat(chat_id) if chat_id in self._chats else None

    def find_dm(self, user_a: UUID, user_b: UUID) -> Optional[Chat]:
        if user_a is None or user_b is None:
            return None
        with self._lock:
            dm_id = self._dm_pairs.get(_pair(user_a, user_b))
            return self._chat(dm_id) if dm_id is not None else None

    def create_dm(self, initiator: UUID, receiver: UUID) -> Tuple[Chat, bool]:
        if initiator is None or receiver is None:
            raise _invalid("a DM needs both an initiator and a receiver")
        with self._lock:
            existing = self._dm_pairs.get(_pair(initiator, receiver))
            if existing is not None:
                return self._chat(existing), False
            if initiator == receiver:
                raise _invalid("a DM needs two different people")
            if initiator not in self._users or receiver not in self._users:
                raise NotFoundError("user")
            dm = self._insert_dm(initiator, receiver, "accepted", self._tick())
            return self._chat(dm.dm_id), True

    def list_user_chats(self, user_id: UUID) -> List[ChatListItem]:
        with self._lock:
            items = [item for item in (self._item(cid, user_id) for cid in self._chats_of(user_id))
                     if item is not None]
            # ORDER BY coalesce(newest message's sent_at, chat created_at) DESC, chat_id DESC
            items.sort(key=self._activity_key, reverse=True)
            return items

    def _chats_of(self, user_id: UUID) -> List[UUID]:
        ids = [gid for gid, g in self._groups.items()
               if user_id in g.memberships and g.memberships[user_id].status == "accepted"]
        ids.extend(did for did, d in self._dms.items()
                   if d.status == "accepted" and user_id in (d.initiator, d.receiver))
        return ids

    def get_chat_item(self, user_id: UUID, chat_id: UUID) -> Optional[ChatListItem]:
        with self._lock:
            return self._item(chat_id, user_id)

    def list_groups(self, offset: int = 0, limit: int = 200) -> Tuple[List[GroupListItem], bool]:
        offset, limit = _page(offset, limit)
        with self._lock:
            groups = [g for g in self._groups.values()
                      if any(m.status == "accepted" for m in g.memberships.values())]
            groups.sort(key=lambda g: (self._chats[g.group_id].created_at, g.group_id))
            page = groups[offset:offset + limit]
            return ([GroupListItem(chat=self._chat(g.group_id), members=self._group_members(g)) for g in page],
                    len(groups) > offset + limit)

    def get_members(self, chat_id: UUID) -> List[Member]:
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None:
                return []
            if chat.kind == "group":
                return self._group_members(self._groups[chat_id])
            dm = self._dms[chat_id]
            if dm.status != "accepted":
                return []
            members = [m for m in (self._dm_member(dm, dm.initiator), self._dm_member(dm, dm.receiver))
                       if m is not None]
            members.sort(key=lambda m: (m.joined_at, m.user_id))
            return members

    def get_member(self, chat_id: UUID, user_id: UUID) -> Optional[Member]:
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None:
                return None
            if chat.kind == "group":
                group = self._groups[chat_id]
                m = group.memberships.get(user_id)
                return self._group_member(group, m) if m is not None and m.status == "accepted" else None
            dm = self._dms[chat_id]
            if dm.status != "accepted" or user_id is None or user_id not in (dm.initiator, dm.receiver):
                return None
            return self._dm_member(dm, user_id)

    def join_group(self, chat_id: UUID, user_id: UUID) -> Optional[JoinResult]:
        with self._lock:
            group = self._groups.get(chat_id)
            if group is None:
                raise NotFoundError("group")
            m = group.memberships.get(user_id)
            if m is not None and m.status == "accepted":
                return None
            if m is None and user_id not in self._users:
                raise NotFoundError("user")
            now = self._tick()
            newest = self._newest_id(self._chats[chat_id])
            if m is not None:   # the pending invite becomes the membership
                m.status, m.joined_at = "accepted", now
                m.last_read_message_id = max(m.last_read_message_id, newest)
                return JoinResult(settled=self._invite_request(group, m))
            self._add_membership(group, user_id, "accepted", None, now, last_read=newest)
            return JoinResult(settled=None)

    def remove_member(self, chat_id: UUID, user_id: UUID) -> Tuple[bool, bool]:
        with self._lock:
            group = self._groups.get(chat_id)
            m = group.memberships.get(user_id) if group is not None else None
            if m is None or m.status != "accepted":
                return False, False
            self._drop_membership(group, m)  # type: ignore[arg-type]
            if any(x.status == "accepted" for x in group.memberships.values()):  # type: ignore[union-attr]
                return True, False
            # The last accepted member left: the group goes, with its pending invites and messages.
            self._delete_chat(chat_id)
            return True, True

    def mark_read(self, chat_id: UUID, user_id: UUID, message_id: Optional[UUID]) -> UUID:
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None:
                return NO_MESSAGE
            # The newest message of THIS chat at or below message_id (None = the newest).
            ids = chat.message_ids
            if message_id is None:
                target = ids[-1] if ids else NO_MESSAGE
            else:
                at = bisect_right(ids, message_id)          # ids are kept in order
                target = ids[at - 1] if at else NO_MESSAGE
            if chat.kind == "group":
                m = self._groups[chat_id].memberships.get(user_id)
                if m is None or m.status != "accepted":
                    return NO_MESSAGE
                m.last_read_message_id = max(m.last_read_message_id, target)
                return m.last_read_message_id
            dm = self._dms[chat_id]
            if dm.status != "accepted" or user_id is None:
                return NO_MESSAGE
            if user_id == dm.initiator:
                dm.initiator_last_read = max(dm.initiator_last_read, target)
                return dm.initiator_last_read
            if user_id == dm.receiver:
                dm.receiver_last_read = max(dm.receiver_last_read, target)
                return dm.receiver_last_read
            return NO_MESSAGE

    # ------------------------------------------------------------------ messages
    def add_message(self, chat_id: UUID, sender_id: Optional[UUID], content: str,
                    kind: str = "text") -> Message:
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None:
                raise NotFoundError("chat")
            _check_message(content, kind, sender_id)
            if sender_id is not None and sender_id not in self._users:
                raise NotFoundError("user")
            return self._message(self._insert_message(chat, sender_id, kind, content))

    def list_messages(self, chat_id: UUID, query: MessageQuery) -> Tuple[List[Message], bool]:
        count = max(0, query.count)
        before_time = _utc(query.before_time) if query.before_time is not None else None
        after_time = _utc(query.after_time) if query.after_time is not None else None
        needle = _lower(query.content) if query.content else None
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None:
                return [], False
            lo = bisect_right(chat.message_ids, query.after_id) if query.after_id is not None else 0
            hi = bisect_left(chat.message_ids, query.before_id) if query.before_id is not None else len(chat.message_ids)
            matched = []
            for row in chat.messages[lo:hi]:
                if before_time is not None or after_time is not None:
                    sent_at = message_time(row.message_id)
                    if before_time is not None and not sent_at < before_time:
                        continue
                    if after_time is not None and not sent_at > after_time:
                        continue
                if needle is not None and needle not in _lower(row.content):
                    continue
                matched.append(row)
            if query.ascending:
                page = matched[:count]
            else:   # the newest `count`, returned oldest first
                page = matched[len(matched) - count:] if count else []
            return [self._message(row) for row in page], len(matched) > len(page)

    def first_message_after(self, chat_id: UUID, after: UUID) -> Optional[UUID]:
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None or after is None:
                return None
            i = bisect_right(chat.message_ids, after)
            return chat.message_ids[i] if i < len(chat.message_ids) else None

    # ------------------------------------------------------------------ DM requests and group invites
    def create_request(self, kind: str, from_user: UUID, to_user: UUID,
                       chat_id: Optional[UUID] = None, message: Optional[str] = None) -> ChatRequest:
        with self._lock:
            if kind == "invite":
                return self._create_invite(from_user, to_user, chat_id)
            if kind == "dm":
                return self._create_dm_request(from_user, to_user, message)
            raise ValueError(f"unknown request kind {kind!r}")

    def _create_invite(self, from_user: UUID, to_user: UUID, chat_id: Optional[UUID]) -> ChatRequest:
        if chat_id is None:
            raise ValueError("an invite needs the group's chat_id")
        group = self._groups.get(chat_id)
        existing = group.memberships.get(to_user) if group is not None else None

        def refuse(m: _MembershipRow) -> Exception:
            return AlreadyMember() if m.status == "accepted" else AlreadyRequested()

        if from_user == to_user:   # CHECK (invited_by IS DISTINCT FROM user_id), after the conflict
            if existing is not None:
                raise refuse(existing)
            raise _invalid("nobody can invite themselves")
        if existing is not None:
            raise refuse(existing)
        # A DM's chat id is not a group (the groups foreign key fails, like a missing group).
        if group is None or to_user not in self._users or (from_user is not None and from_user not in self._users):
            raise NotFoundError("group or user")
        m = self._add_membership(group, to_user, "pending", from_user, self._tick())
        return self._invite_request(group, m)

    def _create_dm_request(self, from_user: UUID, to_user: UUID, message: Optional[str]) -> ChatRequest:
        if from_user == to_user:
            raise _invalid("a DM needs two different people")
        if from_user is None or to_user is None:
            raise _invalid("a DM needs both an initiator and a receiver")
        if _pair(from_user, to_user) in self._dm_pairs:   # any status, either direction
            raise AlreadyRequested()
        if from_user not in self._users or to_user not in self._users:
            raise NotFoundError("user")
        if message:   # no first message for None or "" (as PostgresRepository)
            _check_message(message, "text", from_user)
        # Checked first, so a failure leaves nothing behind (one transaction in PostgreSQL).
        dm = self._insert_dm(from_user, to_user, "pending", self._tick())
        if message:
            first = self._insert_message(self._chats[dm.dm_id], from_user, "text", message)
            dm.initiator_last_read = first.message_id   # your own message counts as read
        return self._dm_request(dm)

    def _find_request(self, request_id: UUID):
        """(dm row, None, None) or (None, group row, membership row) or (None, None, None)."""
        dm = self._dms.get(request_id)
        if dm is not None:
            return dm, None, None
        where = self._membership_ids.get(request_id)
        if where is None:
            return None, None, None
        group = self._groups[where[0]]
        return None, group, group.memberships[where[1]]

    def get_request(self, request_id: UUID) -> Optional[ChatRequest]:
        with self._lock:
            dm, group, m = self._find_request(request_id)
            if dm is not None:
                return self._dm_request(dm)
            # A membership is an invite while pending, or once accepted if someone invited
            # it (the owner's own row and a direct join are not requests).
            if m is not None and (m.status == "pending" or m.invited_by is not None):
                return self._invite_request(group, m)
            return None

    def _pending(self, user_id: UUID) -> Tuple[List[ChatRequest], List[ChatRequest]]:
        incoming: List[ChatRequest] = []
        outgoing: List[ChatRequest] = []
        for dm in self._dms.values():
            if dm.status == "pending":
                if dm.receiver == user_id:
                    incoming.append(self._dm_request(dm))
                if dm.initiator == user_id:
                    outgoing.append(self._dm_request(dm))
        for group in self._groups.values():
            for m in group.memberships.values():
                if m.status == "pending":
                    if m.user_id == user_id:
                        incoming.append(self._invite_request(group, m))
                    if m.invited_by == user_id:
                        outgoing.append(self._invite_request(group, m))
        return incoming, outgoing

    def list_pending_requests(self, user_id: UUID) -> Tuple[List[ChatRequest], List[ChatRequest]]:
        with self._lock:
            incoming, outgoing = self._pending(user_id)
        incoming.sort(key=lambda r: (r.created_at, r.request_id))
        outgoing.sort(key=lambda r: (r.created_at, r.request_id))
        return incoming, outgoing

    def list_pending_invites(self, chat_id: UUID, limit: int = 500) -> Tuple[List[ChatRequest], int]:
        limit = max(0, limit)
        with self._lock:
            group = self._groups.get(chat_id)
            if group is None:
                return [], 0
            pending = sorted((m for m in group.memberships.values() if m.status == "pending"),
                             key=lambda m: (m.created_at, m.membership_id))
            return [self._invite_request(group, m) for m in pending[:limit]], len(pending)

    def resolve_request(self, request_id: UUID, status: str, actor_id: UUID) -> Optional[ChatRequest]:
        if status not in _ANSWERS:
            raise ValueError(f"status must be one of {', '.join(_ANSWERS)}")

        def may_answer(sender: Optional[UUID], receiver: Optional[UUID]) -> bool:
            who = sender if status == "cancelled" else receiver
            return who is not None and who == actor_id

        with self._lock:
            dm, group, m = self._find_request(request_id)
            if dm is not None:
                if dm.status != "pending" or not may_answer(dm.initiator, dm.receiver):
                    return None
                now = self._tick()
                if status == "accepted":
                    dm.status, dm.accepted_at = "accepted", now
                    return self._dm_request(dm)
                answered = self._dm_request(dm)
                self._delete_chat(dm.dm_id)   # the request's chat and first message go with it
                answered.status, answered.responded_at = status, now
                return answered
            if m is None or m.status != "pending" or not may_answer(m.invited_by, m.user_id):
                return None
            now = self._tick()
            if status == "accepted":
                m.status, m.joined_at = "accepted", now
                m.last_read_message_id = max(m.last_read_message_id, self._newest_id(self._chats[group.group_id]))
                return self._invite_request(group, m)
            answered = self._invite_request(group, m)
            self._drop_membership(group, m)
            answered.status, answered.responded_at = status, now
            return answered

    def count_pending_requests(self, user_id: UUID) -> Tuple[int, int]:
        with self._lock:
            incoming = outgoing = 0
            for dm in self._dms.values():
                if dm.status == "pending":
                    incoming += dm.receiver == user_id
                    outgoing += dm.initiator == user_id
            for group in self._groups.values():
                for m in group.memberships.values():
                    if m.status == "pending":
                        incoming += m.user_id == user_id
                        outgoing += m.invited_by == user_id
            return incoming, outgoing
