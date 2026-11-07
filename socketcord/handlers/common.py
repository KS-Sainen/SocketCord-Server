"""Steps shared by several handlers: posting a message, opening a DM, joining a group.

Anything that changes a chat and then tells people about it runs under that
chat's lock (ctx.app.chat_lock), so pushes for one chat leave the server in the
same order the changes were committed.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from ..db.base import Chat, ChatRequest, GroupListItem, Member, Message, MessageQuery, UserRef, UUID
from ..protocol import ReplyCord
from ..router import RequestContext, bad_request, forbidden, not_found
from ..views import (chat_info_view, chat_item_view, invited_chat_view, message_view, ref_of,
                     request_view, user_ref)

MAX_MESSAGE_LEN = 4000
MEMBERS_IN_CHAT_INFO = 500      # members (and pending invites, shown to members) in a chat info object
PENDING_ROWS_MAX = 50           # invitations shown as pending rows in GET chats
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def spelled(ctx: RequestContext, primary: str, *aliases: str) -> str:
    """The name the caller used for a field that has more than one accepted spelling
    (the reply's word and the older one). `primary` wins if both are sent, and names
    the field in a MISSING_FIELD error."""
    for key in (primary, *aliases):
        if ctx.has(key):
            return key
    return primary


def username_field(ctx: RequestContext, required: bool = True) -> Optional[str]:
    """`username`, by the registration rule: 3-32 letters, digits, '_', '.' or '-'
    (trimmed first). Uniqueness is the caller's check."""
    name = ctx.str_field("username", required=required, max_len=64)
    if name is None:
        return None
    if not USERNAME_RE.match(name):
        raise bad_request("INVALID_USERNAME",
                          "username must be 3-32 characters of letters, digits, '_', '.' or '-'",
                          field="username")
    return name


def get_chat_or_404(ctx: RequestContext, chat_id: UUID) -> Chat:
    chat = ctx.repo.get_chat(chat_id)
    if chat is None:
        raise not_found("CHAT_NOT_FOUND", "no such chat")
    return chat


def require_member(ctx: RequestContext, chat: Chat) -> Member:
    member = ctx.repo.get_member(chat.chat_id, ctx.user.user_id)
    if member is None:
        if chat.kind == "dm":
            # Other people's DMs, and DM requests that are not accepted yet, look
            # exactly like a chat that does not exist.
            raise not_found("CHAT_NOT_FOUND", "no such chat")
        raise forbidden("NOT_A_MEMBER", "join this group first")
    return member


def hide_foreign_dm(ctx: RequestContext, chat: Chat) -> None:
    """A DM you are not in (or not in yet) looks exactly like a chat that does not exist."""
    if chat.kind == "dm" and ctx.repo.get_member(chat.chat_id, ctx.user.user_id) is None:
        raise not_found("CHAT_NOT_FOUND", "no such chat")


def limit(ctx: RequestContext, limiter, what: str) -> None:
    """Per-account rate limit for actions that cost other people (pushes, history)."""
    wait = limiter.take(ctx.user.user_id)
    if wait > 0:
        raise forbidden("RATE_LIMITED", f"you are {what} too fast; wait and try again", retry_after=int(wait) + 1)


def post_message(ctx: RequestContext, chat: Chat, content: str, sender_id: Optional[UUID],
                 kind: str = "text", exclude_origin: bool = True) -> Message:
    """Stores a message and pushes it to every online member (R6: new incoming messages)."""
    with ctx.app.chat_lock(chat.chat_id):
        msg = ctx.repo.add_message(chat.chat_id, sender_id, content, kind)
        if sender_id is not None:
            ctx.repo.mark_read(chat.chat_id, sender_id, msg.message_id)  # your own message counts as read
        members = ctx.repo.get_members(chat.chat_id)
        ctx.hub.push([m.user_id for m in members],
                     ReplyCord.push("chat/new_message", {"chat_id": chat.chat_id, "chat_kind": chat.kind,
                                                         "message": message_view(msg)}),
                     exclude_conn=ctx.peer.conn_id if exclude_origin else None)
    return msg


def chat_info_for(ctx: RequestContext, chat: Chat) -> dict:
    """GET chat/{id}/info for the caller; the same object wherever a reply carries `info`."""
    members = ctx.repo.get_members(chat.chat_id)
    me = next((m for m in members if m.user_id == ctx.user.user_id), None)
    invited, total = (ctx.repo.list_pending_invites(chat.chat_id, MEMBERS_IN_CHAT_INFO)
                      if chat.kind == "group" and me is not None else (None, 0))
    return chat_info_view(chat, members, ctx.hub, me, max_members=MEMBERS_IN_CHAT_INFO,
                          invited=invited, invited_total=total)


def pending_chat_rows(ctx: RequestContext, limit: int = PENDING_ROWS_MAX) -> list:
    """Group invitations the caller has not answered yet, as chat rows with
    pending = true, so GET chats shows them even if the push was missed (the
    invitation arrived while the client was offline)."""
    incoming, _ = ctx.repo.list_pending_requests(ctx.user.user_id)
    rows = []
    for req in incoming:
        if req.kind != "invite":
            continue            # a DM request has no room yet; it is in GET users/requests
        chat = ctx.repo.get_chat(req.chat_id)
        if chat is None:
            continue            # the group was deleted a moment ago
        rows.append(invited_chat_view(chat, req.created_at, len(ctx.repo.get_members(req.chat_id))))
        if len(rows) >= limit:
            break
    return rows


def chat_item(ctx: RequestContext, user_id: UUID, chat_id: UUID) -> Optional[dict]:
    """The chat as it appears in that user's GET chats list (None if they are not in it)."""
    item = ctx.repo.get_chat_item(user_id, chat_id)
    return chat_item_view(item, ctx.hub) if item is not None else None


def announce_dm_open(ctx: RequestContext, chat: Chat, users: Iterable[UUID], replay: bool = True) -> None:
    """chats/new to both people, on every connection (the caller's too, so no push
    about the room can reach a connection before it knows the room), then, with
    `replay`, the request's stored first message as chat/new_message. Call under
    the chat's lock."""
    users = list(users)
    for uid in users:
        item = chat_item(ctx, uid, chat.chat_id)
        if item is not None:
            ctx.hub.push([uid], ReplyCord.push("chats/new", {"chat": item, "reason": "dm_opened"}))
    if replay:
        # The DM request's message was stored with the request; deliver it now.
        earlier, _ = ctx.repo.list_messages(chat.chat_id, MessageQuery(count=20))
        for msg in earlier:
            ctx.hub.push(users, ReplyCord.push("chat/new_message", {"chat_id": chat.chat_id,
                                                                    "chat_kind": chat.kind,
                                                                    "message": message_view(msg)}))


def announce_pending(ctx: RequestContext, request: ChatRequest) -> None:
    """users/request for a new DM request or invite, to its receiver, plus chats/new
    with a pending chat row for a group invitation: under the chat's lock, and only
    if it is still pending, so they can never arrive after the newer state
    (accepted, or the room already open) that an answer announced."""
    with ctx.app.chat_lock(request.chat_id):
        now = ctx.repo.get_request(request.request_id)
        if now is None or now.status != "pending":
            return
        ctx.hub.push([request.to_user], ReplyCord.push("users/request", {"request": request_view(now)}))
        if now.kind != "invite":
            return      # a DM request has no room yet, so there is no chat row to send
        chat = ctx.repo.get_chat(now.chat_id)
        if chat is not None:
            row = invited_chat_view(chat, now.created_at, len(ctx.repo.get_members(chat.chat_id)))
            ctx.hub.push([request.to_user], ReplyCord.push("chats/new", {"chat": row}))


def announce_request(ctx: RequestContext, request: ChatRequest, to: Optional[UUID]) -> None:
    """users/request about a request's new status, to the other side."""
    if to is not None:
        ctx.hub.push([to], ReplyCord.push("users/request", {"request": request_view(request)}),
                     exclude_conn=ctx.peer.conn_id)


def answer_request(ctx: RequestContext, req: ChatRequest, status: str) -> Optional[dict]:
    """The caller accepts / declines / cancels a request it may answer: the change,
    its pushes, and the reply fields {"request", and when accepted "chat" (+ "info"
    for an invite)}. None if it was no longer pending by the time it was resolved."""
    me = ctx.user.user_id
    with ctx.app.chat_lock(req.chat_id):
        resolved = ctx.repo.resolve_request(req.request_id, status, me)
        if resolved is None:
            return None
        if status == "accepted":
            if req.kind == "dm":
                room = ctx.repo.get_chat(req.chat_id)
                if room is not None:
                    announce_dm_open(ctx, room, [req.from_user, me])
            else:
                chat = ctx.repo.get_chat(req.chat_id)
                if chat is not None:
                    after_join(ctx, chat, ctx.user)
    if req.kind == "invite" and status in ("declined", "cancelled"):
        # The invitee's pending chat row is gone: on this change the client drops it
        # (their other devices too; the inviter hears about it as users/request).
        ctx.hub.push([req.to_user], ReplyCord.push("chat/members_changed",
                                                   {"chat_id": req.chat_id, "change": "invite_cancelled"}),
                     exclude_conn=ctx.peer.conn_id)
    result = {"request": request_view(resolved)}
    if status == "accepted":
        result["chat"] = chat_item(ctx, me, req.chat_id)
        if req.kind == "invite":
            chat = ctx.repo.get_chat(req.chat_id)
            if chat is not None:
                result["info"] = chat_info_for(ctx, chat)
    announce_request(ctx, resolved, req.to_user if status == "cancelled" else req.from_user)
    return result


def check_request_limits(ctx: RequestContext, target_id: UUID) -> None:
    """DM requests and invites push a packet to someone else, so they are limited:
    a rate per account, and a cap on pending requests per sender and per receiver
    (which also keeps GET users/requests small)."""
    limit(ctx, ctx.app.request_limiter, "sending requests")
    cap = ctx.config.max_pending_requests
    if cap > 0:
        if ctx.repo.count_pending_requests(ctx.user.user_id)[1] >= cap:
            raise forbidden("LIMIT_REACHED", f"you already have {cap} pending requests; cancel some first")
        if ctx.repo.count_pending_requests(target_id)[0] >= cap:
            raise forbidden("LIMIT_REACHED", "that user has too many pending requests right now")


def group_item(ctx: RequestContext, chat: Chat) -> GroupListItem:
    return GroupListItem(chat=chat, members=ctx.repo.get_members(chat.chat_id))


def announce_membership(ctx: RequestContext, chat: Chat, who: UserRef, change: str) -> None:
    """Keeps everyone's group list (R9) current with a small delta, not the whole
    member list: who joined or left, and the new member count. Call under the chat lock."""
    count = len(ctx.repo.get_members(chat.chat_id))
    if count == 0:
        ctx.hub.push_all(ReplyCord.push("chats/group_deleted", {"chat_id": chat.chat_id}))
        return
    ctx.hub.push_all(ReplyCord.push("chat/members_changed", {
        "chat_id": chat.chat_id, "name": chat.name, "member_count": count, "change": change,
        "user": user_ref(who)}))


def after_join(ctx: RequestContext, chat: Chat, user) -> None:
    """Someone just became a member (joined by themselves or accepted an invite):
    the history line, their read marker, and everyone's group list. Call under the chat lock."""
    # Not to the joiner's own connection: its reply (chat.last_message) carries this line,
    # and a push about the room must not arrive before the reply that adds the room.
    post_message(ctx, chat, f"{user.username} joined the group", sender_id=None, kind="system")
    # Whatever was said before you joined does not count as unread for you.
    ctx.repo.mark_read(chat.chat_id, user.user_id, None)
    announce_membership(ctx, chat, ref_of(user), "joined")
