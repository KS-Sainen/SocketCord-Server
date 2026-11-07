"""Contract tests for the data layer: every Repository behaves as base.py says.

Each test runs against MemoryRepository and, when SOCKETCORD_TEST_DATABASE_URL
names a throwaway PostgreSQL database, against PostgresRepository too (the
`repo` fixture in conftest.py truncates the tables before each test). The
expectations come from the docstrings in socketcord/db/base.py, not from either
implementation, so both are held to the same rules.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence, Tuple

import pytest

from socketcord import scram
from socketcord.db.base import (DEFAULT_TEXT_BG_COLOR, DEFAULT_TEXT_COLOR, NO_MESSAGE, AlreadyMember,
                                AlreadyRequested, Chat, ChatListItem, ChatRequest, GroupListItem,
                                JoinResult, Member, MemberGroup, Message, MessageQuery, NotFoundError,
                                RateLimited, RegistrationLimit, RegistrationRule, Repository, SameColors,
                                Session, User, UserRef, UsernameTaken, message_time)

DAY = 24 * 3600
TICK = 0.003        # pause between steps whose order a test reads back from timestamps
# A UUIDv7-shaped id from the far future (the largest 48-bit millisecond clock).
FAR_FUTURE = uuid.UUID(int=(((1 << 48) - 1) << 80) | (0x7 << 76) | (0b10 << 62))


# ---------------------------------------------------------------------- helpers

def make_user(repo: Repository, name: str, password: Optional[str] = None, ip: Optional[str] = None,
              device: Optional[str] = None, limit: Optional[RegistrationLimit] = None) -> User:
    v, _ = scram.make_verifier(password or f"pw-{name}", iterations=4096)
    args = (name, v.salt, v.iterations, v.stored_key, v.server_key, ip, device)
    return repo.create_user(*args) if limit is None else repo.create_user(*args, limit)


def password_hash(password: str) -> str:
    """A verifier string that no stored user has (a stale `expected_verifier`)."""
    v, _ = scram.make_verifier(password, iterations=4096)
    return scram.encode_verifier(v.salt, v.iterations, v.stored_key, v.server_key)


def token_hash() -> bytes:
    return os.urandom(32)


def expiry(days: float = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def ref(user: User) -> UserRef:
    return UserRef(user.user_id, user.username, user.text_color, user.text_bg_color)


def ids(messages: Sequence[Message]) -> List[uuid.UUID]:
    return [m.message_id for m in messages]


def page(repo: Repository, chat_id: uuid.UUID, **query) -> Tuple[List[uuid.UUID], bool]:
    messages, more = repo.list_messages(chat_id, MessageQuery(**query))
    return ids(messages), more


def contents(repo: Repository, chat_id: uuid.UUID, **query) -> Tuple[List[str], bool]:
    messages, more = repo.list_messages(chat_id, MessageQuery(**query))
    return [m.content for m in messages], more


def search(repo: Repository, prefix: str, offset: int = 0, limit: int = 20) -> Tuple[List[str], bool]:
    found, more = repo.search_users(prefix, offset, limit)
    return [u.username for u in found], more


def chat_ids(items) -> List[uuid.UUID]:
    return [i.chat.chat_id for i in items]


def request_ids(requests: Sequence[ChatRequest]) -> List[uuid.UUID]:
    return [r.request_id for r in requests]


def between(low: uuid.UUID, high: uuid.UUID) -> uuid.UUID:
    """A UUID strictly between two message ids that is not a message id itself."""
    candidate = uuid.UUID(int=low.int + 1)
    assert low < candidate < high
    return candidate


def table_counts(repo: Repository) -> Optional[Tuple[int, ...]]:
    """Rows in (chats, groups, dms, group_memberships, messages) for PostgreSQL, to
    check that a refused call left nothing behind. None for the memory repository,
    whose state is only visible through the interface (which the tests check too)."""
    pool = getattr(repo, "pool", None)
    if pool is None:
        return None
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT (SELECT count(*) FROM chats) AS n_chats, (SELECT count(*) FROM groups) AS n_groups,
                      (SELECT count(*) FROM dms) AS n_dms, (SELECT count(*) FROM group_memberships) AS n_memberships,
                      (SELECT count(*) FROM messages) AS n_messages""").fetchone()
    return tuple(row.values()) if isinstance(row, dict) else tuple(row)


def run_parallel(fn: Callable, arg_lists: Sequence[tuple], timeout: float = 30.0) -> list:
    """Calls fn(*args) for every args in its own thread, all released at once.
    Returns what each call returned or raised, in order."""
    barrier = threading.Barrier(len(arg_lists), timeout=timeout)
    results: list = [None] * len(arg_lists)

    def worker(i: int, args: tuple) -> None:
        try:
            barrier.wait()
            results[i] = fn(*args)
        except BaseException as exc:   # handed to the test
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i, args), daemon=True) for i, args in enumerate(arg_lists)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert not any(t.is_alive() for t in threads), "a call did not finish"
    return results


# ---------------------------------------------------------------------- users

def test_create_user_stores_the_verifier_and_default_colours(repo):
    assert isinstance(repo, Repository)
    v, _ = scram.make_verifier("pw-alice", iterations=4096)
    before = datetime.now(timezone.utc)
    user = repo.create_user("Alice", v.salt, v.iterations, v.stored_key, v.server_key, None, None)
    assert isinstance(user, User) and isinstance(user.user_id, uuid.UUID)
    assert user.username == "Alice"                                    # spelling kept
    assert user.password_hash == scram.encode_verifier(v.salt, v.iterations, v.stored_key, v.server_key)
    assert (user.salt, user.iterations, user.stored_key, user.server_key) == \
        (v.salt, v.iterations, v.stored_key, v.server_key)
    assert (user.text_color, user.text_bg_color) == (DEFAULT_TEXT_COLOR, DEFAULT_TEXT_BG_COLOR)
    assert user.created_at.tzinfo is not None and abs((user.created_at - before).total_seconds()) < 60
    assert repo.get_user(user.user_id) == user
    assert repo.get_user(uuid.uuid4()) is None


def test_usernames_are_unique_case_insensitively(repo):
    carol = make_user(repo, "Carol")
    for name in ("Carol", "carol", "CAROL"):
        with pytest.raises(UsernameTaken):
            make_user(repo, name)
    assert repo.get_user_by_username("carol") == carol
    assert repo.get_user_by_username("cAROL") == carol
    assert repo.get_user_by_username("carl") is None
    assert repo.search_users("carol") == ([carol], False)              # the refused ones left nothing


def test_get_users_returns_the_known_ones(repo):
    a, b = make_user(repo, "gu_a"), make_user(repo, "gu_b")
    ghost = uuid.uuid4()
    assert repo.get_users([a.user_id, ghost, b.user_id, a.user_id]) == {a.user_id: a, b.user_id: b}
    assert repo.get_users(uid for uid in (b.user_id,)) == {b.user_id: b}     # any iterable
    assert repo.get_users([]) == {}
    assert repo.get_users([ghost]) == {}


def test_search_users_prefix_order_and_paging(repo):
    for name in ("bobcat", "robert", "BOBBY", "abob", "bob", "Bob2"):
        make_user(repo, name)
    found, more = repo.search_users("BOB")
    assert [u.username for u in found] == ["bob", "Bob2", "BOBBY", "bobcat"] and more is False
    assert found[0] == repo.get_user_by_username("bob")                # full User records
    assert search(repo, "bob", 0, 2) == (["bob", "Bob2"], True)
    assert search(repo, "bob", 2, 2) == (["BOBBY", "bobcat"], False)  # exactly the rest
    assert search(repo, "bob", 3, 2) == (["bobcat"], False)
    assert search(repo, "bob", 4, 2) == ([], False)
    assert search(repo, "bobb") == (["BOBBY"], False)
    assert search(repo, "BOBCAT") == (["bobcat"], False)
    assert search(repo, "") == (["abob", "bob", "Bob2", "BOBBY", "bobcat", "robert"], False)
    assert search(repo, "", 0, 5) == (["abob", "bob", "Bob2", "BOBBY", "bobcat"], True)
    assert search(repo, "", 5, 5) == (["robert"], False)
    assert search(repo, "zed") == ([], False)


def test_search_users_default_page_is_twenty(repo):
    for i in range(21):
        make_user(repo, f"page{i:02d}")
    found, more = repo.search_users("page")
    assert [u.username for u in found] == [f"page{i:02d}" for i in range(20)] and more is True
    assert search(repo, "page", 20) == (["page20"], False)


def test_search_users_prefix_is_literal(repo):
    for name in ("a_bc", "axbc", "a.bc", "a-bc", "abcd"):
        make_user(repo, name)
    assert search(repo, "a_") == (["a_bc"], False)                     # "_" is no wildcard
    assert search(repo, "A_B") == (["a_bc"], False)
    assert search(repo, "a.") == (["a.bc"], False)                     # nor is "."
    assert search(repo, "a-") == (["a-bc"], False)
    for pattern in ("%", "a%", "_", "_bc", "a\\", "\\", "a*", "a[", ".*", "a.*"):
        assert search(repo, pattern) == ([], False), pattern
    assert sorted(search(repo, "a")[0]) == sorted(["a_bc", "axbc", "a.bc", "a-bc", "abcd"])


def test_update_profile_sets_only_the_given_colours(repo):
    user = make_user(repo, "painter")
    red = repo.update_profile(user.user_id, "#ff0000", None)
    assert (red.text_color, red.text_bg_color) == ("#ff0000", DEFAULT_TEXT_BG_COLOR)
    assert repo.get_user(user.user_id) == red
    both = repo.update_profile(user.user_id, None, "#000000")
    assert (both.text_color, both.text_bg_color) == ("#ff0000", "#000000")
    same = repo.update_profile(user.user_id, None, None)
    assert (same.text_color, same.text_bg_color) == ("#ff0000", "#000000")
    assert (same.user_id, same.username, same.password_hash, same.created_at) == \
        (user.user_id, user.username, user.password_hash, user.created_at)
    swapped = repo.update_profile(user.user_id, "#000000", "#ff0000")  # a swap at once is fine
    assert (swapped.text_color, swapped.text_bg_color) == ("#000000", "#ff0000")
    assert repo.update_profile(uuid.uuid4(), "#010101", None) is None


def test_update_profile_refuses_equal_colours(repo):
    user = make_user(repo, "unreadable")
    repo.update_profile(user.user_id, "#ff0000", "#000000")
    with pytest.raises(SameColors):
        repo.update_profile(user.user_id, "#000000", None)             # equals the kept background
    with pytest.raises(SameColors):
        repo.update_profile(user.user_id, None, "#ff0000")             # equals the kept text colour
    with pytest.raises(SameColors):
        repo.update_profile(user.user_id, "#123456", "#123456")
    fresh = make_user(repo, "defaults")
    with pytest.raises(SameColors):
        repo.update_profile(fresh.user_id, DEFAULT_TEXT_BG_COLOR, None)
    stored = repo.get_user(user.user_id)
    assert (stored.text_color, stored.text_bg_color) == ("#ff0000", "#000000")   # nothing changed
    stored = repo.get_user(fresh.user_id)
    assert (stored.text_color, stored.text_bg_color) == (DEFAULT_TEXT_COLOR, DEFAULT_TEXT_BG_COLOR)


