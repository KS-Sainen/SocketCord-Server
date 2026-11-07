"""PostgreSQL repository (psycopg 3 + connection pool) on the schema in schema.sql.

Every public method runs in its own transaction: `with self._tx() as conn`
commits on success and rolls back if anything raises. Methods that write go
through `_write(work)`: a deadlock or serialization failure (SQLSTATE 40P01,
40001, 40000) is retried once in a fresh transaction, then reported as
DatabaseUnavailable. `work` makes its ids (message ids) inside, so a retry gets
new ones.

Lock order (see base.py): the chats row first, in the strongest mode the
transaction will need (FOR UPDATE to delete the chat or a membership, FOR NO KEY
UPDATE to flip a status or insert a message), then dms / group_memberships rows,
then messages.
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple, TypeVar

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from .. import scram
from .base import (DEFAULT_TEXT_BG_COLOR, DEFAULT_TEXT_COLOR, NO_MESSAGE, AlreadyMember,
                   AlreadyRequested, Chat, ChatListItem, ChatRequest, DatabaseUnavailable,
                   GroupListItem, JoinResult, Member, MemberGroup, Message, MessageQuery, NotFoundError,
                   RateLimited, RegistrationLimit, Repository, RepositoryError, SameColors, Session,
                   User, UserRef, UsernameTaken, UUID, message_time, new_message_id)

log = logging.getLogger(__name__)

T = TypeVar("T")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_LOCK_ID = 7_350_001
PAIR_INDEX = "dms_one_per_pair"                      # one DM per pair of users
USERNAME_INDEX = "users_username_lower_key"          # unique names without regard to case
USERNAME_CHECK = "users_username_check"              # the username's CHECK (PostgreSQL's own name for it)
COLOR_RE = re.compile(r"#[0-9a-f]{6}")                # used with fullmatch()
NIL = "'00000000-0000-0000-0000-000000000000'::uuid"  # NO_MESSAGE: sorts before every message id

def _event_time():
    """The time of a chat, membership or request event, on the same clock as message
    times (the UUIDv7 clock of this process, not the database server's now()). Lists
    order these against each other (chats vs messages, DM requests vs invites), and
    the database may run in a VM whose clock is off by more than the gaps between them."""
    return message_time(new_message_id())


# The newest message id of chat %(c)s, or NIL. PostgreSQL has no max(uuid).
NEWEST = f"""coalesce((SELECT nm.message_id FROM messages nm WHERE nm.chat_id = %(c)s
                        ORDER BY nm.message_id DESC LIMIT 1), {NIL})"""


def _ref_cols(alias: str, prefix: str) -> str:
    """The UserRef columns of `users {alias}`, named for _ref(row, prefix)."""
    return ", ".join(f"{alias}.{col} AS {prefix}{col}"
                     for col in ("user_id", "username", "text_color", "text_bg_color"))


USER_COLS = "user_id, username, password_hash, text_color, text_bg_color, created_at"

# A Chat from `chats c` + its subtype row (needs CHAT_FROM). A DM has no name, an
# empty description, and its initiator as created_by; a group is always "accepted".
CHAT_COLS = """c.chat_id, c.kind, c.created_at, g.group_name AS name,
               coalesce(g.description, '') AS description,
               CASE WHEN c.kind = 'group' THEN g.created_by ELSE d.initiator END AS created_by,
               d.initiator, d.receiver, coalesce(d.status, 'accepted') AS status"""
CHAT_JOINS = """LEFT JOIN groups g ON g.group_id = c.chat_id
                LEFT JOIN dms d ON d.dm_id = c.chat_id"""
CHAT_FROM = f"chats c {CHAT_JOINS}"

# Members: accepted group memberships, and the two people of an accepted DM.
GROUP_MEMBER_SELECT = """
    SELECT gm.group_id AS chat_id, u.user_id, u.username, u.text_color, u.text_bg_color,
           CASE WHEN g.created_by = u.user_id THEN 'owner' ELSE 'member' END AS role,
           gm.joined_at, gm.last_read_message_id
      FROM group_memberships gm
      JOIN groups g ON g.group_id = gm.group_id
      JOIN users u ON u.user_id = gm.user_id
     WHERE gm.status = 'accepted'"""
DM_MEMBER_SELECT = """
    SELECT d.dm_id AS chat_id, u.user_id, u.username, u.text_color, u.text_bg_color,
           'member' AS role, d.accepted_at AS joined_at,
           CASE WHEN u.user_id = d.initiator THEN d.initiator_last_read
                ELSE d.receiver_last_read END AS last_read_message_id
      FROM dms d
      JOIN users u ON u.user_id IN (d.initiator, d.receiver)
     WHERE d.status = 'accepted'"""

MESSAGE_SELECT = f"""
    SELECT m.message_id, m.chat_id, m.kind, m.content, m.sent_at, {_ref_cols('su', 's_')}
      FROM messages m
      LEFT JOIN users su ON su.user_id = m.sender_id"""

# Requests. A DM request is a dms row (request_id = chat_id = dm_id; sender = the
# initiator, receiver = the receiver; message = the DM's first message). An invite
# is a group_memberships row (request_id = membership_id, chat_id = the group;
# sender = invited_by, receiver = the invitee). The caller adds the WHERE.
DM_REQUEST_SELECT = f"""
    SELECT d.dm_id AS request_id, 'dm'::text AS kind, d.dm_id AS chat_id, NULL::text AS chat_name,
           (SELECT fm.content FROM messages fm WHERE fm.chat_id = d.dm_id
             ORDER BY fm.message_id LIMIT 1) AS message,
           d.status::text AS status, c.created_at, d.accepted_at AS responded_at,
           {_ref_cols('su', 's_')}, {_ref_cols('ru', 'r_')}
      FROM dms d
      JOIN chats c ON c.chat_id = d.dm_id
      JOIN users ru ON ru.user_id = d.receiver
      LEFT JOIN users su ON su.user_id = d.initiator"""
INVITE_REQUEST_SELECT = f"""
    SELECT gm.membership_id AS request_id, 'invite'::text AS kind, gm.group_id AS chat_id,
           g.group_name::text AS chat_name, NULL::text AS message, gm.status::text AS status,
           gm.created_at, gm.joined_at AS responded_at,
           {_ref_cols('su', 's_')}, {_ref_cols('ru', 'r_')}
      FROM group_memberships gm
      JOIN groups g ON g.group_id = gm.group_id
      JOIN users ru ON ru.user_id = gm.user_id
      LEFT JOIN users su ON su.user_id = gm.invited_by"""


def _chat_items_sql(one_chat: bool) -> str:
    """list_user_chats() rows of user %(u)s (only chat %(c)s if one_chat)."""
    group_filter = "AND gm.group_id = %(c)s" if one_chat else ""
    dm_filter = "AND d.dm_id = %(c)s" if one_chat else ""
    return f"""
        WITH mine AS (
            SELECT gm.group_id AS chat_id,
                   CASE WHEN g.created_by = gm.user_id THEN 'owner' ELSE 'member' END AS role,
                   gm.last_read_message_id AS marker,
                   (SELECT count(*) FROM group_memberships am
                     WHERE am.group_id = gm.group_id AND am.status = 'accepted') AS member_count,
                   NULL::uuid AS peer_id
              FROM group_memberships gm
              JOIN groups g ON g.group_id = gm.group_id
             WHERE gm.user_id = %(u)s AND gm.status = 'accepted' {group_filter}
            UNION ALL
            SELECT d.dm_id, 'member',
                   CASE WHEN d.initiator = %(u)s THEN d.initiator_last_read ELSE d.receiver_last_read END,
                   2,
                   CASE WHEN d.initiator = %(u)s THEN d.receiver ELSE d.initiator END
              FROM dms d
             WHERE d.status = 'accepted' AND %(u)s IN (d.initiator, d.receiver) {dm_filter})
        SELECT {CHAT_COLS}, mine.role, mine.marker, mine.member_count,
               (SELECT count(*) FROM messages um
                 WHERE um.chat_id = mine.chat_id AND um.message_id > mine.marker
                   AND um.sender_id IS DISTINCT FROM %(u)s) AS unread,
               lm.message_id AS lm_message_id, lm.chat_id AS lm_chat_id, lm.kind AS lm_kind,
               lm.content AS lm_content, lm.sent_at AS lm_sent_at, {_ref_cols('lu', 'lm_s_')},
               {_ref_cols('pu', 'peer_')}
          FROM mine
          JOIN chats c ON c.chat_id = mine.chat_id
          {CHAT_JOINS}
          LEFT JOIN LATERAL (SELECT m.message_id, m.chat_id, m.sender_id, m.kind, m.content, m.sent_at
                               FROM messages m WHERE m.chat_id = mine.chat_id
                              ORDER BY m.message_id DESC LIMIT 1) lm ON true
          LEFT JOIN users lu ON lu.user_id = lm.sender_id
          LEFT JOIN users pu ON pu.user_id = mine.peer_id
         ORDER BY coalesce(lm.sent_at, c.created_at) DESC, lm.message_id DESC NULLS LAST, c.chat_id DESC"""


# ---------------------------------------------------------------------- row -> record

def _ref(r: dict, prefix: str) -> Optional[UserRef]:
    if r[prefix + "user_id"] is None:
        return None
    return UserRef(user_id=r[prefix + "user_id"], username=r[prefix + "username"],
                   text_color=r[prefix + "text_color"], text_bg_color=r[prefix + "text_bg_color"])


def _user(r: dict) -> User:
    return User(user_id=r["user_id"], username=r["username"], password_hash=r["password_hash"],
                text_color=r["text_color"], text_bg_color=r["text_bg_color"], created_at=r["created_at"])


def _session(r: dict) -> Session:
    return Session(token_hash=bytes(r["session_token_hash"]), user_id=r["user_id"],
                   expires_at=r["session_expires_at"])


def _chat(r: dict) -> Chat:
    return Chat(chat_id=r["chat_id"], kind=r["kind"], name=r["name"], description=r["description"],
                created_by=r["created_by"], created_at=r["created_at"], initiator=r["initiator"],
                receiver=r["receiver"], status=r["status"])


def _member(r: dict) -> Member:
    return Member(chat_id=r["chat_id"], user_id=r["user_id"], username=r["username"],
                  text_color=r["text_color"], text_bg_color=r["text_bg_color"], role=r["role"],
                  joined_at=r["joined_at"], last_read_message_id=r["last_read_message_id"])


def _message(r: dict, prefix: str = "") -> Message:
    return Message(message_id=r[prefix + "message_id"], chat_id=r[prefix + "chat_id"],
                   sender=_ref(r, prefix + "s_"), kind=r[prefix + "kind"], content=r[prefix + "content"],
                   created_at=r[prefix + "sent_at"])


def _request(r: dict) -> ChatRequest:
    return ChatRequest(request_id=r["request_id"], kind=r["kind"], sender=_ref(r, "s_"),
                       receiver=_ref(r, "r_"), chat_id=r["chat_id"], chat_name=r["chat_name"],
                       message=r["message"], status=r["status"], created_at=r["created_at"],
                       responded_at=r["responded_at"])


def _chat_item(r: dict) -> ChatListItem:
    last = _message(r, "lm_") if r["lm_message_id"] is not None else None
    return ChatListItem(chat=_chat(r), role=r["role"], last_read_message_id=r["marker"],
                        unread=r["unread"], member_count=r["member_count"], last_message=last,
                        peer=_ref(r, "peer_"))


def _fetch_request(conn: psycopg.Connection, kind: str, request_id: UUID) -> Optional[ChatRequest]:
    if kind == "dm":
        row = conn.execute(f"{DM_REQUEST_SELECT} WHERE d.dm_id = %s", (request_id,)).fetchone()
    else:
        row = conn.execute(f"{INVITE_REQUEST_SELECT} WHERE gm.membership_id = %s", (request_id,)).fetchone()
    return _request(row) if row else None


class _PairTaken(Exception):
    """create_dm lost the race for the pair's one DM (rolled back; read the winner)."""


# Every statement gets at most 5 s (and at most 5 s waiting for a lock), so one
# expensive or stuck query cannot hold a worker thread for long.
SESSION_OPTIONS = "-c TimeZone=UTC -c statement_timeout=5000 -c lock_timeout=5000"

# SQLSTATE class 40 errors that _write retries once. psycopg's error classes are
# flat: DeadlockDetected (40P01) and SerializationFailure (40001) are NOT
# subclasses of TransactionRollback (40000), so all three are named.
ROLLED_BACK = (pg_errors.TransactionRollback, pg_errors.SerializationFailure, pg_errors.DeadlockDetected)


class PostgresRepository(Repository):
    def __init__(self, url: str, max_size: int = 10, open_timeout: float = 30.0) -> None:
        deadline = time.monotonic() + open_timeout
        while True:
            # A pool that timed out while opening is closed for good, so each attempt
            # builds a new one.
            pool = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=max_size,
                open=False,
                name="socketcord",
                kwargs={"row_factory": dict_row, "options": SESSION_OPTIONS, "connect_timeout": 5},
                check=ConnectionPool.check_connection,
            )
            try:
                pool.open(wait=True, timeout=min(5.0, max(0.5, deadline - time.monotonic())))
                break
            except (PoolTimeout, PoolClosed):
                pool.close()
                if time.monotonic() >= deadline:
                    raise DatabaseUnavailable(f"could not connect to PostgreSQL within {open_timeout:.0f}s")
                log.warning("PostgreSQL not reachable yet, retrying...")
        self.pool = pool
        try:
            self.apply_schema()
        except BaseException:
            pool.close()
            raise

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def _tx(self, retrying: bool = False) -> Iterator[psycopg.Connection]:
        """One transaction. Connection trouble, timeouts and (unless `retrying`, for
        _write) deadlocks / serialization failures become DatabaseUnavailable."""
        try:
            with self.pool.connection(timeout=10) as conn:
                yield conn
        except ROLLED_BACK as exc:
            if retrying:
                raise
            raise DatabaseUnavailable(str(exc)) from exc
        except (psycopg.OperationalError, PoolTimeout, PoolClosed) as exc:
            raise DatabaseUnavailable(str(exc)) from exc

    def _write(self, work: Callable[[psycopg.Connection], T]) -> T:
        """Runs work(conn) in a transaction; a deadlock (40P01) or serialization
        failure (40001) is retried once in a fresh transaction, then reported as
        DatabaseUnavailable."""
        try:
            with self._tx(retrying=True) as conn:
                return work(conn)
        except ROLLED_BACK as exc:
            log.warning("transaction rolled back (%s), retrying once: %s", exc.sqlstate, exc)
        with self._tx() as conn:
            return work(conn)

    def apply_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        try:
            with self._tx() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
                conn.execute(sql)
        except pg_errors.RaiseException as exc:
            # schema.sql refuses a database that still has the old schema; its HINT
            # says how to reset it.
            message = exc.diag.message_primary or str(exc)
            hint = exc.diag.message_hint
            raise DatabaseUnavailable(f"{message}. {hint}" if hint else message) from None
        log.info("database schema is up to date")

    # ------------------------------------------------------------------ users
    @staticmethod
    def _retry_after(conn: psycopg.Connection, limit: RegistrationLimit) -> float:
        worst = 0.0
        for rule in limit.rules:
            # "<<=" = inside or equal: the IP rule's value is an IPv4 address or an IPv6 /64.
            column = "created_ip <<= %(value)s::inet" if rule.kind == "ip" else "created_device = %(value)s"
            rows = conn.execute(
                f"""SELECT now() AS now, created_at FROM users
                     WHERE created_at > now() - %(window)s * interval '1 second' AND {column}
                     ORDER BY created_at DESC
                     LIMIT %(max)s""",
                {"window": limit.window_seconds, "value": rule.value, "max": rule.max_count},
            ).fetchall()
            if len(rows) >= rule.max_count:
                free_at = rows[-1]["created_at"] + timedelta(seconds=limit.window_seconds)
                worst = max(worst, (free_at - rows[-1]["now"]).total_seconds())
        return max(0.0, worst)

    def registration_retry_after(self, limit: RegistrationLimit) -> float:
        with self._tx() as conn:
            return self._retry_after(conn, limit)

    def create_user(self, username, salt, iterations, stored_key, server_key,
                    created_ip, created_device, limit=None) -> User:
        password_hash = scram.encode_verifier(salt, iterations, stored_key, server_key)

        def work(conn: psycopg.Connection) -> User:
            if limit is not None and limit.rules:
                # Serialize registrations from the same IP/device so two parallel
                # requests cannot both slip under the limit.
                for key in limit.keys:
                    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 42))", (key,))
                retry = self._retry_after(conn, limit)
                if retry > 0:
                    raise RateLimited(retry)
            try:
                row = conn.execute(
                    f"""INSERT INTO users (username, password_hash, text_color, text_bg_color,
                                           created_ip, created_device)
                        VALUES (%s, %s, %s, %s, %s::inet, %s)
                        RETURNING {USER_COLS}""",
                    (username, password_hash, DEFAULT_TEXT_COLOR, DEFAULT_TEXT_BG_COLOR,
                     created_ip, created_device),
                ).fetchone()
            except pg_errors.UniqueViolation:
                raise UsernameTaken(username) from None
            return _user(row)

        return self._write(work)

    def get_user(self, user_id):
        with self._tx() as conn:
            row = conn.execute(f"SELECT {USER_COLS} FROM users WHERE user_id = %s", (user_id,)).fetchone()
        return _user(row) if row else None

    def get_user_by_username(self, username):
        with self._tx() as conn:
            row = conn.execute(f"SELECT {USER_COLS} FROM users WHERE lower(username) = lower(%s::text)",
                               (username.strip(),)).fetchone()
        return _user(row) if row else None

    def get_users(self, user_ids: Iterable[UUID]) -> Dict[UUID, User]:
        ids = list(set(user_ids))
        if not ids:
            return {}
        with self._tx() as conn:
            rows = conn.execute(f"SELECT {USER_COLS} FROM users WHERE user_id = ANY(%s::uuid[])",
                                (ids,)).fetchall()
        return {r["user_id"]: _user(r) for r in rows}

    def search_users(self, prefix, offset=0, limit=20) -> Tuple[List[User], bool]:
        # ^@ is "starts with": a literal prefix ("_" is a legal username character,
        # so LIKE would be wrong). COLLATE "C" keeps the order the same on every server.
        with self._tx() as conn:
            rows = conn.execute(
                f"""SELECT {USER_COLS} FROM users
                     WHERE lower(username) ^@ lower(%(p)s::text)
                     ORDER BY lower(username) = lower(%(p)s::text) DESC, lower(username) COLLATE "C"
                     LIMIT %(n)s OFFSET %(o)s""",
                {"p": prefix, "n": max(0, limit) + 1, "o": max(0, offset)},
            ).fetchall()
        return [_user(r) for r in rows[:max(0, limit)]], len(rows) > max(0, limit)

    def update_profile(self, user_id, text_color, text_bg_color, username=None):
        def work(conn: psycopg.Connection) -> Optional[User]:
            try:
                row = conn.execute(
                    f"""UPDATE users SET username = coalesce(%s, username),
                                         text_color = coalesce(%s, text_color),
                                         text_bg_color = coalesce(%s, text_bg_color)
                         WHERE user_id = %s
                     RETURNING {USER_COLS}""",
                    (username, text_color, text_bg_color, user_id),
                ).fetchone()
            except pg_errors.CheckViolation as exc:
                if exc.diag.constraint_name == USERNAME_CHECK:
                    raise ValueError("username must be 3..32 of A-Z a-z 0-9 _ . -") from None
                # With well-formed colours the only CHECK left is text_color <> text_bg_color.
                if all(c is None or COLOR_RE.fullmatch(c) for c in (text_color, text_bg_color)):
                    raise SameColors("the text and background colours would be the same") from None
                raise ValueError("colours must be '#rrggbb' in lowercase") from None
            except pg_errors.StringDataRightTruncation:
                raise ValueError("a username is at most 32 and a colour 7 characters") from None
            except pg_errors.UniqueViolation as exc:
                if exc.diag.constraint_name != USERNAME_INDEX:
                    raise
                raise UsernameTaken(username) from None
            return _user(row) if row else None

        return self._write(work)

    # ------------------------------------------------------------------ sessions
    def create_session(self, token_hash, user_id, expires_at, expected_verifier):
        def work(conn: psycopg.Connection) -> Optional[Tuple[Session, Optional[bytes]]]:
            # FOR UPDATE (the mode the UPDATE needs anyway) both blocks a concurrent
            # password change and makes two logins at once queue up, so each reads
            # the hash it really replaces.
            old = conn.execute(
                """SELECT session_token_hash FROM users
                    WHERE user_id = %s AND password_hash = %s
                      FOR UPDATE""",
                (user_id, expected_verifier),
            ).fetchone()
            if old is None:
                return None
            row = conn.execute(
                """UPDATE users SET session_token_hash = %s, session_expires_at = %s
                    WHERE user_id = %s
                RETURNING user_id, session_token_hash, session_expires_at""",
                (token_hash, expires_at, user_id),
            ).fetchone()
            replaced = old["session_token_hash"]
            return _session(row), (bytes(replaced) if replaced is not None else None)

        return self._write(work)

    def get_session(self, token_hash):
        with self._tx() as conn:
            row = conn.execute(
                f"""SELECT {USER_COLS}, session_token_hash, session_expires_at
                      FROM users WHERE session_token_hash = %s""",
                (token_hash,),
            ).fetchone()
        return (_session(row), _user(row)) if row else None

    def revoke_session(self, token_hash) -> bool:
        def work(conn: psycopg.Connection) -> bool:
            cur = conn.execute(
                """UPDATE users SET session_token_hash = NULL, session_expires_at = NULL
                    WHERE session_token_hash = %s""",
                (token_hash,),
            )
            return cur.rowcount == 1

        return self._write(work)

    def change_credentials(self, user_id, salt, iterations, stored_key, server_key, expected_verifier,
                           current_token_hash, new_token_hash, expires_at):
        password_hash = scram.encode_verifier(salt, iterations, stored_key, server_key)

        def work(conn: psycopg.Connection) -> Optional[Session]:
            still_valid = conn.execute(
                """SELECT session_token_hash FROM users
                    WHERE user_id = %s AND password_hash = %s AND session_token_hash = %s
                      FOR UPDATE""",
                (user_id, expected_verifier, current_token_hash),
            ).fetchone()
            if still_valid is None:
                return None
            row = conn.execute(
                """UPDATE users SET password_hash = %s, session_token_hash = %s, session_expires_at = %s
                    WHERE user_id = %s
                RETURNING user_id, session_token_hash, session_expires_at""",
                (password_hash, new_token_hash, expires_at, user_id),
            ).fetchone()
            return _session(row)

        return self._write(work)

    # ------------------------------------------------------------------ chats
    def create_group(self, name, description, owner_id, chat_id=None) -> Chat:
        def work(conn: psycopg.Connection) -> Chat:
            chat = conn.execute("INSERT INTO chats (chat_id, kind, created_at) "
                                "VALUES (coalesce(%s, gen_random_uuid()), 'group', %s) RETURNING chat_id",
                                (chat_id, _event_time())).fetchone()
            try:
                conn.execute(
                    """INSERT INTO groups (group_id, group_name, description, created_by)
                       VALUES (%s, %s, %s, %s)""",
                    (chat["chat_id"], name, description, owner_id))
                conn.execute(
                    """INSERT INTO group_memberships (user_id, group_id, status, created_at, joined_at)
                       VALUES (%s, %s, 'accepted', %s, %s)""",
                    (owner_id, chat["chat_id"], *([_event_time()] * 2)))
            except pg_errors.ForeignKeyViolation:
                raise NotFoundError("user") from None
            return self._chat_in(conn, chat["chat_id"])

        return self._write(work)

    @staticmethod
    def _chat_in(conn: psycopg.Connection, chat_id: UUID) -> Optional[Chat]:
        row = conn.execute(f"SELECT {CHAT_COLS} FROM {CHAT_FROM} WHERE c.chat_id = %s", (chat_id,)).fetchone()
        return _chat(row) if row else None

    def get_chat(self, chat_id):
        with self._tx() as conn:
            return self._chat_in(conn, chat_id)

    def find_dm(self, user_a, user_b):
        with self._tx() as conn:
            row = conn.execute(
                f"""SELECT {CHAT_COLS} FROM {CHAT_FROM}
                     WHERE c.kind = 'dm' AND d.initiator IS NOT NULL AND d.receiver IS NOT NULL
                       AND LEAST(d.initiator, d.receiver) = LEAST(%(a)s::uuid, %(b)s::uuid)
                       AND GREATEST(d.initiator, d.receiver) = GREATEST(%(a)s::uuid, %(b)s::uuid)""",
                {"a": user_a, "b": user_b},
            ).fetchone()
        return _chat(row) if row else None

    def create_dm(self, initiator, receiver) -> Tuple[Chat, bool]:
        if initiator == receiver:
            raise ValueError("a DM needs two different users")

        def work(conn: psycopg.Connection) -> Chat:
            dm_id = conn.execute("INSERT INTO chats (kind, created_at) VALUES ('dm', %s) RETURNING chat_id",
                                 (_event_time(),)).fetchone()["chat_id"]
            try:
                conn.execute(
                    """INSERT INTO dms (dm_id, initiator, receiver, status, accepted_at)
                       VALUES (%s, %s, %s, 'accepted', %s)""",
                    (dm_id, initiator, receiver, _event_time()))
            except pg_errors.UniqueViolation as exc:
                if exc.diag.constraint_name != PAIR_INDEX:
                    raise
                raise _PairTaken() from None
            except pg_errors.ForeignKeyViolation:
                raise NotFoundError("user") from None
            return self._chat_in(conn, dm_id)

        for _ in range(3):
            try:
                return self._write(work), True
            except _PairTaken:
                pass
            # The pair already has a DM (the failed transaction rolled back, chats row
            # included). Read it in a new transaction: the old one could not see it.
            existing = self.find_dm(initiator, receiver)
            if existing is not None:
                return existing, False
        raise DatabaseUnavailable("the DM of this pair kept appearing and disappearing; try again")

    def list_user_chats(self, user_id) -> List[ChatListItem]:
        with self._tx() as conn:
            rows = conn.execute(_chat_items_sql(one_chat=False), {"u": user_id}).fetchall()
        return [_chat_item(r) for r in rows]

    def get_chat_item(self, user_id, chat_id) -> Optional[ChatListItem]:
        with self._tx() as conn:
            row = conn.execute(_chat_items_sql(one_chat=True), {"u": user_id, "c": chat_id}).fetchone()
        return _chat_item(row) if row else None

    def list_groups(self, offset=0, limit=200) -> Tuple[List[GroupListItem], bool]:
        limit, offset = max(0, limit), max(0, offset)
        with self._tx() as conn:
            # Both SELECTs must see the same snapshot, or a group deleted between
            # them would be listed with no members.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            chats = conn.execute(
                f"""SELECT {CHAT_COLS} FROM {CHAT_FROM}
                     WHERE c.kind = 'group'
                       AND EXISTS (SELECT 1 FROM group_memberships m
                                    WHERE m.group_id = c.chat_id AND m.status = 'accepted')
                     ORDER BY c.created_at, c.chat_id
                     LIMIT %s OFFSET %s""",
                (limit + 1, offset),
            ).fetchall()
            has_more = len(chats) > limit
            chats = chats[:limit]
            ids = [r["chat_id"] for r in chats]
            members = conn.execute(
                f"""{GROUP_MEMBER_SELECT} AND gm.group_id = ANY(%s::uuid[])
                     ORDER BY gm.joined_at, gm.user_id""",
                (ids,),
            ).fetchall() if ids else []
        by_chat: Dict[UUID, List[Member]] = {}
        for r in members:
            by_chat.setdefault(r["chat_id"], []).append(_member(r))
        return [GroupListItem(chat=_chat(r), members=by_chat.get(r["chat_id"], [])) for r in chats], has_more

    def list_member_groups(self, user_id, viewer_id=None) -> List[MemberGroup]:
        with self._tx() as conn:
            rows = conn.execute(
                f"""SELECT {CHAT_COLS},
                           CASE WHEN g.created_by = gm.user_id THEN 'owner' ELSE 'member' END AS role,
                           gm.joined_at,
                           (SELECT count(*) FROM group_memberships am
                             WHERE am.group_id = gm.group_id AND am.status = 'accepted') AS member_count,
                           EXISTS (SELECT 1 FROM group_memberships vm
                                    WHERE vm.group_id = gm.group_id AND vm.user_id = %(v)s::uuid
                                      AND vm.status = 'accepted') AS viewer_is_member
                      FROM group_memberships gm
                      JOIN chats c ON c.chat_id = gm.group_id
                      {CHAT_JOINS}
                     WHERE gm.user_id = %(u)s AND gm.status = 'accepted'
                     ORDER BY lower(g.group_name) COLLATE "C", c.chat_id""",
                {"u": user_id, "v": viewer_id},
            ).fetchall()
        return [MemberGroup(chat=_chat(r), role=r["role"], joined_at=r["joined_at"],
                            member_count=r["member_count"], viewer_is_member=r["viewer_is_member"]) for r in rows]

    def find_invitees(self, group_id, inviter_id, online_ids, query="", limit=50) -> List[User]:
        # strpos is a plain substring search ("_" and "%" are ordinary characters here).
        with self._tx() as conn:
            rows = conn.execute(
                f"""SELECT {USER_COLS} FROM users u
                     WHERE u.user_id <> %(me)s
                       AND (u.user_id = ANY(%(online)s::uuid[])
                            OR EXISTS (SELECT 1 FROM dms d
                                        WHERE d.status = 'accepted'
                                          AND ((d.initiator = %(me)s AND d.receiver = u.user_id)
                                               OR (d.receiver = %(me)s AND d.initiator = u.user_id))))
                       AND NOT EXISTS (SELECT 1 FROM group_memberships gm
                                        WHERE gm.group_id = %(g)s AND gm.user_id = u.user_id)
                       AND strpos(lower(u.username), lower(%(q)s::text)) > 0
                     ORDER BY lower(u.username) COLLATE "C"
                     LIMIT %(n)s""",
                {"me": inviter_id, "online": list(set(online_ids)), "g": group_id, "q": query,
                 "n": max(0, limit)},
            ).fetchall()
        return [_user(r) for r in rows]

    def get_members(self, chat_id) -> List[Member]:
        with self._tx() as conn:
            rows = conn.execute(
                f"""{GROUP_MEMBER_SELECT} AND gm.group_id = %(c)s
                    UNION ALL
                    {DM_MEMBER_SELECT} AND d.dm_id = %(c)s
                    ORDER BY joined_at, user_id""",
                {"c": chat_id},
            ).fetchall()
        return [_member(r) for r in rows]

    def get_member(self, chat_id, user_id):
        with self._tx() as conn:
            row = conn.execute(
                f"""{GROUP_MEMBER_SELECT} AND gm.group_id = %(c)s AND gm.user_id = %(u)s
                    UNION ALL
                    {DM_MEMBER_SELECT} AND d.dm_id = %(c)s AND u.user_id = %(u)s""",
                {"c": chat_id, "u": user_id},
            ).fetchone()
        return _member(row) if row else None

    def join_group(self, chat_id, user_id) -> Optional[JoinResult]:
        def work(conn: psycopg.Connection) -> Optional[JoinResult]:
            # The chat row first (a status flip / insert, so NO KEY UPDATE): a join
            # and the last member leaving cannot miss each other.
            if conn.execute("SELECT 1 FROM chats WHERE chat_id = %s FOR NO KEY UPDATE",
                            (chat_id,)).fetchone() is None:
                raise NotFoundError("group")
            try:
                row = conn.execute(
                    f"""INSERT INTO group_memberships AS gm
                               (user_id, group_id, status, created_at, joined_at, last_read_message_id)
                        VALUES (%(u)s, %(c)s, 'accepted', %(t)s, %(t)s, {NEWEST})
                        ON CONFLICT (user_id, group_id) DO UPDATE
                           SET status = 'accepted', joined_at = %(t)s,
                               last_read_message_id = EXCLUDED.last_read_message_id
                         WHERE gm.status = 'pending'
                     RETURNING (gm.xmax = 0) AS inserted, gm.membership_id""",
                    {"u": user_id, "c": chat_id, "t": _event_time()},
                ).fetchone()
            except pg_errors.ForeignKeyViolation:
                raise NotFoundError("group") from None   # not a group (any more), or no such user
            if row is None:
                return None                               # already an accepted member
            if row["inserted"]:
                return JoinResult(settled=None)
            return JoinResult(settled=_fetch_request(conn, "invite", row["membership_id"]))

        return self._write(work)

    def remove_member(self, chat_id, user_id) -> Tuple[bool, bool]:
        def work(conn: psycopg.Connection) -> Tuple[bool, bool]:
            # FOR UPDATE: this may delete the chat (the trigger drops a group with its
            # last accepted member), so two last members leaving at once queue up
            # here and the second one sees the first one's delete.
            if conn.execute("SELECT 1 FROM chats WHERE chat_id = %s FOR UPDATE",
                            (chat_id,)).fetchone() is None:
                return False, False
            cur = conn.execute(
                """DELETE FROM group_memberships
                    WHERE group_id = %s AND user_id = %s AND status = 'accepted'""",
                (chat_id, user_id))
            if cur.rowcount != 1:
                return False, False
            gone = conn.execute("SELECT 1 FROM chats WHERE chat_id = %s", (chat_id,)).fetchone() is None
            return True, gone

        return self._write(work)

    def mark_read(self, chat_id, user_id, message_id) -> UUID:
        # Forward only, to a real message of THIS chat: the newest one at or below
        # `message_id` (None = the newest), so an id from elsewhere or from the future
        # cannot set a bogus marker. The chat is a group or a DM; at most one UPDATE matches.
        target = "GREATEST({col}, latest.id)"
        upto = f"""coalesce((SELECT um.message_id FROM messages um
                              WHERE um.chat_id = %(c)s AND (%(m)s::uuid IS NULL OR um.message_id <= %(m)s::uuid)
                              ORDER BY um.message_id DESC LIMIT 1), {NIL})"""

        def work(conn: psycopg.Connection) -> UUID:
            row = conn.execute(
                f"""WITH latest AS (SELECT {upto} AS id),
                    g AS (UPDATE group_memberships gm
                             SET last_read_message_id = {target.format(col="gm.last_read_message_id")}
                            FROM latest
                           WHERE gm.group_id = %(c)s AND gm.user_id = %(u)s AND gm.status = 'accepted'
                       RETURNING gm.last_read_message_id AS marker),
                    d AS (UPDATE dms
                             SET initiator_last_read = CASE WHEN initiator = %(u)s
                                     THEN {target.format(col="initiator_last_read")}
                                     ELSE initiator_last_read END,
                                 receiver_last_read = CASE WHEN receiver = %(u)s
                                     THEN {target.format(col="receiver_last_read")}
                                     ELSE receiver_last_read END
                            FROM latest
                           WHERE dm_id = %(c)s AND status = 'accepted' AND %(u)s IN (initiator, receiver)
                       RETURNING CASE WHEN initiator = %(u)s THEN initiator_last_read
                                      ELSE receiver_last_read END AS marker)
                    SELECT marker FROM g UNION ALL SELECT marker FROM d""",
                {"c": chat_id, "u": user_id, "m": message_id},
            ).fetchone()
            return row["marker"] if row else NO_MESSAGE

        return self._write(work)

    # ------------------------------------------------------------------ messages
    def add_message(self, chat_id, sender_id, content, kind="text") -> Message:
        def work(conn: psycopg.Connection) -> Message:
            # One writer per chat at a time, and the id is made while holding the
            # lock, above the chat's newest id: within a chat, ids commit in order,
            # so a larger id is never visible before a smaller one.
            if conn.execute("SELECT 1 FROM chats WHERE chat_id = %s FOR NO KEY UPDATE",
                            (chat_id,)).fetchone() is None:
                raise NotFoundError("chat")
            newest = conn.execute(
                "SELECT message_id FROM messages WHERE chat_id = %s ORDER BY message_id DESC LIMIT 1",
                (chat_id,)).fetchone()
            message_id = new_message_id(after=newest["message_id"] if newest else None)
            try:
                row = conn.execute(
                    f"""WITH m AS (
                            INSERT INTO messages (message_id, chat_id, sender_id, kind, content)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING message_id, chat_id, sender_id, kind, content, sent_at)
                        SELECT m.message_id, m.chat_id, m.kind, m.content, m.sent_at, {_ref_cols('su', 's_')}
                          FROM m LEFT JOIN users su ON su.user_id = m.sender_id""",
                    (message_id, chat_id, sender_id, kind, content),
                ).fetchone()
            except pg_errors.ForeignKeyViolation:
                raise NotFoundError("sender") from None
            return _message(row)

        return self._write(work)

    def list_messages(self, chat_id, query: MessageQuery) -> Tuple[List[Message], bool]:
        conds = ["m.chat_id = %s"]
        params: list = [chat_id]
        if query.before_id is not None:
            conds.append("m.message_id < %s")
            params.append(query.before_id)
        if query.after_id is not None:
            conds.append("m.message_id > %s")
            params.append(query.after_id)
        if query.before_time is not None:
            conds.append("m.sent_at < %s")
            params.append(query.before_time)
        if query.after_time is not None:
            conds.append("m.sent_at > %s")
            params.append(query.after_time)
        if query.content:
            # strpos is a plain substring search (no wildcards, no backtracking).
            conds.append("strpos(lower(m.content), lower(%s::text)) > 0")
            params.append(query.content)
        count = max(0, query.count)
        order = "ASC" if query.ascending else "DESC"
        params.append(count + 1)
        sql = f"{MESSAGE_SELECT} WHERE {' AND '.join(conds)} ORDER BY m.message_id {order} LIMIT %s"
        with self._tx() as conn:
            rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > count
        rows = rows[:count]
        if not query.ascending:
            rows.reverse()
        return [_message(r) for r in rows], has_more

    def first_message_after(self, chat_id, after) -> Optional[UUID]:
        with self._tx() as conn:
            row = conn.execute(
                """SELECT message_id FROM messages WHERE chat_id = %s AND message_id > %s
                    ORDER BY message_id LIMIT 1""",
                (chat_id, after)).fetchone()
        return row["message_id"] if row else None

    # ------------------------------------------------------------------ requests
    def create_request(self, kind, from_user, to_user, chat_id=None, message=None) -> ChatRequest:
        if kind == "dm":
            if from_user == to_user:
                raise ValueError("a DM needs two different users")
            return self._write(lambda conn: self._create_dm_request(conn, from_user, to_user, message))
        if kind == "invite":
            if chat_id is None:
                raise ValueError("an invite needs the group's chat_id")
            return self._write(lambda conn: self._create_invite(conn, from_user, to_user, chat_id))
        raise ValueError(f"unknown request kind {kind!r}")

    @staticmethod
    def _create_dm_request(conn: psycopg.Connection, from_user: UUID, to_user: UUID,
                           message: Optional[str]) -> ChatRequest:
        # Plain INSERTs in one transaction: if the pair already has a DM, the
        # unique index fails the dms INSERT and the whole transaction (chats row
        # included) rolls back. (ON CONFLICT DO NOTHING would commit an orphan chat.)
        dm_id = conn.execute("INSERT INTO chats (kind, created_at) VALUES ('dm', %s) RETURNING chat_id",
                             (_event_time(),)).fetchone()["chat_id"]
        try:
            conn.execute("INSERT INTO dms (dm_id, initiator, receiver, status) VALUES (%s, %s, %s, 'pending')",
                         (dm_id, from_user, to_user))
            if message:
                first_id = new_message_id()
                conn.execute(
                    """INSERT INTO messages (message_id, chat_id, sender_id, kind, content)
                       VALUES (%s, %s, %s, 'text', %s)""",
                    (first_id, dm_id, from_user, message))
                # Your own message counts as read (as post_message does for every message).
                conn.execute("UPDATE dms SET initiator_last_read = %s WHERE dm_id = %s", (first_id, dm_id))
        except pg_errors.UniqueViolation as exc:
            if exc.diag.constraint_name != PAIR_INDEX:
                raise
            raise AlreadyRequested() from None
        except pg_errors.ForeignKeyViolation:
            raise NotFoundError("user") from None
        return _fetch_request(conn, "dm", dm_id)

    @staticmethod
    def _create_invite(conn: psycopg.Connection, from_user: UUID, to_user: UUID,
                       group_id: UUID) -> ChatRequest:
        def existing() -> Optional[str]:
            row = conn.execute("SELECT status FROM group_memberships WHERE user_id = %s AND group_id = %s",
                               (to_user, group_id)).fetchone()
            return row["status"] if row else None

        def refuse(status: str) -> RepositoryError:
            return AlreadyMember() if status == "accepted" else AlreadyRequested()

        if from_user == to_user:   # the CHECK would fire before the conflict check
            status = existing()
            if status is not None:
                raise refuse(status)
            raise ValueError("a user cannot invite themselves")
        for _ in range(3):
            try:
                row = conn.execute(
                    """INSERT INTO group_memberships (user_id, group_id, status, invited_by, created_at)
                       VALUES (%s, %s, 'pending', %s, %s)
                       ON CONFLICT (user_id, group_id) DO NOTHING
                       RETURNING membership_id""",
                    (to_user, group_id, from_user, _event_time())).fetchone()
            except pg_errors.ForeignKeyViolation:
                raise NotFoundError("group or user") from None
            if row is not None:
                return _fetch_request(conn, "invite", row["membership_id"])
            # A row for this pair exists: a new statement (fresh snapshot) reads it.
            status = existing()
            if status is not None:
                raise refuse(status)
            # It was deleted in between (declined, left): try the insert again.
        raise AlreadyRequested()

    def get_request(self, request_id):
        # A membership is an invite while pending, or once accepted if someone invited
        # it (a direct join or the owner's own row is not).
        with self._tx() as conn:
            row = conn.execute(
                f"""{DM_REQUEST_SELECT} WHERE d.dm_id = %(r)s
                    UNION ALL
                    {INVITE_REQUEST_SELECT} WHERE gm.membership_id = %(r)s
                       AND (gm.status = 'pending' OR gm.invited_by IS NOT NULL)
                    LIMIT 1""",
                {"r": request_id},
            ).fetchone()
        return _request(row) if row else None

    def get_pending_invite(self, group_id, user_id) -> Optional[ChatRequest]:
        with self._tx() as conn:
            row = conn.execute(
                f"""{INVITE_REQUEST_SELECT}
                     WHERE gm.group_id = %s AND gm.user_id = %s AND gm.status = 'pending'""",
                (group_id, user_id),
            ).fetchone()
        return _request(row) if row else None

    def list_pending_requests(self, user_id) -> Tuple[List[ChatRequest], List[ChatRequest]]:
        with self._tx() as conn:
            rows = conn.execute(
                f"""{DM_REQUEST_SELECT} WHERE d.status = 'pending' AND %(u)s IN (d.initiator, d.receiver)
                    UNION ALL
                    {INVITE_REQUEST_SELECT} WHERE gm.status = 'pending' AND %(u)s IN (gm.user_id, gm.invited_by)
                    ORDER BY created_at, request_id""",
                {"u": user_id},
            ).fetchall()
        reqs = [_request(r) for r in rows]
        return [r for r in reqs if r.to_user == user_id], [r for r in reqs if r.from_user == user_id]

    def list_pending_invites(self, chat_id, limit=500) -> Tuple[List[ChatRequest], int]:
        with self._tx() as conn:
            # The page and the total from one snapshot.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            rows = conn.execute(
                f"""{INVITE_REQUEST_SELECT} WHERE gm.group_id = %s AND gm.status = 'pending'
                     ORDER BY gm.created_at, gm.membership_id
                     LIMIT %s""",
                (chat_id, max(0, limit)),
            ).fetchall()
            total = conn.execute(
                "SELECT count(*) AS n FROM group_memberships WHERE group_id = %s AND status = 'pending'",
                (chat_id,)).fetchone()["n"]
        return [_request(r) for r in rows], total

    def resolve_request(self, request_id, status, actor_id) -> Optional[ChatRequest]:
        if status not in ("accepted", "declined", "cancelled"):
            raise ValueError(f"a request can be accepted, declined or cancelled, not {status!r}")

        def work(conn: psycopg.Connection) -> Optional[ChatRequest]:
            target = conn.execute(
                """SELECT 'dm' AS kind, dm_id AS chat_id FROM dms WHERE dm_id = %(r)s
                   UNION ALL
                   SELECT 'invite', group_id FROM group_memberships WHERE membership_id = %(r)s""",
                {"r": request_id},
            ).fetchone()
            if target is None:
                return None
            # The chat row first, in the strongest mode needed: accepting flips a
            # status; declining / cancelling deletes a row (a DM: its chat too).
            mode = "NO KEY UPDATE" if status == "accepted" else "UPDATE"
            if conn.execute(f"SELECT 1 FROM chats WHERE chat_id = %s FOR {mode}",
                            (target["chat_id"],)).fetchone() is None:
                return None
            if target["kind"] == "dm":
                return self._resolve_dm(conn, request_id, status, actor_id)
            return self._resolve_invite(conn, request_id, target["chat_id"], status, actor_id)

        return self._write(work)

    @staticmethod
    def _resolve_dm(conn: psycopg.Connection, dm_id: UUID, status: str,
                    actor_id: UUID) -> Optional[ChatRequest]:
        if status == "accepted":
            row = conn.execute(
                """UPDATE dms SET status = 'accepted', accepted_at = %s
                    WHERE dm_id = %s AND status = 'pending' AND receiver = %s
                RETURNING dm_id""",
                (_event_time(), dm_id, actor_id)).fetchone()
            return _fetch_request(conn, "dm", dm_id) if row else None
        before = _fetch_request(conn, "dm", dm_id)      # the record to return, read before it goes
        if before is None or before.status != "pending":
            return None
        side = "receiver" if status == "declined" else "initiator"
        # Only a still-pending DM is deleted, and with it its chat (and the first message).
        row = conn.execute(
            f"""WITH d AS (DELETE FROM dms WHERE dm_id = %s AND status = 'pending' AND {side} = %s
                           RETURNING dm_id)
                DELETE FROM chats WHERE chat_id IN (SELECT dm_id FROM d)
                RETURNING chat_id""",
            (dm_id, actor_id)).fetchone()
        return replace(before, status=status, responded_at=_event_time()) if row else None

    @staticmethod
    def _resolve_invite(conn: psycopg.Connection, membership_id: UUID, group_id: UUID, status: str,
                        actor_id: UUID) -> Optional[ChatRequest]:
        if status == "accepted":
            row = conn.execute(
                f"""UPDATE group_memberships
                       SET status = 'accepted', joined_at = %(t)s, last_read_message_id = {NEWEST}
                     WHERE membership_id = %(r)s AND status = 'pending' AND user_id = %(a)s
                 RETURNING membership_id""",
                {"c": group_id, "r": membership_id, "a": actor_id, "t": _event_time()}).fetchone()
            return _fetch_request(conn, "invite", membership_id) if row else None
        before = _fetch_request(conn, "invite", membership_id)
        if before is None or before.status != "pending":
            return None
        side = "user_id" if status == "declined" else "invited_by"
        row = conn.execute(
            f"""DELETE FROM group_memberships
                 WHERE membership_id = %s AND status = 'pending' AND {side} = %s
             RETURNING membership_id""",
            (membership_id, actor_id)).fetchone()
        return replace(before, status=status, responded_at=_event_time()) if row else None

    def count_pending_requests(self, user_id) -> Tuple[int, int]:
        with self._tx() as conn:
            row = conn.execute(
                """SELECT (SELECT count(*) FROM dms WHERE status = 'pending' AND receiver = %(u)s)
                        + (SELECT count(*) FROM group_memberships
                            WHERE status = 'pending' AND user_id = %(u)s) AS incoming,
                          (SELECT count(*) FROM dms WHERE status = 'pending' AND initiator = %(u)s)
                        + (SELECT count(*) FROM group_memberships
                            WHERE status = 'pending' AND invited_by = %(u)s) AS outgoing""",
                {"u": user_id},
            ).fetchone()
        return row["incoming"], row["outgoing"]
