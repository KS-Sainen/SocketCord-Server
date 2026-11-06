"""Chat rooms: list, create/join/leave groups, read and send messages, invite."""
from __future__ import annotations

import uuid
from uuid import UUID

from ..db.base import AlreadyMember, AlreadyRequested, MessageQuery, NotFoundError
from ..protocol import GET, POST, ReplyCord
from ..router import RequestContext, bad_request, forbidden, not_found, ok
from ..views import chat_item_view, group_view, marker, message_view, ref_of, request_view
from . import router
from .common import (MAX_MESSAGE_LEN, after_join, announce_membership, announce_pending, announce_request,
                     chat_info_for, chat_item,
                     check_request_limits, get_chat_or_404, group_item, hide_foreign_dm, limit, post_message,
                     require_member)

GROUP_PAGE_DEFAULT, GROUP_PAGE_MAX = 100, 200
CHAT_PAGE_MAX = 200
MEMBERS_IN_GROUP_LIST = 100     # per group in GET chats/groups (member_count is always exact)
SEARCH_MAX_LEN = 50


@router.route(GET, "chats")
def my_chats(ctx: RequestContext) -> ReplyCord:
    """Every chat the caller is in, most recent activity first, with unread counts.
    Paged with `count` (default and max 200) and `offset`."""
    count = ctx.int_field("count", required=False, minimum=1, maximum=CHAT_PAGE_MAX, default=CHAT_PAGE_MAX)
    offset = ctx.int_field("offset", required=False, minimum=0, maximum=10_000_000, default=0)
    items = ctx.repo.list_user_chats(ctx.user.user_id)
    page = items[offset:offset + count]
    return ok({"chats": [chat_item_view(i, ctx.hub) for i in page], "offset": offset,
               "has_more": offset + count < len(items)})


@router.route(POST, "chats")
def create_group(ctx: RequestContext) -> ReplyCord:
    """R8: a new group starts with only its creator as a member."""
    name = ctx.str_field("name", max_len=64)
    description = ctx.str_field("description", required=False, min_len=0, max_len=500, default="",
                                multiline=True)
    # Every new group is pushed to everyone online, so creating them is rate-limited per account.
    limit(ctx, ctx.app.group_limiter, "creating groups")
    # The id is chosen first so the chat's lock covers the insert AND the push: a join
    # of the new group cannot announce itself before group_created does.
    chat_id = uuid.uuid4()
    with ctx.app.chat_lock(chat_id):
        chat = ctx.repo.create_group(name, description, ctx.user.user_id, chat_id=chat_id)
        view = group_view(group_item(ctx, chat), ctx.hub, ctx.user.user_id)
        ctx.hub.push_all(ReplyCord.event("group_created",
                                         {"group": {k: v for k, v in view.items() if k != "joined"}}),
                         exclude_conn=ctx.peer.conn_id)
    # "chat" has the GET chats shape (for the chat list); "group" the GET chats/groups shape.
    return ok({"chat": chat_item(ctx, ctx.user.user_id, chat.chat_id), "group": view})


@router.route(GET, "chats/groups")
def all_groups(ctx: RequestContext) -> ReplyCord:
    """R9: every group on the server, with its member list, whoever created it.
    Paged (oldest first) so the reply always fits in one packet: `count`, `offset`."""
    me = ctx.user.user_id
    count = ctx.int_field("count", required=False, minimum=1, maximum=GROUP_PAGE_MAX, default=GROUP_PAGE_DEFAULT)
    offset = ctx.int_field("offset", required=False, minimum=0, maximum=10_000_000, default=0)
    groups, has_more = ctx.repo.list_groups(offset=offset, limit=count)
    return ok({"groups": [group_view(g, ctx.hub, me, max_members=MEMBERS_IN_GROUP_LIST) for g in groups],
               "offset": offset, "has_more": has_more})


@router.route(GET, "chat/{chat_id:uuid}/info")
def chat_info(ctx: RequestContext, chat_id: UUID) -> ReplyCord:
    chat = get_chat_or_404(ctx, chat_id)
    hide_foreign_dm(ctx, chat)
    return ok({"chat": chat_info_for(ctx, chat)})


def _message_query(ctx: RequestContext) -> MessageQuery:
    body = ctx.body
    q = MessageQuery(
        count=ctx.int_field("count", required=False, minimum=1, maximum=200, default=50),
        before_id=ctx.uuid_field("before_id", required=False),
        after_id=ctx.uuid_field("after_id", required=False),
        content=ctx.str_field("content", required=False, max_len=SEARCH_MAX_LEN),
    )
    # README filter "time": a timestamp (= messages before it) or {"before": t, "after": t}.
    t = body.get("time")
    if isinstance(t, dict):
        unknown = set(t) - {"before", "after"}
        if unknown:
            raise bad_request("INVALID_FIELD", "'time' only takes 'before' and 'after'", field="time")
        if t.get("before") is not None:
            q.before_time = ctx.time_field(t["before"], "time.before")
        if t.get("after") is not None:
            q.after_time = ctx.time_field(t["after"], "time.after")
    elif t is not None:
        q.before_time = ctx.time_field(t, "time")
    return q


@router.route(GET, "chat/{chat_id:uuid}/messages")
def chat_messages(ctx: RequestContext, chat_id: UUID) -> ReplyCord:
    """Recent messages, oldest first. Only members can read them (R7/R11)."""
    chat = get_chat_or_404(ctx, chat_id)
    member = require_member(ctx, chat)
    messages, has_more = ctx.repo.list_messages(chat_id, _message_query(ctx))
    # The GUI draws its NEW line above this message (it may be older than this page).
    first_unread = ctx.repo.first_message_after(chat_id, member.last_read_message_id)
    return ok({"chat_id": chat_id, "messages": [message_view(m) for m in messages], "has_more": has_more,
               "last_read_message_id": marker(member.last_read_message_id),
               "first_unread_message_id": first_unread})