def test_embedded_users_show_the_current_profile(repo):
    ann, ben = make_user(repo, "ann"), make_user(repo, "ben")
    g = repo.create_group("colours", "", ann.user_id)
    sent = repo.add_message(g.chat_id, ann.user_id, "hi")
    assert sent.sender == UserRef(ann.user_id, "ann", DEFAULT_TEXT_COLOR, DEFAULT_TEXT_BG_COLOR)
    dm, _ = repo.create_dm(ben.user_id, ann.user_id)
    ann = repo.update_profile(ann.user_id, "#aa0000", "#00aa00")
    [listed], _ = repo.list_messages(g.chat_id, MessageQuery())
    assert listed.sender == UserRef(ann.user_id, "ann", "#aa0000", "#00aa00")
    member = repo.get_member(g.chat_id, ann.user_id)
    assert (member.text_color, member.text_bg_color) == ("#aa0000", "#00aa00")
    assert repo.get_chat_item(ben.user_id, dm.chat_id).peer == ref(ann)
    assert repo.get_chat_item(ann.user_id, g.chat_id).last_message.sender == ref(ann)
    invite = repo.create_request("invite", ann.user_id, ben.user_id, chat_id=g.chat_id)
    assert invite.sender == ref(ann) and invite.receiver == ref(ben)


def test_update_profile_renames_without_regard_to_case(repo):
    alice, bob = make_user(repo, "alice"), make_user(repo, "Bob")
    renamed = repo.update_profile(alice.user_id, None, None, username="Alicia")
    assert renamed.username == "Alicia"
    assert (renamed.user_id, renamed.password_hash, renamed.text_color, renamed.text_bg_color, renamed.created_at) \
        == (alice.user_id, alice.password_hash, alice.text_color, alice.text_bg_color, alice.created_at)
    assert repo.get_user(alice.user_id) == renamed
    assert repo.get_user_by_username("ALICIA") == renamed
    assert repo.get_user_by_username("alice") is None                  # the old name is gone ...
    assert search(repo, "ali") == (["Alicia"], False)
    own_case = repo.update_profile(alice.user_id, None, None, username="ALICIA")   # your own name, other case
    assert own_case.username == "ALICIA" and repo.get_user_by_username("alicia") == own_case
    assert repo.update_profile(alice.user_id, None, None, username="ALICIA") == own_case   # no change at all
    for clash in ("Bob", "bob", "BOB"):
        with pytest.raises(UsernameTaken):
            repo.update_profile(alice.user_id, "#ff0000", None, username=clash)
    assert repo.get_user(alice.user_id) == own_case                    # nothing changed, the colour neither
    newcomer = make_user(repo, "alice")                                # ... and free for someone else
    assert repo.get_user_by_username("Alice") == newcomer
    with pytest.raises(UsernameTaken):
        repo.update_profile(bob.user_id, None, None, username="aLiCe")
    both = repo.update_profile(bob.user_id, "#123456", "#abcdef", username="Bobby")
    assert (both.username, both.text_color, both.text_bg_color) == ("Bobby", "#123456", "#abcdef")
    assert repo.get_user_by_username("bob") is None and repo.get_user_by_username("bobby") == both
    assert repo.update_profile(uuid.uuid4(), None, None, username="ghost") is None


def test_update_profile_refuses_names_the_schema_refuses(repo):
    user = make_user(repo, "namer")
    for bad in ("ab", "has space", "x" * 33, "semi;colon", "", "naïve"):
        with pytest.raises(ValueError):
            repo.update_profile(user.user_id, None, None, username=bad)
    assert repo.get_user(user.user_id) == user


def test_update_profile_checks_the_colours_before_the_name(repo):
    user, other = make_user(repo, "first_check"), make_user(repo, "taken_name")
    with pytest.raises(SameColors):
        repo.update_profile(user.user_id, "#000000", "#000000", username="taken_name")
    assert repo.get_user(user.user_id) == user and repo.get_user(other.user_id) == other


def test_renamed_users_show_the_new_name_everywhere(repo):
    ann, ben = make_user(repo, "ann_old"), make_user(repo, "ben_old")
    g = repo.create_group("names", "", ann.user_id)
    repo.add_message(g.chat_id, ann.user_id, "hi")
    dm, _ = repo.create_dm(ben.user_id, ann.user_id)
    invite = repo.create_request("invite", ann.user_id, ben.user_id, chat_id=g.chat_id)
    ann = repo.update_profile(ann.user_id, None, None, username="ann_new")
    [listed], _ = repo.list_messages(g.chat_id, MessageQuery())
    assert listed.sender == ref(ann) and listed.sender.username == "ann_new"
    assert repo.get_member(g.chat_id, ann.user_id).username == "ann_new"
    assert repo.get_chat_item(ben.user_id, dm.chat_id).peer == ref(ann)
    assert repo.get_request(invite.request_id).sender == ref(ann)
    assert repo.get_users([ann.user_id]) == {ann.user_id: ann}


# ---------------------------------------------------------------------- registration limits

def test_registration_limit_by_device(repo):
    limit = RegistrationLimit(DAY, [RegistrationRule("device", "device-aaaaaaaa", 1)])
    make_user(repo, "nodevice")                                        # no device: counts for no rule
    assert repo.registration_retry_after(limit) == 0
    make_user(repo, "dev1", device="device-aaaaaaaa", limit=limit)
    assert DAY - 300 < repo.registration_retry_after(limit) <= DAY
    with pytest.raises(RateLimited) as refused:
        make_user(repo, "dev2", device="device-aaaaaaaa", limit=limit)
    assert DAY - 300 < refused.value.retry_after <= DAY
    assert repo.get_user_by_username("dev2") is None
    other = RegistrationLimit(DAY, [RegistrationRule("device", "device-bbbbbbbb", 1)])
    assert repo.registration_retry_after(other) == 0
    make_user(repo, "dev3", device="device-bbbbbbbb", limit=other)     # another machine is fine
    make_user(repo, "dev4", device="device-aaaaaaaa")                  # no limit given: no check
    assert repo.registration_retry_after(RegistrationLimit(DAY, [])) == 0
    make_user(repo, "dev5", device="device-aaaaaaaa", limit=RegistrationLimit(DAY, []))


def test_registration_limit_by_ip(repo):
    limit = RegistrationLimit(DAY, [RegistrationRule("ip", "203.0.113.7", 1)])
    make_user(repo, "ip1", ip="203.0.113.7", device="device-11111111", limit=limit)
    assert repo.registration_retry_after(limit) > DAY - 300
    with pytest.raises(RateLimited):
        make_user(repo, "ip2", ip="203.0.113.7", device="device-22222222", limit=limit)
    neighbour = RegistrationLimit(DAY, [RegistrationRule("ip", "203.0.113.8", 1)])
    assert repo.registration_retry_after(neighbour) == 0
    make_user(repo, "ip3", ip="203.0.113.8", limit=neighbour)


def test_registration_limit_counts_ipv6_per_64(repo):
    # The handler's IP rule value is security.client_key(ip): an IPv6 client counts per /64.
    net = RegistrationLimit(DAY, [RegistrationRule("ip", "2001:db8:1:2::/64", 1)])
    make_user(repo, "six1", ip="2001:db8:1:2::5", limit=net)
    assert repo.registration_retry_after(net) > DAY - 300
    with pytest.raises(RateLimited):
        make_user(repo, "six2", ip="2001:db8:1:2:ffff::9", limit=net)
    other = RegistrationLimit(DAY, [RegistrationRule("ip", "2001:db8:1:3::/64", 1)])
    assert repo.registration_retry_after(other) == 0
    make_user(repo, "six3", ip="2001:db8:1:3::5", limit=other)


def test_registration_limit_max_count_and_combined_rules(repo):
    def limit(device: str, ip: str) -> RegistrationLimit:
        return RegistrationLimit(DAY, [RegistrationRule("device", device, 1), RegistrationRule("ip", ip, 2)])

    make_user(repo, "multi1", ip="198.51.100.1", device="device-m1", limit=limit("device-m1", "198.51.100.1"))
    make_user(repo, "multi2", ip="198.51.100.1", device="device-m2", limit=limit("device-m2", "198.51.100.1"))
    with pytest.raises(RateLimited):           # fresh device ids cannot dodge the IP rule
        make_user(repo, "multi3", ip="198.51.100.1", device="device-m3", limit=limit("device-m3", "198.51.100.1"))
    with pytest.raises(RateLimited):           # nor a fresh IP the device rule
        make_user(repo, "multi4", ip="198.51.100.2", device="device-m1", limit=limit("device-m1", "198.51.100.2"))
    assert repo.registration_retry_after(limit("device-m9", "198.51.100.2")) == 0
    assert [repo.get_user_by_username(n) is None for n in ("multi1", "multi2", "multi3", "multi4")] == \
        [False, False, True, True]


def test_registration_window_expires(repo):
    limit = RegistrationLimit(1, [RegistrationRule("device", "device-window1", 1)])
    make_user(repo, "win1", device="device-window1", limit=limit)
    assert 0 < repo.registration_retry_after(limit) <= 1
    with pytest.raises(RateLimited):
        make_user(repo, "win2", device="device-window1", limit=limit)
    time.sleep(1.1)
    assert repo.registration_retry_after(limit) == 0
    make_user(repo, "win3", device="device-window1", limit=limit)


def test_registration_limit_check_and_insert_are_atomic(repo):
    limit = RegistrationLimit(DAY, [RegistrationRule("device", "device-same-machine", 1)])
    verifiers = [scram.make_verifier(f"pw-race{i}", iterations=4096)[0] for i in range(6)]

    def register(i: int):
        v = verifiers[i]
        return repo.create_user(f"race{i}", v.salt, v.iterations, v.stored_key, v.server_key,
                                None, "device-same-machine", limit)

    results = run_parallel(register, [(i,) for i in range(6)])
    assert sum(isinstance(r, User) for r in results) == 1, results
    assert sum(isinstance(r, RateLimited) for r in results) == 5, results


# ---------------------------------------------------------------------- sessions

def test_create_session_replaces_the_previous_one(repo):
    user = make_user(repo, "sess")
    first, second = token_hash(), token_hash()
    exp1, exp2 = expiry(1), expiry(2)
    assert repo.create_session(first, user.user_id, exp1, user.password_hash) == \
        (Session(first, user.user_id, exp1), None)                      # a new user has no session
    assert repo.get_session(first) == (Session(first, user.user_id, exp1), user)
    assert repo.create_session(second, user.user_id, exp2, user.password_hash) == \
        (Session(second, user.user_id, exp2), first)
    assert repo.get_session(first) is None
    assert repo.get_session(second) == (Session(second, user.user_id, exp2), user)


def test_create_session_refuses_a_stale_verifier(repo):
    user = make_user(repo, "stalelogin")
    current = token_hash()
    repo.create_session(current, user.user_id, expiry(), user.password_hash)
    rejected = token_hash()
    assert repo.create_session(rejected, user.user_id, expiry(), password_hash("not-the-password")) is None
    assert repo.get_session(current)[0].token_hash == current          # untouched
    assert repo.get_session(rejected) is None
    assert repo.create_session(token_hash(), uuid.uuid4(), expiry(), user.password_hash) is None


def test_get_session_returns_expired_sessions_and_none_for_unknown(repo):
    user = make_user(repo, "expired")
    h, past = token_hash(), datetime.now(timezone.utc) - timedelta(hours=1)
    repo.create_session(h, user.user_id, past, user.password_hash)
    session, who = repo.get_session(h)
    assert session == Session(h, user.user_id, past) and who == user
    assert repo.get_session(token_hash()) is None
    assert repo.revoke_session(h) is True                              # expired, but still the current one
    assert repo.get_session(h) is None


def test_revoke_session_ends_only_the_current_session(repo):
    user = make_user(repo, "logout")
    old, current = token_hash(), token_hash()
    repo.create_session(old, user.user_id, expiry(), user.password_hash)
    repo.create_session(current, user.user_id, expiry(), user.password_hash)
    assert repo.revoke_session(old) is False                           # replaced already
    assert repo.get_session(current) is not None
    assert repo.revoke_session(current) is True
    assert repo.revoke_session(current) is False
    assert repo.get_session(current) is None
    assert repo.revoke_session(token_hash()) is False
    fresh, exp = token_hash(), expiry()
    assert repo.create_session(fresh, user.user_id, exp, user.password_hash) == \
        (Session(fresh, user.user_id, exp), None)                       # nothing left to replace


