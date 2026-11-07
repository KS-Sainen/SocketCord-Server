"""Records and the repository interface the handlers talk to.

Two implementations exist: PostgresRepository (the real one, used in Docker)
and MemoryRepository (no setup, nothing persisted; for tests and quick runs).
Handlers only ever see this interface, and tests/test_repository.py holds both
to the same contract.

The data model is the team's (see schema.sql): users, chats, messages, and two
kinds of chat that extend Chat: groups (with group_memberships, status
'pending' = invited, 'accepted' = member) and dms (initiator, receiver; status
'pending' = a DM request, 'accepted' = an open DM room).

Rules every implementation follows:

* Message ids are UUIDv7 from new_message_id(), made while holding the chat's
  write lock and above the chat's newest id, so within one chat a larger id is
  never visible before a smaller one (paging with after_id never skips).
* "Nothing read" is NO_MESSAGE (the nil UUID), never None: it sorts before
  every message id.
* Lock order in PostgreSQL: the chats row first, in the strongest mode the
  transaction will need (FOR UPDATE when it deletes the chat or a membership,
  FOR NO KEY UPDATE when it flips a status or inserts a message), then dms /
  group_memberships rows, then messages. Never lock a row again in a stronger mode.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .. import scram

UUID = uuid.UUID
NO_MESSAGE = uuid.UUID(int=0)       # "nothing read yet": sorts before every message id
DEFAULT_TEXT_COLOR = "#171717"      # the frontend's own default user style
DEFAULT_TEXT_BG_COLOR = "#f2f2f2"


# ---------------------------------------------------------------------- message ids

class _MessageIds:
    """Time-ordered message ids: UUID version 7 (RFC 9562).

    48 bits of Unix milliseconds, the version, a 12-bit counter for ids made in
    the same millisecond, the variant, then 62 random bits. Ids never repeat or
    go backwards within a process, even if the clock steps back: the counter
    keeps counting and moves on to the next millisecond when it overflows.
    Passing `after` (the chat's newest id) also keeps them above ids made before
    a restart with the clock behind. PostgreSQL compares uuid values bytewise and
    Python compares UUID.int, so in both a later id sorts after an earlier one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ms = 0
        self._seq = 0

    def __call__(self, after: Optional[UUID] = None) -> UUID:
        with self._lock:
            if after is not None and after != NO_MESSAGE:
                floor = (after.int >> 80, (after.int >> 64) & 0xFFF)
                if floor >= (self._ms, self._seq):
                    self._ms, self._seq = floor
            now_ms = time.time_ns() // 1_000_000
            if now_ms > self._ms:
                self._ms, self._seq = now_ms, 0
            else:
                self._seq += 1
                if self._seq > 0xFFF:
                    self._ms, self._seq = self._ms + 1, 0
            ms, seq = self._ms, self._seq
        rand = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
        return uuid.UUID(int=(ms << 80) | (0x7 << 76) | (seq << 64) | (0b10 << 62) | rand)


new_message_id = _MessageIds()


def message_time(message_id: UUID) -> datetime:
    """The Timestamp of a message: the 48-bit millisecond clock inside its UUIDv7
    (schema.sql computes messages.sent_at the same way)."""
    return datetime.fromtimestamp((message_id.int >> 80) / 1000.0, tz=timezone.utc)


# ---------------------------------------------------------------------- errors

class RepositoryError(Exception):
    """Base for data-layer errors that handlers turn into status codes."""


class UsernameTaken(RepositoryError):
    pass


class RateLimited(RepositoryError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class AlreadyRequested(RepositoryError):
    """A pending request or invite for the same pair / group already exists."""


class AlreadyMember(RepositoryError):
    """The invitee is already an accepted member of the group."""


class SameColors(RepositoryError):
    """A profile update would make the text colour equal the background colour."""


class NotFoundError(RepositoryError):
    """The row a call depends on is gone (e.g. a group deleted a moment ago)."""


class DatabaseUnavailable(RepositoryError):
    pass


# ---------------------------------------------------------------------- records

@dataclass
class User:
    user_id: UUID
    username: str
    password_hash: str            # the SCRAM verifier, scram.encode_verifier() format
    text_color: str               # "#rrggbb", lowercase
    text_bg_color: str
    created_at: datetime
    # Parsed from password_hash.
    salt: bytes = field(init=False, repr=False)
    iterations: int = field(init=False)
    stored_key: bytes = field(init=False, repr=False)
    server_key: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        v = scram.decode_verifier(self.password_hash)
        self.salt, self.iterations, self.stored_key, self.server_key = v.salt, v.iterations, v.stored_key, v.server_key


@dataclass
class UserRef:
    """A user as embedded in other objects (sender, peer, inviter, ...)."""
    user_id: UUID
    username: str
    text_color: str = DEFAULT_TEXT_COLOR
    text_bg_color: str = DEFAULT_TEXT_BG_COLOR


@dataclass
class Session:
    token_hash: bytes             # sha256 of the session token
    user_id: UUID
    expires_at: datetime


@dataclass
class Chat:
    chat_id: UUID
    kind: str                     # "group" | "dm"
    name: Optional[str]           # the group's name; None for a DM
    description: str              # "" for a DM
    created_by: Optional[UUID]    # the group's creator; for a DM, the initiator
    created_at: datetime
    initiator: Optional[UUID] = None   # DMs only
    receiver: Optional[UUID] = None    # DMs only
    status: str = "accepted"           # DMs: "pending" until the receiver accepts


@dataclass
class Member:
    """An accepted member of a group, or one of the two people of an accepted DM."""
    chat_id: UUID
    user_id: UUID
    username: str
    text_color: str
    text_bg_color: str
    role: str                     # "owner" (the group's creator) | "member"
    joined_at: datetime
    last_read_message_id: UUID = NO_MESSAGE


@dataclass
class Message:
    message_id: UUID              # UUIDv7
    chat_id: UUID
    sender: Optional[UserRef]     # None for system notices (and once the sender's account is gone)
    kind: str                     # "text" | "system"
    content: str
    created_at: datetime          # = message_time(message_id)

    @property
    def sender_id(self) -> Optional[UUID]:
        return self.sender.user_id if self.sender else None


@dataclass
class ChatRequest:
    """A DM request (a pending DM) or a group invite (a pending membership).

    request_id: for a DM request, the DM's chat id; for an invite, its
    membership_id. status is "pending" or "accepted" for rows that exist;
    "declined" / "cancelled" only appear on the record a resolve returns, because
    those rows are deleted."""
    request_id: UUID
    kind: str                     # "dm" | "invite"
    sender: Optional[UserRef]     # who asked or invited (None once that account is gone)
    receiver: UserRef             # who must answer
    chat_id: UUID                 # the DM room, or the group
    chat_name: Optional[str]      # the group's name (invites)
    message: Optional[str]        # a DM request's first message
    status: str
    created_at: datetime
    responded_at: Optional[datetime] = None

    @property
    def from_user(self) -> Optional[UUID]:
        return self.sender.user_id if self.sender else None

    @property
    def to_user(self) -> UUID:
        return self.receiver.user_id


@dataclass
class ChatListItem:
    """One row of GET chats: a chat the user is in (accepted membership / accepted DM)."""
    chat: Chat
    role: str
    last_read_message_id: UUID
    unread: int                   # messages after the marker that someone else sent
    member_count: int
    last_message: Optional[Message] = None
    peer: Optional[UserRef] = None    # the other person, for DMs


@dataclass
class GroupListItem:
    chat: Chat
    members: List[Member] = field(default_factory=list)


@dataclass
class MemberGroup:
    """One row of GET users/{user_id}/groups: a group the user is an accepted member of."""
    chat: Chat
    role: str                     # that user's role in it: "owner" | "member"
    joined_at: datetime           # when that user joined
    member_count: int             # accepted members
    viewer_is_member: bool = False    # the user who asked is an accepted member too


@dataclass
class RegistrationRule:
    """At most `max_count` accounts per window whose created_ip / created_device equals `value`."""

    kind: str          # "ip" | "device"
    value: str
    max_count: int


@dataclass
class RegistrationLimit:
    window_seconds: int
    rules: List[RegistrationRule] = field(default_factory=list)

    @property
    def keys(self) -> List[str]:
        return sorted(f"{r.kind}:{r.value}" for r in self.rules)


@dataclass
class MessageQuery:
    count: int = 50
    before_id: Optional[UUID] = None
    after_id: Optional[UUID] = None
    before_time: Optional[datetime] = None
    after_time: Optional[datetime] = None
    content: Optional[str] = None

    @property
    def ascending(self) -> bool:
        """Forward paging ("what is new since X") when only lower bounds are given."""
        return (self.after_id is not None or self.after_time is not None) \
            and self.before_id is None and self.before_time is None


@dataclass
class JoinResult:
    """What join_group changed: a new accepted row, or a pending invite settled."""
    settled: Optional[ChatRequest] = None   # the invite that became a membership (status "accepted")


# ---------------------------------------------------------------------- the interface

class Repository(ABC):
    # ---- users
    @abstractmethod
    def create_user(self, username: str, salt: bytes, iterations: int, stored_key: bytes,
                    server_key: bytes, created_ip: Optional[str], created_device: Optional[str],
                    limit: Optional[RegistrationLimit] = None) -> User:
        """Raises UsernameTaken or RateLimited. The limit check and insert are atomic.
        New users get DEFAULT_TEXT_COLOR / DEFAULT_TEXT_BG_COLOR and no session."""

    @abstractmethod
    def registration_retry_after(self, limit: RegistrationLimit) -> float:
        """Seconds until the limit allows another account (0 = allowed now)."""

    @abstractmethod
    def get_user(self, user_id: UUID) -> Optional[User]: ...

    @abstractmethod
    def get_user_by_username(self, username: str) -> Optional[User]:
        """Case-insensitive."""

    @abstractmethod
    def get_users(self, user_ids: Iterable[UUID]) -> Dict[UUID, User]: ...

    @abstractmethod
    def search_users(self, prefix: str, offset: int = 0, limit: int = 20) -> Tuple[List[User], bool]:
        """Users whose username starts with `prefix` (case-insensitive, literal: no
        wildcards; "" = everyone). An exact match comes first, then the rest by
        lower(username). Returns (page, has_more)."""

    @abstractmethod
    def update_profile(self, user_id: UUID, text_color: Optional[str],
                       text_bg_color: Optional[str], username: Optional[str] = None) -> Optional[User]:
        """Sets the given colours ("#rrggbb", already lowercase) and username; None keeps
        a value. One atomic update. The username follows the registration rules and
        stays unique without regard to case: renaming to your own name in another case
        is fine, a name another user has raises UsernameTaken. The SCRAM verifier does
        not depend on the name, so the password keeps working. Raises SameColors if the
        result would have equal colours (checked before the name's uniqueness). None if
        no such user."""

    # ---- sessions: one per account
    @abstractmethod
    def create_session(self, token_hash: bytes, user_id: UUID, expires_at: datetime,
                       expected_verifier: str) -> Optional[Tuple[Session, Optional[bytes]]]:
        """Makes this the user's only session, but only if the user's password_hash
        still equals `expected_verifier` (the one the login proof was checked
        against). Returns (session, the token hash it replaced or None), or None if
        the password changed in between. Atomic: two logins at once each report the
        hash they really replaced."""

    @abstractmethod
    def get_session(self, token_hash: bytes) -> Optional[Tuple[Session, User]]:
        """The session with this token hash and its user, in one read. Expired
        sessions are returned too (the caller decides); None if no user has it."""

    @abstractmethod
    def revoke_session(self, token_hash: bytes) -> bool:
        """Ends the session if it is still the user's current one. True if it was."""

    @abstractmethod
    def change_credentials(self, user_id: UUID, salt: bytes, iterations: int, stored_key: bytes,
                           server_key: bytes, expected_verifier: str, current_token_hash: bytes,
                           new_token_hash: bytes, expires_at: datetime) -> Optional[Session]:
        """In ONE transaction, only if the user's password_hash still equals
        `expected_verifier` AND their session is still `current_token_hash`: store
        the new verifier and replace the session token (rotation). Returns the new
        session, or None if either had changed."""

    # ---- chats
    @abstractmethod
    def create_group(self, name: str, description: str, owner_id: UUID,
                     chat_id: Optional[UUID] = None) -> Chat:
        """chats + groups + the owner's accepted membership, in one transaction (R8).
        `chat_id` lets the caller choose the id (to hold the chat's lock before it exists)."""

    @abstractmethod
    def get_chat(self, chat_id: UUID) -> Optional[Chat]:
        """A group, or a DM in any status."""

    @abstractmethod
    def find_dm(self, user_a: UUID, user_b: UUID) -> Optional[Chat]:
        """The DM of this pair (either direction, any status), if any."""

    @abstractmethod
    def create_dm(self, initiator: UUID, receiver: UUID) -> Tuple[Chat, bool]:
        """An ACCEPTED DM made at once (DM_REQUIRE_ACCEPT=false, the default). Returns (chat,
        created); if the pair already has a DM (any status) it is returned with
        created=False. Raises NotFoundError if a user is gone."""

    @abstractmethod
    def list_user_chats(self, user_id: UUID) -> List[ChatListItem]:
        """Accepted group memberships and accepted DMs, latest activity first."""

    @abstractmethod
    def get_chat_item(self, user_id: UUID, chat_id: UUID) -> Optional[ChatListItem]:
        """The list_user_chats() row of one chat, or None if the user is not in it."""

    @abstractmethod
    def list_groups(self, offset: int = 0, limit: int = 200) -> Tuple[List[GroupListItem], bool]:
        """Groups with at least one accepted member, oldest first, each with its
        accepted members, from one consistent snapshot. Returns (page, has_more)."""

    @abstractmethod
    def list_member_groups(self, user_id: UUID, viewer_id: Optional[UUID] = None) -> List[MemberGroup]:
        """Every group `user_id` is an accepted member of, by lower(group name) then
        chat_id ([] for an unknown user). viewer_is_member says whether `viewer_id`
        is an accepted member of each one too."""

    @abstractmethod
    def find_invitees(self, group_id: UUID, inviter_id: UUID, online_ids: Iterable[UUID],
                      query: str = "", limit: int = 50) -> List[User]:
        """People `inviter_id` could invite to group `group_id`: users in `online_ids`
        or with an ACCEPTED DM with the inviter, whose username contains `query`
        (case-insensitive, literal: no wildcards; "" = everyone), except the inviter
        and anyone with a membership row in the group (accepted members and pending
        invitees). By lower(username), at most `limit`."""

    @abstractmethod
    def get_members(self, chat_id: UUID) -> List[Member]:
        """Accepted members, by joined_at then user_id; the two people of an
        accepted DM; [] for a pending DM."""

    @abstractmethod
    def get_member(self, chat_id: UUID, user_id: UUID) -> Optional[Member]:
        """Only accepted members count (pending invitees and pending DMs do not)."""

    @abstractmethod
    def join_group(self, chat_id: UUID, user_id: UUID) -> Optional[JoinResult]:
        """R10, in one transaction under the chat's lock: a pending invite of the
        user becomes an accepted membership (JoinResult.settled = that invite), or
        a new accepted membership is made (settled = None). The read marker moves
        to the newest message. None if the user was already a member; raises
        NotFoundError if the group is gone."""

    @abstractmethod
    def remove_member(self, chat_id: UUID, user_id: UUID) -> Tuple[bool, bool]:
        """Leaves a group. Returns (removed, chat_deleted): the group is deleted with
        its last accepted member (pending invites and messages go with it)."""

    @abstractmethod
    def mark_read(self, chat_id: UUID, user_id: UUID, message_id: Optional[UUID]) -> UUID:
        """Moves the user's read marker forward to `message_id`, clamped to the
        chat's newest message (None = the newest). Never moves it back. Returns the
        marker (NO_MESSAGE if the chat has no messages or the user is not in it)."""

    # ---- messages
    @abstractmethod
    def add_message(self, chat_id: UUID, sender_id: Optional[UUID], content: str,
                    kind: str = "text") -> Message:
        """Raises NotFoundError if the chat is gone. See the id rule above."""

    @abstractmethod
    def list_messages(self, chat_id: UUID, query: MessageQuery) -> Tuple[List[Message], bool]:
        """Returns (messages oldest-first, has_more)."""

    @abstractmethod
    def first_message_after(self, chat_id: UUID, after: UUID) -> Optional[UUID]:
        """The id of the oldest message of the chat above `after`, or None."""

    # ---- DM requests and group invites
    @abstractmethod
    def create_request(self, kind: str, from_user: UUID, to_user: UUID,
                       chat_id: Optional[UUID] = None, message: Optional[str] = None) -> ChatRequest:
        """kind "invite": a pending membership of `to_user` in group `chat_id`,
        invited by `from_user`. Raises AlreadyMember if they are in it, AlreadyRequested
        if already invited, NotFoundError if the group or a user is gone.
        kind "dm": chats + a pending dms row + the first message (from `from_user`),
        all in one transaction. Raises AlreadyRequested if the pair already has a DM
        (any status, either direction; nothing is left behind), NotFoundError if a
        user is gone."""

    @abstractmethod
    def get_request(self, request_id: UUID) -> Optional[ChatRequest]:
        """A DM (by its chat id) or an invite (by membership_id), pending or accepted."""

    @abstractmethod
    def get_pending_invite(self, group_id: UUID, user_id: UUID) -> Optional[ChatRequest]:
        """The PENDING invite of `user_id` to group `group_id`, or None (no row, or
        already an accepted member)."""

    @abstractmethod
    def list_pending_requests(self, user_id: UUID) -> Tuple[List[ChatRequest], List[ChatRequest]]:
        """Returns (incoming, outgoing) pending DM requests and invites, oldest first."""

    @abstractmethod
    def list_pending_invites(self, chat_id: UUID, limit: int = 500) -> Tuple[List[ChatRequest], int]:
        """Pending invites of one group, oldest first: (first `limit`, total)."""

    @abstractmethod
    def resolve_request(self, request_id: UUID, status: str, actor_id: UUID) -> Optional[ChatRequest]:
        """Answers a PENDING request, in one transaction, checking who may:
        "accepted" / "declined" by its receiver, "cancelled" by its sender.
        accepted: a DM becomes an open room; an invite becomes an accepted
          membership (read marker to the newest message).
        declined / cancelled: the pending row is deleted (a DM request with its
          chat and first message).
        Returns the request with the new status and responded_at, or None if no
        such pending request exists for this actor (answered already, gone, not theirs)."""

    @abstractmethod
    def count_pending_requests(self, user_id: UUID) -> Tuple[int, int]:
        """Returns (incoming, outgoing) pending DM requests + invites of a user."""

    # ---- lifecycle
    def close(self) -> None:
        pass