@router.route(POST, "chat/{chat_id:uuid}/message")
def send_message(ctx: RequestContext, chat_id: UUID) -> ReplyCord:
    """R7/R11: post to a chat you are in; every online member gets event/message."""
    chat = get_chat_or_404(ctx, chat_id)
    require_member(ctx, chat)
    content = ctx.str_field("content", max_len=MAX_MESSAGE_LEN, strip=False, multiline=True)
    if not content.strip():
        raise bad_request("INVALID_FIELD", "'content' cannot be blank", field="content")
    limit(ctx, ctx.app.message_limiter, "sending messages")
    msg = post_message(ctx, chat, content, ctx.user.user_id)
    return ok({"message": message_view(msg)})


@router.route(POST, "chat/{chat_id:uuid}/read")
def mark_read(ctx: RequestContext, chat_id: UUID) -> ReplyCord:
    chat = get_chat_or_404(ctx, chat_id)
    require_member(ctx, chat)
    message_id = ctx.uuid_field("message_id", required=False)
    last = ctx.repo.mark_read(chat_id, ctx.user.user_id, message_id)
    return ok({"chat_id": chat_id, "last_read_message_id": marker(last)})


@router.route(POST, "chat/{chat_id:uuid}/invite")
def invite(ctx: RequestContext, chat_id: UUID) -> ReplyCord:
    """Invite someone to a group. They are added only if they accept (R10)."""
    chat = get_chat_or_404(ctx, chat_id)
    hide_foreign_dm(ctx, chat)
    if chat.kind != "group":
        raise forbidden("NOT_A_GROUP", "only groups take invites")
    require_member(ctx, chat)
    target_id = ctx.uuid_field("user_id")
    if target_id == ctx.user.user_id:
        raise bad_request("INVALID_TARGET", "you are already in this group")
    target = ctx.repo.get_user(target_id)
    if target is None:
        raise not_found("USER_NOT_FOUND", "no such user")
    if ctx.repo.get_member(chat_id, target_id) is not None:
        raise forbidden("ALREADY_MEMBER", f"{target.username} is already in this group")
    check_request_limits(ctx, target_id)
    try:
        req = ctx.repo.create_request("invite", ctx.user.user_id, target_id, chat_id=chat_id)
    except AlreadyMember:
        raise forbidden("ALREADY_MEMBER", f"{target.username} is already in this group") from None
    except AlreadyRequested:
        raise forbidden("ALREADY_INVITED", f"{target.username} already has a pending invite") from None
    announce_pending(ctx, req)
    return ok({"status": "pending", "request": request_view(req)})


@router.route(POST, "chat/{chat_id:uuid}/join")
def join(ctx: RequestContext, chat_id: UUID) -> ReplyCord:
    """R10: a client joins a group by itself."""
    chat = get_chat_or_404(ctx, chat_id)
    hide_foreign_dm(ctx, chat)
    if chat.kind != "group":
        raise forbidden("NOT_A_GROUP", "DM rooms cannot be joined")
    if ctx.repo.get_member(chat_id, ctx.user.user_id) is not None:
        raise forbidden("ALREADY_MEMBER", "you are already in this group")
    # Every join is announced to everyone online and written to the history.
    limit(ctx, ctx.app.membership_limiter, "joining and leaving groups")
    with ctx.app.chat_lock(chat_id):
        # A pending invite of yours is settled by joining (R10: you join by yourself).
        joined = ctx.repo.join_group(chat_id, ctx.user.user_id)
        if joined is None:
            raise forbidden("ALREADY_MEMBER", "you are already in this group")
        after_join(ctx, chat, ctx.user)
    if joined.settled is not None:
        announce_request(ctx, joined.settled, joined.settled.from_user)
    return ok({"chat": chat_item(ctx, ctx.user.user_id, chat_id), "info": chat_info_for(ctx, chat)})


@router.route(POST, "chat/{chat_id:uuid}/leave")
def leave(ctx: RequestContext, chat_id: UUID) -> ReplyCord:
    chat = get_chat_or_404(ctx, chat_id)
    hide_foreign_dm(ctx, chat)
    if chat.kind != "group":
        raise forbidden("NOT_A_GROUP", "DM rooms cannot be left")
    if ctx.repo.get_member(chat_id, ctx.user.user_id) is None:
        raise forbidden("NOT_A_MEMBER", "you are not in this group")
    limit(ctx, ctx.app.membership_limiter, "joining and leaving groups")
    with ctx.app.chat_lock(chat_id):
        removed, deleted = ctx.repo.remove_member(chat_id, ctx.user.user_id)
        if not removed:
            raise forbidden("NOT_A_MEMBER", "you are not in this group")
        if deleted:
            # The last member left, so the group is gone.
            ctx.hub.push_all(ReplyCord.event("group_deleted", {"chat_id": chat_id}))
        else:
            try:
                post_message(ctx, chat, f"{ctx.user.username} left the group", sender_id=None,
                             kind="system", exclude_origin=False)
                announce_membership(ctx, chat, ref_of(ctx.user), "left")
            except NotFoundError:
                pass  # deleted by a concurrent change; that request announced it
    return ok({"chat_id": chat_id, "left": True, "chat_deleted": deleted})