def test_parallel_logins_each_report_the_hash_they_replaced(repo):
    user = make_user(repo, "racer")
    start = token_hash()
    repo.create_session(start, user.user_id, expiry(), user.password_hash)
    hashes = [token_hash() for _ in range(6)]
    results = run_parallel(lambda h: repo.create_session(h, user.user_id, expiry(), user.password_hash),
                           [(h,) for h in hashes])
    assert all(isinstance(r, tuple) for r in results), results
    successor = {}
    for new, (session, replaced) in zip(hashes, results):
        assert session.token_hash == new
        assert replaced not in successor, "two logins replaced the same session"
        successor[replaced] = new
    # Following "who replaced whom" from the first session visits every login once.
    chain, current = [], start
    for _ in hashes:
        current = successor.get(current)
        assert current is not None, (start, successor)
        chain.append(current)
    assert sorted(chain) == sorted(hashes)
    assert repo.get_session(current)[0].token_hash == current
    assert [h for h in [start] + hashes if repo.get_session(h) is not None] == [current]


def test_change_credentials_rotates_the_session_token(repo):
    user = make_user(repo, "rotor")
    old_token = token_hash()
    repo.create_session(old_token, user.user_id, expiry(), user.password_hash)
    nv, _ = scram.make_verifier("new-password", iterations=4096)
    new_hash = scram.encode_verifier(nv.salt, nv.iterations, nv.stored_key, nv.server_key)
    new_token, exp = token_hash(), expiry(3)
    session = repo.change_credentials(user.user_id, nv.salt, nv.iterations, nv.stored_key, nv.server_key,
                                      user.password_hash, old_token, new_token, exp)
    assert session == Session(new_token, user.user_id, exp)
    assert repo.get_session(old_token) is None                         # the old hash finds nothing
    found, who = repo.get_session(new_token)
    assert found == session
    assert who.password_hash == new_hash
    assert (who.salt, who.iterations, who.stored_key, who.server_key) == \
        (nv.salt, nv.iterations, nv.stored_key, nv.server_key)
    assert repo.get_user(user.user_id).password_hash == new_hash
    # Logins checked against the old verifier are refused; the new one works.
    assert repo.create_session(token_hash(), user.user_id, expiry(), user.password_hash) is None
    latest = token_hash()
    assert repo.create_session(latest, user.user_id, exp, new_hash) == (Session(latest, user.user_id, exp), new_token)


def test_change_credentials_refuses_a_stale_verifier(repo):
    user = make_user(repo, "stalepw")
    current = token_hash()
    repo.create_session(current, user.user_id, expiry(), user.password_hash)
    nv, _ = scram.make_verifier("new-password", iterations=4096)
    new_token = token_hash()
    assert repo.change_credentials(user.user_id, nv.salt, nv.iterations, nv.stored_key, nv.server_key,
                                   password_hash("changed-meanwhile"), current, new_token, expiry()) is None
    assert repo.get_user(user.user_id).password_hash == user.password_hash
    assert repo.get_session(current) is not None and repo.get_session(new_token) is None


def test_change_credentials_refuses_a_stale_session(repo):
    user = make_user(repo, "stalesess")
    first, second = token_hash(), token_hash()
    repo.create_session(first, user.user_id, expiry(), user.password_hash)
    repo.create_session(second, user.user_id, expiry(), user.password_hash)    # logged in elsewhere
    nv, _ = scram.make_verifier("new-password", iterations=4096)
    new_token = token_hash()
    assert repo.change_credentials(user.user_id, nv.salt, nv.iterations, nv.stored_key, nv.server_key,
                                   user.password_hash, first, new_token, expiry()) is None
    assert repo.get_user(user.user_id).password_hash == user.password_hash
    assert repo.get_session(second) is not None and repo.get_session(new_token) is None
    assert repo.revoke_session(second) is True                          # no session at all now
    assert repo.change_credentials(user.user_id, nv.salt, nv.iterations, nv.stored_key, nv.server_key,
                                   user.password_hash, second, new_token, expiry()) is None
    assert repo.get_user(user.user_id).password_hash == user.password_hash
    assert repo.change_credentials(uuid.uuid4(), nv.salt, nv.iterations, nv.stored_key, nv.server_key,
                                   user.password_hash, second, new_token, expiry()) is None


# ---------------------------------------------------------------------- groups

def test_create_group_makes_the_owner_an_accepted_member(repo):
    owner = make_user(repo, "founder")
    g = repo.create_group("Study group", "Ch1-3 only", owner.user_id)
    assert isinstance(g, Chat) and isinstance(g.chat_id, uuid.UUID)
    assert (g.kind, g.name, g.description, g.created_by) == ("group", "Study group", "Ch1-3 only", owner.user_id)
    assert (g.initiator, g.receiver, g.status) == (None, None, "accepted")
    assert g.created_at.tzinfo is not None
    assert repo.get_chat(g.chat_id) == g
    assert repo.get_chat(uuid.uuid4()) is None
    [member] = repo.get_members(g.chat_id)
    assert isinstance(member, Member)
    assert (member.chat_id, member.user_id, member.username, member.role, member.last_read_message_id) == \
        (g.chat_id, owner.user_id, "founder", "owner", NO_MESSAGE)
    assert (member.text_color, member.text_bg_color) == (DEFAULT_TEXT_COLOR, DEFAULT_TEXT_BG_COLOR)
    assert member.joined_at.tzinfo is not None
    assert repo.get_member(g.chat_id, owner.user_id) == member
    item = repo.get_chat_item(owner.user_id, g.chat_id)
    assert isinstance(item, ChatListItem)
    assert (item.chat, item.role, item.last_read_message_id, item.unread, item.member_count,
            item.last_message, item.peer) == (g, "owner", NO_MESSAGE, 0, 1, None, None)
    before = table_counts(repo)
    with pytest.raises(NotFoundError):                                  # the owner is gone
        repo.create_group("orphan", "", uuid.uuid4())
    assert table_counts(repo) == before and chat_ids(repo.list_groups()[0]) == [g.chat_id]


def test_group_creator_starts_with_nothing_read(repo):
    owner, other = make_user(repo, "creator"), make_user(repo, "chatter")
    g = repo.create_group("unread", "", owner.user_id)
    repo.join_group(g.chat_id, other.user_id)
    repo.add_message(g.chat_id, owner.user_id, "mine")                 # own messages are never unread
    repo.add_message(g.chat_id, other.user_id, "one")
    last = repo.add_message(g.chat_id, other.user_id, "two")
    item = repo.get_chat_item(owner.user_id, g.chat_id)
    assert (item.last_read_message_id, item.unread, item.last_message) == (NO_MESSAGE, 2, last)
    assert repo.get_member(g.chat_id, owner.user_id).last_read_message_id == NO_MESSAGE


def test_new_member_read_marker_and_unread(repo):
    owner, early, late = make_user(repo, "host"), make_user(repo, "early"), make_user(repo, "late")
    g = repo.create_group("joiners", "", owner.user_id)
    assert repo.join_group(g.chat_id, early.user_id) == JoinResult(settled=None)
    assert repo.get_member(g.chat_id, early.user_id).last_read_message_id == NO_MESSAGE   # nothing to read yet
    repo.add_message(g.chat_id, owner.user_id, "1")
    newest = repo.add_message(g.chat_id, owner.user_id, "2")
    item = repo.get_chat_item(early.user_id, g.chat_id)
    assert (item.last_read_message_id, item.unread) == (NO_MESSAGE, 2)
    repo.join_group(g.chat_id, late.user_id)                          # joining reads what came before
    assert repo.get_member(g.chat_id, late.user_id).last_read_message_id == newest.message_id
    assert repo.get_chat_item(late.user_id, g.chat_id).unread == 0
    repo.add_message(g.chat_id, owner.user_id, "3")
    assert repo.get_chat_item(late.user_id, g.chat_id).unread == 1
    assert repo.get_chat_item(early.user_id, g.chat_id).unread == 3


def test_system_notices_count_as_unread(repo):
    owner, member = make_user(repo, "notices"), make_user(repo, "noticed")
    g = repo.create_group("notices", "", owner.user_id)
    repo.join_group(g.chat_id, member.user_id)
    notice = repo.add_message(g.chat_id, None, "noticed joined the group", kind="system")
    for user in (owner, member):
        item = repo.get_chat_item(user.user_id, g.chat_id)
        assert (item.unread, item.last_message) == (1, notice)


def test_join_group_without_an_invite_makes_a_plain_membership(repo):
    owner, joiner = make_user(repo, "opener"), make_user(repo, "walkin")
    g = repo.create_group("open", "", owner.user_id)
    result = repo.join_group(g.chat_id, joiner.user_id)
    assert isinstance(result, JoinResult) and result.settled is None
    member = repo.get_member(g.chat_id, joiner.user_id)
    assert (member.user_id, member.username, member.role) == (joiner.user_id, "walkin", "member")
    assert repo.get_chat_item(joiner.user_id, g.chat_id).member_count == 2
    assert repo.join_group(g.chat_id, joiner.user_id) is None          # twice: already a member
    assert repo.join_group(g.chat_id, owner.user_id) is None           # the owner is in already
    assert len(repo.get_members(g.chat_id)) == 2
    with pytest.raises(NotFoundError):
        repo.join_group(uuid.uuid4(), joiner.user_id)


def test_join_group_settles_a_pending_invite(repo):
    owner, invitee = make_user(repo, "inviter"), make_user(repo, "joiner")
    g = repo.create_group("settle", "", owner.user_id)
    newest = repo.add_message(g.chat_id, owner.user_id, "welcome")
    invite = repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=g.chat_id)
    result = repo.join_group(g.chat_id, invitee.user_id)
    assert isinstance(result, JoinResult) and result.settled is not None
    settled = result.settled
    assert (settled.request_id, settled.kind, settled.status, settled.chat_id, settled.chat_name) == \
        (invite.request_id, "invite", "accepted", g.chat_id, "settle")
    assert (settled.sender, settled.receiver) == (ref(owner), ref(invitee))
    assert settled.responded_at is not None
    member = repo.get_member(g.chat_id, invitee.user_id)
    assert member.role == "member" and member.last_read_message_id == newest.message_id
    assert repo.list_pending_invites(g.chat_id) == ([], 0)
    assert repo.count_pending_requests(invitee.user_id) == (0, 0)
    assert repo.count_pending_requests(owner.user_id) == (0, 0)
    assert repo.get_request(invite.request_id).status == "accepted"
    assert repo.join_group(g.chat_id, invitee.user_id) is None
    assert repo.resolve_request(invite.request_id, "accepted", invitee.user_id) is None   # settled already


def test_remove_member_leaves_the_group(repo):
    owner, member, outsider = make_user(repo, "stayer"), make_user(repo, "leaver"), make_user(repo, "never")
    g = repo.create_group("leaving", "", owner.user_id)
    repo.join_group(g.chat_id, member.user_id)
    assert repo.remove_member(g.chat_id, outsider.user_id) == (False, False)
    assert repo.remove_member(g.chat_id, member.user_id) == (True, False)
    assert repo.get_member(g.chat_id, member.user_id) is None
    assert [m.user_id for m in repo.get_members(g.chat_id)] == [owner.user_id]
    assert repo.list_user_chats(member.user_id) == []
    assert repo.get_chat_item(member.user_id, g.chat_id) is None
    assert repo.get_chat_item(owner.user_id, g.chat_id).member_count == 1
    assert repo.remove_member(g.chat_id, member.user_id) == (False, False)     # gone already
    assert repo.remove_member(uuid.uuid4(), owner.user_id) == (False, False)


def test_last_member_leaving_deletes_the_group_its_invites_and_messages(repo):
    owner, first, second = make_user(repo, "lastone"), make_user(repo, "invited1"), make_user(repo, "invited2")
    g = repo.create_group("doomed", "", owner.user_id)
    survivor = repo.create_group("survivor", "", first.user_id)
    kept = repo.add_message(survivor.chat_id, first.user_id, "still here")
    for text in ("a", "b", "c"):
        repo.add_message(g.chat_id, owner.user_id, text)
    invite1 = repo.create_request("invite", owner.user_id, first.user_id, chat_id=g.chat_id)
    invite2 = repo.create_request("invite", owner.user_id, second.user_id, chat_id=g.chat_id)
    assert repo.remove_member(g.chat_id, owner.user_id) == (True, True)
    assert repo.get_chat(g.chat_id) is None
    assert repo.get_request(invite1.request_id) is None and repo.get_request(invite2.request_id) is None
    assert repo.list_pending_invites(g.chat_id) == ([], 0)
    assert repo.list_pending_requests(first.user_id) == ([], [])
    assert repo.count_pending_requests(owner.user_id) == (0, 0)
    assert repo.list_messages(g.chat_id, MessageQuery()) == ([], False)
    assert repo.first_message_after(g.chat_id, NO_MESSAGE) is None
    assert repo.get_members(g.chat_id) == []
    assert repo.list_user_chats(owner.user_id) == []
    assert chat_ids(repo.list_groups()[0]) == [survivor.chat_id]
    assert repo.resolve_request(invite1.request_id, "accepted", first.user_id) is None
    assert repo.remove_member(g.chat_id, owner.user_id) == (False, False)
    with pytest.raises(NotFoundError):
        repo.join_group(g.chat_id, second.user_id)
    with pytest.raises(NotFoundError):
        repo.add_message(g.chat_id, None, "anyone?", kind="system")
    with pytest.raises(NotFoundError):
        repo.create_request("invite", first.user_id, second.user_id, chat_id=g.chat_id)
    assert repo.list_messages(survivor.chat_id, MessageQuery()) == ([kept], False)
    assert table_counts(repo) in (None, (1, 1, 0, 1, 1))


def test_owner_role_belongs_to_the_creator(repo):
    owner, member = make_user(repo, "creator2"), make_user(repo, "regular")
    g = repo.create_group("roles", "", owner.user_id)
    repo.join_group(g.chat_id, member.user_id)
    assert [(m.user_id, m.role) for m in repo.get_members(g.chat_id)] == \
        [(owner.user_id, "owner"), (member.user_id, "member")]
    assert repo.remove_member(g.chat_id, owner.user_id) == (True, False)
    assert [(m.user_id, m.role) for m in repo.get_members(g.chat_id)] == [(member.user_id, "member")]
    assert repo.get_chat(g.chat_id).created_by == owner.user_id
    assert repo.join_group(g.chat_id, owner.user_id) == JoinResult(settled=None)
    assert repo.get_member(g.chat_id, owner.user_id).role == "owner"
    assert repo.get_chat_item(owner.user_id, g.chat_id).role == "owner"
    assert repo.get_chat_item(member.user_id, g.chat_id).role == "member"


def test_get_members_lists_accepted_members_by_join_time(repo):
    owner, a, b, invited, late = (make_user(repo, n) for n in ("gm_owner", "gm_a", "gm_b", "gm_inv", "gm_late"))
    g = repo.create_group("members", "", owner.user_id)
    time.sleep(TICK)
    repo.join_group(g.chat_id, a.user_id)
    time.sleep(TICK)
    invite = repo.create_request("invite", owner.user_id, invited.user_id, chat_id=g.chat_id)
    time.sleep(TICK)
    repo.join_group(g.chat_id, b.user_id)
    time.sleep(TICK)
    repo.join_group(g.chat_id, late.user_id)
    assert repo.get_member(g.chat_id, invited.user_id) is None          # invited is not in
    members = repo.get_members(g.chat_id)
    assert [m.user_id for m in members] == [owner.user_id, a.user_id, b.user_id, late.user_id]
    time.sleep(TICK)
    repo.resolve_request(invite.request_id, "accepted", invited.user_id)  # joins when accepting
    members = repo.get_members(g.chat_id)
    assert [m.user_id for m in members] == [owner.user_id, a.user_id, b.user_id, late.user_id, invited.user_id]
    assert members == sorted(members, key=lambda m: (m.joined_at, m.user_id))
    assert [m.role for m in members] == ["owner", "member", "member", "member", "member"]
    assert [m.username for m in members] == ["gm_owner", "gm_a", "gm_b", "gm_late", "gm_inv"]
    assert all(m.chat_id == g.chat_id for m in members)
    assert repo.get_member(g.chat_id, a.user_id) == members[1]
    assert repo.get_members(uuid.uuid4()) == []


def test_list_groups_pages_oldest_first_with_accepted_members(repo):
    o, x, invited = make_user(repo, "lg_o"), make_user(repo, "lg_x"), make_user(repo, "lg_invited")
    g1 = repo.create_group("one", "first", o.user_id)
    time.sleep(TICK)
    g2 = repo.create_group("two", "", x.user_id)
    time.sleep(TICK)
    g3 = repo.create_group("three", "", o.user_id)
    repo.join_group(g1.chat_id, x.user_id)
    repo.create_request("invite", o.user_id, invited.user_id, chat_id=g1.chat_id)
    repo.create_dm(o.user_id, x.user_id)                              # DMs are not groups
    groups, more = repo.list_groups()
    assert chat_ids(groups) == [g1.chat_id, g2.chat_id, g3.chat_id] and more is False
    assert all(isinstance(i, GroupListItem) for i in groups)
    assert groups[0].chat == repo.get_chat(g1.chat_id)
    assert [m.user_id for m in groups[0].members] == [o.user_id, x.user_id]   # not the invitee
    assert groups[0].members == repo.get_members(g1.chat_id)
    assert [m.user_id for m in groups[1].members] == [x.user_id]
    assert (chat_ids(repo.list_groups(0, 2)[0]), repo.list_groups(0, 2)[1]) == ([g1.chat_id, g2.chat_id], True)
    assert (chat_ids(repo.list_groups(2, 2)[0]), repo.list_groups(2, 2)[1]) == ([g3.chat_id], False)
    assert (chat_ids(repo.list_groups(1, 2)[0]), repo.list_groups(1, 2)[1]) == ([g2.chat_id, g3.chat_id], False)
    assert (chat_ids(repo.list_groups(0, 3)[0]), repo.list_groups(0, 3)[1]) == \
        ([g1.chat_id, g2.chat_id, g3.chat_id], False)
    assert repo.list_groups(3, 2) == ([], False)
    assert repo.remove_member(g2.chat_id, x.user_id) == (True, True)   # nobody accepted is left
    assert chat_ids(repo.list_groups()[0]) == [g1.chat_id, g3.chat_id]


# ---------------------------------------------------------------------- read markers

def test_mark_read_is_clamped_to_the_newest_message_and_never_moves_back(repo):
    owner, other = make_user(repo, "reader"), make_user(repo, "poster")
    g = repo.create_group("marks", "", owner.user_id)
    repo.join_group(g.chat_id, other.user_id)
    # An empty chat has nothing to read, whatever id is asked for.
    assert repo.mark_read(g.chat_id, owner.user_id, None) == NO_MESSAGE
    assert repo.mark_read(g.chat_id, owner.user_id, FAR_FUTURE) == NO_MESSAGE
    assert repo.get_member(g.chat_id, owner.user_id).last_read_message_id == NO_MESSAGE
    m1, m2, m3 = [repo.add_message(g.chat_id, other.user_id, text) for text in ("1", "2", "3")]
    assert repo.get_chat_item(owner.user_id, g.chat_id).unread == 3      # the far-future mark did not stick
    assert repo.mark_read(g.chat_id, owner.user_id, m1.message_id) == m1.message_id
    assert repo.get_chat_item(owner.user_id, g.chat_id).unread == 2
    assert repo.mark_read(g.chat_id, owner.user_id, FAR_FUTURE) == m3.message_id   # clamped
    assert repo.get_member(g.chat_id, owner.user_id).last_read_message_id == m3.message_id
    assert repo.mark_read(g.chat_id, owner.user_id, m2.message_id) == m3.message_id   # never back
    assert repo.mark_read(g.chat_id, owner.user_id, NO_MESSAGE) == m3.message_id
    assert repo.get_chat_item(owner.user_id, g.chat_id).unread == 0
    m4 = repo.add_message(g.chat_id, other.user_id, "4")
    assert repo.get_chat_item(owner.user_id, g.chat_id).unread == 1
    assert repo.mark_read(g.chat_id, owner.user_id, None) == m4.message_id
    item = repo.get_chat_item(owner.user_id, g.chat_id)
    assert (item.last_read_message_id, item.unread) == (m4.message_id, 0)


def test_mark_read_outside_the_chat_returns_no_message(repo):
    owner, outsider, invitee = make_user(repo, "mr_owner"), make_user(repo, "mr_out"), make_user(repo, "mr_inv")
    g = repo.create_group("private", "", owner.user_id)
    repo.add_message(g.chat_id, owner.user_id, "members only")
    assert repo.mark_read(g.chat_id, outsider.user_id, None) == NO_MESSAGE
    repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=g.chat_id)
    assert repo.mark_read(g.chat_id, invitee.user_id, None) == NO_MESSAGE    # invited is not in
    assert repo.mark_read(uuid.uuid4(), owner.user_id, None) == NO_MESSAGE
    req = repo.create_request("dm", outsider.user_id, invitee.user_id, message="pending")
    assert repo.mark_read(req.chat_id, outsider.user_id, None) == NO_MESSAGE
    assert repo.mark_read(req.chat_id, invitee.user_id, None) == NO_MESSAGE
    repo.resolve_request(req.request_id, "accepted", invitee.user_id)
    item = repo.get_chat_item(invitee.user_id, req.chat_id)
    assert (item.last_read_message_id, item.unread) == (NO_MESSAGE, 1)    # the pending-time mark did nothing


def test_mark_read_keeps_one_marker_per_dm_side(repo):
    a, b = make_user(repo, "side_a"), make_user(repo, "side_b")
    dm, _ = repo.create_dm(a.user_id, b.user_id)
    for user in (a, b):
        assert repo.get_member(dm.chat_id, user.user_id).last_read_message_id == NO_MESSAGE
        assert repo.get_chat_item(user.user_id, dm.chat_id).last_read_message_id == NO_MESSAGE
    m1 = repo.add_message(dm.chat_id, a.user_id, "1")
    repo.add_message(dm.chat_id, b.user_id, "2")
    m3 = repo.add_message(dm.chat_id, a.user_id, "3")
    assert repo.get_chat_item(a.user_id, dm.chat_id).unread == 1        # "2"
    assert repo.get_chat_item(b.user_id, dm.chat_id).unread == 2        # "1" and "3"
    assert repo.mark_read(dm.chat_id, b.user_id, m1.message_id) == m1.message_id
    assert repo.get_chat_item(b.user_id, dm.chat_id).unread == 1        # "3"
    assert repo.get_member(dm.chat_id, b.user_id).last_read_message_id == m1.message_id
    assert repo.get_member(dm.chat_id, a.user_id).last_read_message_id == NO_MESSAGE
    assert repo.mark_read(dm.chat_id, a.user_id, None) == m3.message_id
    assert repo.get_chat_item(a.user_id, dm.chat_id).unread == 0
    assert repo.get_member(dm.chat_id, b.user_id).last_read_message_id == m1.message_id


# ---------------------------------------------------------------------- messages

def test_message_ids_are_uuid7_increasing_and_carry_the_time(repo):
    owner = make_user(repo, "ids")
    g = repo.create_group("ids", "", owner.user_id)
    now = datetime.now(timezone.utc)
    sent = [repo.add_message(g.chat_id, owner.user_id, f"m{i}") for i in range(100)]
    for m in sent:
        assert isinstance(m, Message) and isinstance(m.message_id, uuid.UUID)
        assert m.message_id.version == 7 and m.message_id.variant == uuid.RFC_4122
        assert m.created_at == message_time(m.message_id)
        assert abs((m.created_at - now).total_seconds()) < 60
    assert all(a < b for a, b in zip(ids(sent), ids(sent)[1:]))
    listed, more = repo.list_messages(g.chat_id, MessageQuery(count=100))
    assert listed == sent and more is False
    assert all(m.created_at == message_time(m.message_id) for m in listed)


def test_add_message_records_sender_and_kind(repo):
    owner = make_user(repo, "talker")
    g = repo.create_group("talk", "", owner.user_id)
    text = repo.add_message(g.chat_id, owner.user_id, "สวัสดี 👋 hello")
    assert (text.chat_id, text.kind, text.content) == (g.chat_id, "text", "สวัสดี 👋 hello")
    assert text.sender == ref(owner) and text.sender_id == owner.user_id
    notice = repo.add_message(g.chat_id, None, "talker renamed the group", kind="system")
    assert (notice.sender, notice.sender_id, notice.kind) == (None, None, "system")
    assert repo.list_messages(g.chat_id, MessageQuery()) == ([text, notice], False)
    with pytest.raises(NotFoundError):
        repo.add_message(uuid.uuid4(), owner.user_id, "nowhere")
    with pytest.raises(NotFoundError):                                  # the sender is gone
        repo.add_message(g.chat_id, uuid.uuid4(), "ghost")
    assert repo.list_messages(g.chat_id, MessageQuery()) == ([text, notice], False)


def test_new_ids_stay_above_the_chats_newest_id(repo):
    """An id made before a restart by a process whose clock ran ahead. The row has to
    be written behind the repository's back, so this runs on PostgreSQL only."""
    pool = getattr(repo, "pool", None)
    if pool is None:
        pytest.skip("needs a message row written straight into the table")
    owner = make_user(repo, "restart")
    g = repo.create_group("restart", "", owner.user_id)
    ahead_ms = time.time_ns() // 1_000_000 + 40     # small, so later tests' clocks are not disturbed
    ahead = uuid.UUID(int=(ahead_ms << 80) | (0x7 << 76) | (0x800 << 64) | (0b10 << 62) | 12345)
    with pool.connection() as conn:
        conn.execute("INSERT INTO messages (message_id, chat_id, sender_id, kind, content) "
                     "VALUES (%s, %s, %s, 'text', 'from before the restart')", (ahead, g.chat_id, owner.user_id))
    try:
        after = repo.add_message(g.chat_id, owner.user_id, "after the restart")
        assert after.message_id > ahead and after.message_id.version == 7
        assert after.created_at == message_time(after.message_id) >= message_time(ahead)
        assert page(repo, g.chat_id) == ([ahead, after.message_id], False)
    finally:
        # The process-wide id clock now runs at `ahead`; let the real clock pass it.
        time.sleep(max(0.0, (ahead_ms + 5) / 1000 - time.time()))


def test_after_id_paging_never_skips_under_concurrent_writers(repo):
    owner = make_user(repo, "writer")
    g = repo.create_group("busy", "", owner.user_id)
    writers, per_writer = 4, 25
    done = threading.Event()
    seen: List[uuid.UUID] = []
    errors: list = []

    def write(w: int) -> None:
        for i in range(per_writer):
            repo.add_message(g.chat_id, owner.user_id, f"w{w}-{i}")

    def read() -> None:
        last = NO_MESSAGE
        try:
            while True:
                finished = done.is_set()
                msgs, _ = repo.list_messages(g.chat_id, MessageQuery(count=50, after_id=last))
                seen.extend(ids(msgs))
                if msgs:
                    last = msgs[-1].message_id
                elif finished:
                    return
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    results = run_parallel(write, [(w,) for w in range(writers)])
    done.set()
    reader.join(30)
    assert results == [None] * writers and not errors and not reader.is_alive()
    everything, more = repo.list_messages(g.chat_id, MessageQuery(count=1000))
    assert len(everything) == writers * per_writer and more is False
    assert seen == ids(everything)          # a reader paging with after_id saw every message, in order


def filled_group(repo: Repository, count: int = 10, name: str = "history"):
    owner = make_user(repo, f"{name}_owner")
    g = repo.create_group(name, "", owner.user_id)
    sent = [repo.add_message(g.chat_id, owner.user_id, f"msg {i}") for i in range(count)]
    return g, owner, ids(sent)


def test_list_messages_pages_back_from_the_newest(repo):
    g, owner, m = filled_group(repo)
    noise = repo.create_group("noise", "", owner.user_id)
    repo.add_message(noise.chat_id, owner.user_id, "elsewhere")         # other chats never show up
    assert not MessageQuery().ascending
    assert page(repo, g.chat_id) == (m, False)                          # default count 50
    assert page(repo, g.chat_id, count=4) == (m[6:], True)
    assert page(repo, g.chat_id, count=4, before_id=m[6]) == (m[2:6], True)
    assert page(repo, g.chat_id, count=4, before_id=m[2]) == (m[:2], False)
    assert page(repo, g.chat_id, count=10) == (m, False)
    assert page(repo, g.chat_id, count=15) == (m, False)                # more than there are: all of them
    assert page(repo, g.chat_id, count=1) == (m[9:], True)
    assert page(repo, g.chat_id, count=4, before_id=between(m[6], m[7])) == (m[3:7], True)
    assert page(repo, g.chat_id, count=4, before_id=m[0]) == ([], False)
    assert page(repo, g.chat_id, count=4, before_id=NO_MESSAGE) == ([], False)
    assert repo.list_messages(uuid.uuid4(), MessageQuery()) == ([], False)


def test_list_messages_pages_forward_from_after_id(repo):
    g, _, m = filled_group(repo)
    assert MessageQuery(after_id=m[1]).ascending
    assert page(repo, g.chat_id, count=4, after_id=m[1]) == (m[2:6], True)
    assert page(repo, g.chat_id, count=4, after_id=m[5]) == (m[6:], False)     # exactly the rest
    assert page(repo, g.chat_id, count=4, after_id=m[9]) == ([], False)
    assert page(repo, g.chat_id, count=4, after_id=NO_MESSAGE) == (m[:4], True)
    assert page(repo, g.chat_id, count=4, after_id=between(m[3], m[4])) == (m[4:8], True)
    # Both bounds: not ascending, so the newest `count` of the range.
    assert not MessageQuery(after_id=m[1], before_id=m[8]).ascending
    assert page(repo, g.chat_id, count=3, after_id=m[1], before_id=m[8]) == (m[5:8], True)
    assert page(repo, g.chat_id, count=6, after_id=m[1], before_id=m[8]) == (m[2:8], False)
    assert page(repo, g.chat_id, count=8, after_id=m[1], before_id=m[8]) == (m[2:8], False)


def test_list_messages_content_search_is_literal_and_case_insensitive(repo):
    owner = make_user(repo, "searcher")
    g = repo.create_group("search", "", owner.user_id)
    texts = ["Hello World", "say hello", "100% sure", "a_b", "axb", "back\\slash", "HELLO again", "nothing here"]
    sent = [repo.add_message(g.chat_id, owner.user_id, t) for t in texts]
    assert contents(repo, g.chat_id, content="hello") == (["Hello World", "say hello", "HELLO again"], False)
    assert contents(repo, g.chat_id, content="HeLLo", count=2) == (["say hello", "HELLO again"], True)
    assert contents(repo, g.chat_id, content="hello", count=2, after_id=sent[0].message_id) == \
        (["say hello", "HELLO again"], False)
    assert contents(repo, g.chat_id, content="hello", count=1, after_id=NO_MESSAGE) == (["Hello World"], True)
    assert contents(repo, g.chat_id, content="hello", before_id=sent[6].message_id) == \
        (["Hello World", "say hello"], False)
    assert contents(repo, g.chat_id, content="%") == (["100% sure"], False)
    assert contents(repo, g.chat_id, content="_") == (["a_b"], False)
    assert contents(repo, g.chat_id, content="A_B") == (["a_b"], False)
    assert contents(repo, g.chat_id, content="\\") == (["back\\slash"], False)
    for literal in ("a.b", "a*b", "[ab]", "^say", "hello$", "a%b", "zzz"):
        assert contents(repo, g.chat_id, content=literal) == ([], False), literal


def test_list_messages_time_filters(repo):
    owner = make_user(repo, "clock")
    g = repo.create_group("times", "", owner.user_id)
    early = [repo.add_message(g.chat_id, owner.user_id, f"early {i}") for i in range(2)]
    time.sleep(0.02)
    middle = datetime.now(timezone.utc)
    time.sleep(0.02)
    late = [repo.add_message(g.chat_id, owner.user_id, f"late {i}") for i in range(2)]
    assert MessageQuery(after_time=middle).ascending
    assert page(repo, g.chat_id, after_time=middle) == (ids(late), False)
    assert page(repo, g.chat_id, after_time=middle, count=1) == (ids(late)[:1], True)
    assert page(repo, g.chat_id, before_time=middle) == (ids(early), False)
    assert page(repo, g.chat_id, before_time=middle, count=1) == (ids(early)[1:], True)
    assert page(repo, g.chat_id, after_time=middle, before_id=late[1].message_id) == (ids(late)[:1], False)
    assert page(repo, g.chat_id, before_time=middle, after_id=early[0].message_id) == (ids(early)[1:], False)
    assert page(repo, g.chat_id, after_time=middle, before_time=middle + timedelta(hours=1)) == (ids(late), False)
    assert page(repo, g.chat_id, after_time=middle - timedelta(hours=1), count=3) == \
        (ids(early) + ids(late)[:1], True)
    assert page(repo, g.chat_id, after_time=middle + timedelta(hours=1)) == ([], False)
    assert page(repo, g.chat_id, before_time=middle - timedelta(hours=1)) == ([], False)


def test_first_message_after(repo):
    g, owner, m = filled_group(repo, count=3, name="after")
    assert repo.first_message_after(g.chat_id, NO_MESSAGE) == m[0]
    assert repo.first_message_after(g.chat_id, m[0]) == m[1]
    assert repo.first_message_after(g.chat_id, between(m[1], m[2])) == m[2]
    assert repo.first_message_after(g.chat_id, m[2]) is None
    other = repo.create_group("other", "", owner.user_id)
    repo.add_message(other.chat_id, owner.user_id, "later, elsewhere")
    assert repo.first_message_after(g.chat_id, m[2]) is None           # other chats do not count
    empty = repo.create_group("empty", "", owner.user_id)
    assert repo.first_message_after(empty.chat_id, NO_MESSAGE) is None
    assert repo.first_message_after(uuid.uuid4(), NO_MESSAGE) is None


# ---------------------------------------------------------------------- DMs

def test_create_dm_opens_an_accepted_room_once_per_pair(repo):
    a, b, c = make_user(repo, "dm_a"), make_user(repo, "dm_b"), make_user(repo, "dm_c")
    assert repo.find_dm(a.user_id, b.user_id) is None
    dm, created = repo.create_dm(a.user_id, b.user_id)
    assert created is True and isinstance(dm, Chat)
    assert (dm.kind, dm.name, dm.description, dm.created_by, dm.initiator, dm.receiver, dm.status) == \
        ("dm", None, "", a.user_id, a.user_id, b.user_id, "accepted")
    assert repo.get_chat(dm.chat_id) == dm
    assert repo.find_dm(a.user_id, b.user_id) == dm and repo.find_dm(b.user_id, a.user_id) == dm
    assert repo.create_dm(a.user_id, b.user_id) == (dm, False)
    assert repo.create_dm(b.user_id, a.user_id) == (dm, False)
    assert repo.find_dm(a.user_id, c.user_id) is None
    members = repo.get_members(dm.chat_id)
    assert sorted(m.user_id for m in members) == sorted([a.user_id, b.user_id])
    assert members == sorted(members, key=lambda m: (m.joined_at, m.user_id))
    assert all(m.role == "member" and m.last_read_message_id == NO_MESSAGE for m in members)
    assert repo.get_member(dm.chat_id, a.user_id) is not None and repo.get_member(dm.chat_id, c.user_id) is None
    item = repo.get_chat_item(a.user_id, dm.chat_id)
    assert (item.chat, item.role, item.member_count, item.peer, item.unread, item.last_message) == \
        (dm, "member", 2, ref(b), 0, None)
    assert repo.get_chat_item(b.user_id, dm.chat_id).peer == ref(a)
    assert repo.get_chat_item(c.user_id, dm.chat_id) is None
    assert repo.count_pending_requests(a.user_id) == (0, 0)
    assert repo.list_pending_requests(b.user_id) == ([], [])


def test_create_dm_returns_an_existing_pending_dm(repo):
    a, b = make_user(repo, "pend_a"), make_user(repo, "pend_b")
    req = repo.create_request("dm", a.user_id, b.user_id, message="hi")
    dm, created = repo.create_dm(b.user_id, a.user_id)
    assert created is False and dm == repo.get_chat(req.chat_id)
    assert (dm.status, dm.initiator, dm.receiver) == ("pending", a.user_id, b.user_id)
    assert repo.get_member(dm.chat_id, a.user_id) is None              # still only a request


def test_create_dm_with_a_missing_user_raises_not_found(repo):
    a = make_user(repo, "lonely")
    ghost = uuid.uuid4()
    before = table_counts(repo)
    with pytest.raises(NotFoundError):
        repo.create_dm(a.user_id, ghost)
    with pytest.raises(NotFoundError):
        repo.create_dm(ghost, a.user_id)
    assert repo.find_dm(a.user_id, ghost) is None
    assert repo.list_user_chats(a.user_id) == []
    assert table_counts(repo) == before


def test_dm_request_is_a_pending_dm_with_its_first_message(repo):
    a, b = make_user(repo, "req_a"), make_user(repo, "req_b")
    req = repo.create_request("dm", a.user_id, b.user_id, message="hello there")
    assert isinstance(req, ChatRequest)
    assert (req.kind, req.request_id, req.chat_name, req.message, req.status, req.responded_at) == \
        ("dm", req.chat_id, None, "hello there", "pending", None)
    assert (req.sender, req.receiver, req.from_user, req.to_user) == (ref(a), ref(b), a.user_id, b.user_id)
    assert req.created_at.tzinfo is not None
    chat = repo.get_chat(req.chat_id)
    assert (chat.kind, chat.status, chat.initiator, chat.receiver, chat.created_by, chat.name) == \
        ("dm", "pending", a.user_id, b.user_id, a.user_id, None)
    assert repo.find_dm(b.user_id, a.user_id) == chat
    assert repo.get_request(req.request_id) == req
    [first], more = repo.list_messages(req.chat_id, MessageQuery())
    assert (first.sender, first.kind, first.content, more) == (ref(a), "text", "hello there", False)
    assert repo.list_pending_requests(a.user_id) == ([], [req])
    assert repo.list_pending_requests(b.user_id) == ([req], [])
    assert repo.count_pending_requests(a.user_id) == (0, 1)
    assert repo.count_pending_requests(b.user_id) == (1, 0)


def test_pending_dm_is_not_a_chat_yet(repo):
    a, b = make_user(repo, "nochat_a"), make_user(repo, "nochat_b")
    req = repo.create_request("dm", a.user_id, b.user_id, message="are you there?")
    for user in (a, b):
        assert repo.get_member(req.chat_id, user.user_id) is None
        assert repo.get_chat_item(user.user_id, req.chat_id) is None
        assert repo.list_user_chats(user.user_id) == []
    assert repo.get_members(req.chat_id) == []


def test_one_dm_per_pair_in_both_directions_and_any_status(repo):
    a, b, c = make_user(repo, "pair_a"), make_user(repo, "pair_b"), make_user(repo, "pair_c")
    req = repo.create_request("dm", a.user_id, b.user_id, message="first")
    counts = table_counts(repo)
    with pytest.raises(AlreadyRequested):
        repo.create_request("dm", a.user_id, b.user_id, message="second")
    with pytest.raises(AlreadyRequested):
        repo.create_request("dm", b.user_id, a.user_id, message="reverse")
    assert table_counts(repo) == counts                                 # nothing left behind
    assert repo.find_dm(b.user_id, a.user_id).chat_id == req.chat_id
    assert contents(repo, req.chat_id) == (["first"], False)
    assert repo.list_pending_requests(a.user_id) == ([], [req])
    assert repo.list_pending_requests(b.user_id) == ([req], [])
    repo.resolve_request(req.request_id, "accepted", b.user_id)
    for frm, to in ((a, b), (b, a)):                                    # an open room counts too
        with pytest.raises(AlreadyRequested):
            repo.create_request("dm", frm.user_id, to.user_id, message="once more")
    room, _ = repo.create_dm(a.user_id, c.user_id)
    counts = table_counts(repo)
    with pytest.raises(AlreadyRequested):
        repo.create_request("dm", c.user_id, a.user_id, message="hi")
    assert table_counts(repo) == counts
    assert contents(repo, req.chat_id) == (["first"], False)
    assert repo.list_messages(room.chat_id, MessageQuery()) == ([], False)
    assert repo.count_pending_requests(a.user_id) == (0, 0) and repo.count_pending_requests(c.user_id) == (0, 0)


def test_dm_request_to_a_missing_user_raises_not_found(repo):
    a = make_user(repo, "nobody_home")
    before = table_counts(repo)
    with pytest.raises(NotFoundError):
        repo.create_request("dm", a.user_id, uuid.uuid4(), message="hello?")
    with pytest.raises(NotFoundError):
        repo.create_request("dm", uuid.uuid4(), a.user_id, message="boo")
    assert table_counts(repo) == before
    assert repo.list_pending_requests(a.user_id) == ([], [])


def test_dm_accept_by_the_receiver_only(repo):
    a, b, c = make_user(repo, "acc_a"), make_user(repo, "acc_b"), make_user(repo, "acc_c")
    req = repo.create_request("dm", a.user_id, b.user_id, message="hello")
    assert repo.resolve_request(req.request_id, "accepted", a.user_id) is None     # not the initiator
    assert repo.resolve_request(req.request_id, "accepted", c.user_id) is None     # nor a stranger
    assert repo.get_chat(req.chat_id).status == "pending"
    assert repo.get_request(req.request_id) == req
    accepted = repo.resolve_request(req.request_id, "accepted", b.user_id)
    assert (accepted.request_id, accepted.kind, accepted.chat_id, accepted.status, accepted.message) == \
        (req.request_id, "dm", req.chat_id, "accepted", "hello")
    assert (accepted.sender, accepted.receiver) == (ref(a), ref(b))
    assert accepted.responded_at is not None
    chat = repo.get_chat(req.chat_id)
    assert (chat.status, chat.initiator, chat.receiver) == ("accepted", a.user_id, b.user_id)
    assert sorted(m.user_id for m in repo.get_members(req.chat_id)) == sorted([a.user_id, b.user_id])
    for user, peer in ((a, b), (b, a)):
        [item] = repo.list_user_chats(user.user_id)
        assert (item.chat.chat_id, item.peer, item.member_count, item.role) == (req.chat_id, ref(peer), 2, "member")
        assert item.last_message.content == "hello"                    # the first message is in the room
    got = repo.get_request(req.request_id)
    assert (got.status, got.kind, got.chat_id) == ("accepted", "dm", req.chat_id) and got.responded_at is not None
    assert repo.count_pending_requests(a.user_id) == (0, 0) and repo.count_pending_requests(b.user_id) == (0, 0)
    # Answered already: nothing more to resolve.
    assert repo.resolve_request(req.request_id, "accepted", b.user_id) is None
    assert repo.resolve_request(req.request_id, "declined", b.user_id) is None
    assert repo.resolve_request(req.request_id, "cancelled", a.user_id) is None
    assert repo.get_chat(req.chat_id).status == "accepted"
    assert repo.resolve_request(uuid.uuid4(), "accepted", b.user_id) is None


def test_dm_decline_by_the_receiver_only_deletes_it(repo):
    a, b = make_user(repo, "dec_a"), make_user(repo, "dec_b")
    baseline = table_counts(repo)
    req = repo.create_request("dm", a.user_id, b.user_id, message="knock knock")
    assert repo.resolve_request(req.request_id, "declined", a.user_id) is None     # not the initiator
    assert repo.get_chat(req.chat_id) is not None
    declined = repo.resolve_request(req.request_id, "declined", b.user_id)
    assert (declined.request_id, declined.kind, declined.chat_id, declined.status, declined.message) == \
        (req.request_id, "dm", req.chat_id, "declined", "knock knock")
    assert (declined.sender, declined.receiver) == (ref(a), ref(b))
    assert declined.responded_at is not None
    assert repo.get_chat(req.chat_id) is None
    assert repo.get_request(req.request_id) is None
    assert repo.find_dm(a.user_id, b.user_id) is None
    assert repo.list_messages(req.chat_id, MessageQuery()) == ([], False)     # the first message went too
    assert repo.count_pending_requests(a.user_id) == (0, 0) and repo.count_pending_requests(b.user_id) == (0, 0)
    assert table_counts(repo) == baseline
    assert repo.resolve_request(req.request_id, "declined", b.user_id) is None
    again = repo.create_request("dm", b.user_id, a.user_id, message="sorry, wrong button")
    assert again.chat_id != req.chat_id and again.sender == ref(b)


def test_dm_cancel_by_the_initiator_only_deletes_it(repo):
    a, b = make_user(repo, "can_a"), make_user(repo, "can_b")
    baseline = table_counts(repo)
    req = repo.create_request("dm", a.user_id, b.user_id, message="never mind")
    assert repo.resolve_request(req.request_id, "cancelled", b.user_id) is None    # not the receiver
    assert repo.get_chat(req.chat_id) is not None
    cancelled = repo.resolve_request(req.request_id, "cancelled", a.user_id)
    assert (cancelled.request_id, cancelled.kind, cancelled.status, cancelled.message) == \
        (req.request_id, "dm", "cancelled", "never mind")
    assert (cancelled.sender, cancelled.receiver) == (ref(a), ref(b))
    assert cancelled.responded_at is not None
    assert repo.get_chat(req.chat_id) is None
    assert repo.get_request(req.request_id) is None
    assert repo.find_dm(b.user_id, a.user_id) is None
    assert repo.list_messages(req.chat_id, MessageQuery()) == ([], False)
    assert repo.list_pending_requests(b.user_id) == ([], [])
    assert table_counts(repo) == baseline
    assert repo.resolve_request(req.request_id, "cancelled", a.user_id) is None
    assert repo.create_request("dm", a.user_id, b.user_id, message="second try").status == "pending"


def test_crossing_dm_requests_leave_one_dm(repo):
    for n in range(4):
        a, b = make_user(repo, f"cross_a{n}"), make_user(repo, f"cross_b{n}")
        results = run_parallel(lambda frm, to, text: repo.create_request("dm", frm, to, message=text),
                               [(a.user_id, b.user_id, "from a"), (b.user_id, a.user_id, "from b")])
        made = [r for r in results if isinstance(r, ChatRequest)]
        assert len(made) == 1 and sum(isinstance(r, AlreadyRequested) for r in results) == 1, results
        dm = repo.find_dm(a.user_id, b.user_id)
        assert (dm.chat_id, dm.status) == (made[0].chat_id, "pending")
        assert contents(repo, dm.chat_id) == ([made[0].message], False)
    assert table_counts(repo) in (None, (4, 0, 4, 0, 4))


def test_parallel_create_dm_makes_one_room(repo):
    for n in range(4):
        a, b = make_user(repo, f"both_a{n}"), make_user(repo, f"both_b{n}")
        results = run_parallel(repo.create_dm, [(a.user_id, b.user_id), (b.user_id, a.user_id)])
        assert all(isinstance(r, tuple) for r in results), results
        (first, created1), (second, created2) = results
        assert first == second and first.status == "accepted"
        assert sorted([created1, created2]) == [False, True]
    assert table_counts(repo) in (None, (4, 0, 4, 0, 0))


# ---------------------------------------------------------------------- group invites

def test_invite_is_a_pending_membership(repo):
    owner, invitee = make_user(repo, "inv_owner"), make_user(repo, "inv_guest")
    g = repo.create_group("Club", "", owner.user_id)
    invite = repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=g.chat_id)
    assert isinstance(invite, ChatRequest)
    assert (invite.kind, invite.chat_id, invite.chat_name, invite.message, invite.status, invite.responded_at) == \
        ("invite", g.chat_id, "Club", None, "pending", None)
    assert (invite.sender, invite.receiver) == (ref(owner), ref(invitee))
    assert invite.request_id != g.chat_id
    assert repo.get_request(invite.request_id) == invite
    assert repo.get_member(g.chat_id, invitee.user_id) is None
    assert [m.user_id for m in repo.get_members(g.chat_id)] == [owner.user_id]
    assert repo.get_chat_item(invitee.user_id, g.chat_id) is None
    assert repo.list_user_chats(invitee.user_id) == []
    assert repo.get_chat_item(owner.user_id, g.chat_id).member_count == 1
    assert repo.list_pending_requests(invitee.user_id) == ([invite], [])
    assert repo.list_pending_requests(owner.user_id) == ([], [invite])
    assert repo.count_pending_requests(invitee.user_id) == (1, 0)
    assert repo.count_pending_requests(owner.user_id) == (0, 1)


def test_invite_refuses_members_repeats_and_missing_rows(repo):
    owner, member, invitee = make_user(repo, "ir_owner"), make_user(repo, "ir_member"), make_user(repo, "ir_guest")
    other = make_user(repo, "ir_other")
    g = repo.create_group("strict", "", owner.user_id)
    repo.join_group(g.chat_id, member.user_id)
    with pytest.raises(AlreadyMember):
        repo.create_request("invite", owner.user_id, member.user_id, chat_id=g.chat_id)
    with pytest.raises(AlreadyMember):
        repo.create_request("invite", member.user_id, owner.user_id, chat_id=g.chat_id)
    invite = repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=g.chat_id)
    with pytest.raises(AlreadyRequested):
        repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=g.chat_id)
    with pytest.raises(AlreadyRequested):                               # whoever invites
        repo.create_request("invite", member.user_id, invitee.user_id, chat_id=g.chat_id)
    with pytest.raises(NotFoundError):
        repo.create_request("invite", owner.user_id, uuid.uuid4(), chat_id=g.chat_id)
    with pytest.raises(NotFoundError):                                  # the inviter is gone
        repo.create_request("invite", uuid.uuid4(), other.user_id, chat_id=g.chat_id)
    with pytest.raises(NotFoundError):
        repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=uuid.uuid4())
    assert repo.list_pending_invites(g.chat_id) == ([invite], 1)
    assert [m.user_id for m in repo.get_members(g.chat_id)] == [owner.user_id, member.user_id]


def test_invite_accept_by_the_invitee_only(repo):
    owner, invitee, other = make_user(repo, "ia_owner"), make_user(repo, "ia_guest"), make_user(repo, "ia_other")
    g = repo.create_group("accepting", "", owner.user_id)
    repo.join_group(g.chat_id, other.user_id)
    newest = repo.add_message(g.chat_id, owner.user_id, "before you came")
    invite = repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=g.chat_id)
    assert repo.resolve_request(invite.request_id, "accepted", owner.user_id) is None   # the inviter cannot
    assert repo.resolve_request(invite.request_id, "accepted", other.user_id) is None
    assert repo.get_member(g.chat_id, invitee.user_id) is None
    accepted = repo.resolve_request(invite.request_id, "accepted", invitee.user_id)
    assert (accepted.request_id, accepted.kind, accepted.chat_id, accepted.chat_name, accepted.status) == \
        (invite.request_id, "invite", g.chat_id, "accepting", "accepted")
    assert (accepted.sender, accepted.receiver) == (ref(owner), ref(invitee))
    assert accepted.responded_at is not None
    member = repo.get_member(g.chat_id, invitee.user_id)
    assert (member.role, member.last_read_message_id) == ("member", newest.message_id)   # read up to now
    item = repo.get_chat_item(invitee.user_id, g.chat_id)
    assert (item.unread, item.member_count, item.role) == (0, 3, "member")
    repo.add_message(g.chat_id, owner.user_id, "welcome")
    assert repo.get_chat_item(invitee.user_id, g.chat_id).unread == 1
    assert repo.get_request(invite.request_id).status == "accepted"
    assert repo.count_pending_requests(invitee.user_id) == (0, 0)
    assert repo.count_pending_requests(owner.user_id) == (0, 0)
    assert repo.resolve_request(invite.request_id, "accepted", invitee.user_id) is None
    assert repo.resolve_request(invite.request_id, "declined", invitee.user_id) is None
    assert repo.resolve_request(invite.request_id, "cancelled", owner.user_id) is None
    assert repo.get_member(g.chat_id, invitee.user_id) is not None


def test_invite_cancel_by_the_inviter_only(repo):
    owner, inviter, invitee = make_user(repo, "ic_owner"), make_user(repo, "ic_inviter"), make_user(repo, "ic_guest")
    g = repo.create_group("cancelling", "", owner.user_id)
    repo.join_group(g.chat_id, inviter.user_id)
    invite = repo.create_request("invite", inviter.user_id, invitee.user_id, chat_id=g.chat_id)
    assert repo.resolve_request(invite.request_id, "cancelled", invitee.user_id) is None   # not the invitee
    assert repo.resolve_request(invite.request_id, "cancelled", owner.user_id) is None     # nor another member
    cancelled = repo.resolve_request(invite.request_id, "cancelled", inviter.user_id)
    assert (cancelled.request_id, cancelled.kind, cancelled.status, cancelled.chat_id) == \
        (invite.request_id, "invite", "cancelled", g.chat_id)
    assert (cancelled.sender, cancelled.receiver) == (ref(inviter), ref(invitee))
    assert cancelled.responded_at is not None
    assert repo.get_request(invite.request_id) is None
    assert repo.list_pending_invites(g.chat_id) == ([], 0)
    assert repo.get_member(g.chat_id, invitee.user_id) is None
    assert repo.list_pending_requests(invitee.user_id) == ([], [])
    assert repo.resolve_request(invite.request_id, "cancelled", inviter.user_id) is None
    assert repo.get_chat(g.chat_id) is not None


def test_invite_decline_deletes_the_row(repo):
    owner, invitee = make_user(repo, "id_owner"), make_user(repo, "id_guest")
    g = repo.create_group("declining", "", owner.user_id)
    invite = repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=g.chat_id)
    assert repo.resolve_request(invite.request_id, "declined", owner.user_id) is None      # not the inviter
    declined = repo.resolve_request(invite.request_id, "declined", invitee.user_id)
    assert (declined.request_id, declined.kind, declined.status, declined.chat_id) == \
        (invite.request_id, "invite", "declined", g.chat_id)
    assert (declined.sender, declined.receiver) == (ref(owner), ref(invitee))
    assert declined.responded_at is not None
    assert repo.get_request(invite.request_id) is None
    assert repo.list_pending_requests(invitee.user_id) == ([], [])
    assert repo.list_pending_invites(g.chat_id) == ([], 0)
    assert repo.get_member(g.chat_id, invitee.user_id) is None
    assert repo.resolve_request(invite.request_id, "declined", invitee.user_id) is None
    again = repo.create_request("invite", owner.user_id, invitee.user_id, chat_id=g.chat_id)  # a fresh invite
    assert again.status == "pending" and again.request_id != invite.request_id


def test_get_request_unknown_ids(repo):
    owner = make_user(repo, "gr_owner")
    g = repo.create_group("not a request", "", owner.user_id)
    assert repo.get_request(uuid.uuid4()) is None
    assert repo.get_request(g.chat_id) is None                          # a group is no DM request


# ---------------------------------------------------------------------- listings

def test_list_user_chats_latest_activity_roles_peer_and_counts(repo):
    me, x, y = make_user(repo, "lc_me"), make_user(repo, "lc_x"), make_user(repo, "lc_y")
    mine = repo.create_group("mine", "", me.user_id)
    theirs = repo.create_group("theirs", "", x.user_id)
    repo.join_group(theirs.chat_id, me.user_id)
    repo.join_group(theirs.chat_id, y.user_id)
    dm, _ = repo.create_dm(y.user_id, me.user_id)
    # Not chats of mine (yet): a DM request to me and an invite for me.
    repo.create_request("dm", x.user_id, me.user_id, message="let's talk")
    elsewhere = repo.create_group("elsewhere", "", y.user_id)
    repo.create_request("invite", y.user_id, me.user_id, chat_id=elsewhere.chat_id)
    repo.add_message(elsewhere.chat_id, y.user_id, "you are not in here")

    repo.add_message(mine.chat_id, me.user_id, "a")
    time.sleep(TICK)
    last_dm = repo.add_message(dm.chat_id, y.user_id, "b")
    time.sleep(TICK)
    last_theirs = repo.add_message(theirs.chat_id, x.user_id, "c")
    time.sleep(TICK)
    assert chat_ids(repo.list_user_chats(me.user_id)) == [theirs.chat_id, dm.chat_id, mine.chat_id]
    last_mine = repo.add_message(mine.chat_id, me.user_id, "d")
    items = repo.list_user_chats(me.user_id)
    assert chat_ids(items) == [mine.chat_id, theirs.chat_id, dm.chat_id]
    assert all(isinstance(i, ChatListItem) for i in items)
    m, t, d = items
    assert (m.role, t.role, d.role) == ("owner", "member", "member")
    assert (m.member_count, t.member_count, d.member_count) == (1, 3, 2)
    assert (m.peer, t.peer, d.peer) == (None, None, ref(y))
    assert (m.unread, t.unread, d.unread) == (0, 1, 1)
    assert (m.last_message, t.last_message, d.last_message) == (last_mine, last_theirs, last_dm)
    assert (m.chat, t.chat, d.chat) == (repo.get_chat(mine.chat_id), repo.get_chat(theirs.chat_id), dm)
    assert all(i.last_read_message_id == NO_MESSAGE for i in items)
    for item in items:
        assert repo.get_chat_item(me.user_id, item.chat.chat_id) == item
    assert repo.get_chat_item(y.user_id, dm.chat_id).peer == ref(me)
    assert chat_ids(repo.list_user_chats(y.user_id)) == [theirs.chat_id, dm.chat_id, elsewhere.chat_id]


def test_list_user_chats_counts_creation_as_activity(repo):
    me = make_user(repo, "act_me")
    old = repo.create_group("old", "", me.user_id)
    repo.add_message(old.chat_id, me.user_id, "first")
    time.sleep(0.02)
    new = repo.create_group("new", "", me.user_id)
    items = repo.list_user_chats(me.user_id)
    assert chat_ids(items) == [new.chat_id, old.chat_id]
    assert (items[0].last_message, items[0].unread) == (None, 0)
    time.sleep(0.02)
    repo.add_message(old.chat_id, me.user_id, "second")
    assert chat_ids(repo.list_user_chats(me.user_id)) == [old.chat_id, new.chat_id]


def test_get_chat_item_is_the_list_row_or_none(repo):
    me, other, invitee = make_user(repo, "ci_me"), make_user(repo, "ci_other"), make_user(repo, "ci_inv")
    g = repo.create_group("item", "", me.user_id)
    repo.join_group(g.chat_id, other.user_id)
    repo.add_message(g.chat_id, other.user_id, "hello")
    dm, _ = repo.create_dm(me.user_id, other.user_id)
    repo.create_request("invite", me.user_id, invitee.user_id, chat_id=g.chat_id)
    rows = {i.chat.chat_id: i for i in repo.list_user_chats(me.user_id)}
    assert set(rows) == {g.chat_id, dm.chat_id}
    assert repo.get_chat_item(me.user_id, g.chat_id) == rows[g.chat_id]
    assert repo.get_chat_item(me.user_id, dm.chat_id) == rows[dm.chat_id]
    assert repo.get_chat_item(invitee.user_id, g.chat_id) is None       # invited only
    assert repo.get_chat_item(invitee.user_id, dm.chat_id) is None
    assert repo.get_chat_item(me.user_id, uuid.uuid4()) is None


def test_list_pending_requests_incoming_and_outgoing_of_both_kinds(repo):
    me, a, b, c, d = (make_user(repo, n) for n in ("pr_me", "pr_a", "pr_b", "pr_c", "pr_d"))
    group_a = repo.create_group("a's group", "", a.user_id)
    my_group = repo.create_group("my group", "", me.user_id)
    r1 = repo.create_request("dm", a.user_id, me.user_id, message="hi me")
    time.sleep(TICK)
    r2 = repo.create_request("invite", a.user_id, me.user_id, chat_id=group_a.chat_id)
    time.sleep(TICK)
    r3 = repo.create_request("dm", me.user_id, b.user_id, message="hi b")
    time.sleep(TICK)
    r4 = repo.create_request("invite", me.user_id, c.user_id, chat_id=my_group.chat_id)
    time.sleep(TICK)
    r5 = repo.create_request("dm", d.user_id, me.user_id, message="hey")
    time.sleep(TICK)
    r6 = repo.create_request("invite", me.user_id, d.user_id, chat_id=my_group.chat_id)
    repo.create_request("dm", a.user_id, b.user_id, message="not about me")
    repo.create_dm(me.user_id, c.user_id)                               # an open room is no request
    incoming, outgoing = repo.list_pending_requests(me.user_id)
    assert request_ids(incoming) == [r1.request_id, r2.request_id, r5.request_id]
    assert request_ids(outgoing) == [r3.request_id, r4.request_id, r6.request_id]
    assert (incoming, outgoing) == ([r1, r2, r5], [r3, r4, r6])
    assert repo.count_pending_requests(me.user_id) == (3, 3)
    assert repo.count_pending_requests(b.user_id) == (2, 0)
    assert repo.count_pending_requests(a.user_id) == (0, 3)
    repo.resolve_request(r1.request_id, "accepted", me.user_id)
    repo.resolve_request(r4.request_id, "cancelled", me.user_id)
    repo.resolve_request(r5.request_id, "declined", me.user_id)
    incoming, outgoing = repo.list_pending_requests(me.user_id)
    assert (request_ids(incoming), request_ids(outgoing)) == ([r2.request_id], [r3.request_id, r6.request_id])
    assert repo.count_pending_requests(me.user_id) == (1, 2)
    assert repo.list_pending_requests(make_user(repo, "pr_none").user_id) == ([], [])


def test_list_pending_invites_with_total(repo):
    owner, member = make_user(repo, "pi_owner"), make_user(repo, "pi_member")
    guests = [make_user(repo, f"pi_guest{i}") for i in range(4)]
    g = repo.create_group("invites", "", owner.user_id)
    other = repo.create_group("other", "", owner.user_id)
    repo.join_group(g.chat_id, member.user_id)
    repo.create_request("invite", owner.user_id, guests[0].user_id, chat_id=other.chat_id)   # another group
    inv1 = repo.create_request("invite", owner.user_id, guests[0].user_id, chat_id=g.chat_id)
    time.sleep(TICK)
    inv2 = repo.create_request("invite", member.user_id, guests[1].user_id, chat_id=g.chat_id)
    time.sleep(TICK)
    inv3 = repo.create_request("invite", owner.user_id, guests[2].user_id, chat_id=g.chat_id)
    time.sleep(TICK)
    inv4 = repo.create_request("invite", member.user_id, guests[3].user_id, chat_id=g.chat_id)
    assert repo.list_pending_invites(g.chat_id) == ([inv1, inv2, inv3, inv4], 4)
    assert repo.list_pending_invites(g.chat_id, limit=2) == ([inv1, inv2], 4)
    invites, total = repo.list_pending_invites(g.chat_id)
    assert invites[1].sender == ref(member) and invites[1].receiver == ref(guests[1])
    assert all(i.kind == "invite" and i.chat_name == "invites" and i.status == "pending" for i in invites)
    repo.join_group(g.chat_id, guests[1].user_id)                       # settles inv2
    repo.resolve_request(inv3.request_id, "declined", guests[2].user_id)
    repo.resolve_request(inv4.request_id, "cancelled", member.user_id)
    assert repo.list_pending_invites(g.chat_id) == ([inv1], 1)
    assert repo.list_pending_invites(g.chat_id, limit=1) == ([inv1], 1)
    assert repo.list_pending_invites(other.chat_id)[1] == 1
    assert repo.list_pending_invites(uuid.uuid4()) == ([], 0)


def test_list_member_groups_by_name_with_roles_and_counts(repo):
    me, x, y = make_user(repo, "mg_me"), make_user(repo, "mg_x"), make_user(repo, "mg_y")
    zeta = repo.create_group("zeta", "", x.user_id)
    alpha = repo.create_group("Alpha", "the first", me.user_id)
    beta = repo.create_group("beta", "", x.user_id)
    thai = repo.create_group("กลุ่มเรียน", "", y.user_id)
    repo.create_group("not mine", "", y.user_id)
    invited = repo.create_group("invited only", "", y.user_id)
    dup1 = repo.create_group("dup", "", me.user_id)
    dup2 = repo.create_group("DUP", "", me.user_id)
    for g in (zeta, beta, thai):
        repo.join_group(g.chat_id, me.user_id)
    repo.join_group(beta.chat_id, y.user_id)
    repo.create_request("invite", y.user_id, me.user_id, chat_id=invited.chat_id)   # pending: not a group of mine
    repo.create_dm(me.user_id, x.user_id)                                            # a DM is no group

    rows = repo.list_member_groups(me.user_id)
    assert all(isinstance(r, MemberGroup) for r in rows)
    dups = sorted([dup1.chat_id, dup2.chat_id])
    assert [r.chat.chat_id for r in rows] == [alpha.chat_id, beta.chat_id, *dups, zeta.chat_id, thai.chat_id]
    by_id = {r.chat.chat_id: r for r in rows}
    assert by_id[alpha.chat_id].chat == repo.get_chat(alpha.chat_id)
    assert [(by_id[g.chat_id].role, by_id[g.chat_id].member_count) for g in (alpha, beta, zeta, thai)] == \
        [("owner", 1), ("member", 3), ("member", 2), ("member", 2)]
    assert by_id[beta.chat_id].joined_at == repo.get_member(beta.chat_id, me.user_id).joined_at
    assert not any(r.viewer_is_member for r in rows)                                  # no viewer given
    seen_by_x = {r.chat.chat_id: r.viewer_is_member for r in repo.list_member_groups(me.user_id, x.user_id)}
    assert seen_by_x == {alpha.chat_id: False, beta.chat_id: True, dup1.chat_id: False, dup2.chat_id: False,
                         zeta.chat_id: True, thai.chat_id: False}
    assert all(r.viewer_is_member for r in repo.list_member_groups(me.user_id, me.user_id))
    assert [r.chat.chat_id for r in repo.list_member_groups(x.user_id)] == [beta.chat_id, zeta.chat_id]
    assert repo.list_member_groups(uuid.uuid4()) == []
    repo.remove_member(zeta.chat_id, me.user_id)
    assert zeta.chat_id not in {r.chat.chat_id for r in repo.list_member_groups(me.user_id)}
    repo.update_profile(x.user_id, None, None, username="mg_x_renamed")                # rows follow the user id
    assert [r.chat.chat_id for r in repo.list_member_groups(x.user_id)] == [beta.chat_id, zeta.chat_id]


def test_find_invitees_online_people_and_dm_peers_minus_the_group(repo):
    me = make_user(repo, "inv_me")
    on1, on2 = make_user(repo, "inv_online1"), make_user(repo, "Inv_Online2")
    peer = make_user(repo, "inv_peer")                   # offline, an open DM with me (they started it)
    pending_peer = make_user(repo, "inv_pending")        # offline, only a DM request
    stranger = make_user(repo, "inv_stranger")           # offline, no DM with me
    member = make_user(repo, "inv_member")               # online, already in the group
    invited = make_user(repo, "inv_invited")             # online, already invited
    other_dm = make_user(repo, "inv_otherdm")            # offline, a DM with someone else
    g = repo.create_group("invitees", "", me.user_id)
    repo.join_group(g.chat_id, member.user_id)
    invite = repo.create_request("invite", member.user_id, invited.user_id, chat_id=g.chat_id)
    repo.create_dm(peer.user_id, me.user_id)
    repo.create_request("dm", me.user_id, pending_peer.user_id, message="hi")
    repo.create_dm(other_dm.user_id, stranger.user_id)
    online = [me.user_id, on1.user_id, on2.user_id, member.user_id, invited.user_id, uuid.uuid4()]

    def names(*args, **kw) -> List[str]:
        return [u.username for u in repo.find_invitees(g.chat_id, me.user_id, *args, **kw)]

    found = repo.find_invitees(g.chat_id, me.user_id, online)
    assert [u.username for u in found] == ["inv_online1", "Inv_Online2", "inv_peer"]
    assert all(isinstance(u, User) for u in found) and found[2] == repo.get_user(peer.user_id)
    assert names(online, "ONLINE") == ["inv_online1", "Inv_Online2"]          # ignoring case
    assert names(online, "line2") == ["Inv_Online2"]                          # anywhere in the name
    assert names(online, "_peer") == ["inv_peer"]                             # "_" is no wildcard
    assert names(online, "%") == [] and names(online, "zzz") == []
    assert names(online, limit=2) == ["inv_online1", "Inv_Online2"]
    assert names(online, limit=0) == []
    assert names([]) == ["inv_peer"]                                          # nobody online: DM peers only
    assert names(iter(online)) == ["inv_online1", "Inv_Online2", "inv_peer"]  # any iterable
    repo.resolve_request(invite.request_id, "declined", invited.user_id)      # declined: may be invited again
    repo.join_group(g.chat_id, on1.user_id)                                   # joined: not any more
    assert names(online) == ["inv_invited", "Inv_Online2", "inv_peer"]
    # For another member the DM peers are their own; the group's members stay out.
    assert [u.username for u in repo.find_invitees(g.chat_id, member.user_id, online)] == \
        ["inv_invited", "Inv_Online2"]


def test_get_pending_invite_by_group_and_user(repo):
    owner, guest = make_user(repo, "gpi_owner"), make_user(repo, "gpi_guest")
    g = repo.create_group("pending", "", owner.user_id)
    assert repo.get_pending_invite(g.chat_id, guest.user_id) is None
    invite = repo.create_request("invite", owner.user_id, guest.user_id, chat_id=g.chat_id)
    assert repo.get_pending_invite(g.chat_id, guest.user_id) == invite
    assert repo.get_pending_invite(g.chat_id, owner.user_id) is None          # a member, not invited
    assert repo.get_pending_invite(uuid.uuid4(), guest.user_id) is None
    repo.resolve_request(invite.request_id, "accepted", guest.user_id)
    assert repo.get_pending_invite(g.chat_id, guest.user_id) is None          # accepted: nothing pending
    dm, _ = repo.create_dm(owner.user_id, guest.user_id)
    assert repo.get_pending_invite(dm.chat_id, guest.user_id) is None
