"""Profile, online list, user search, DM requests, and answering requests/invites."""
from __future__ import annotations

import re
from typing import List, Optional
from uuid import UUID

from ..db.base import AlreadyRequested, Chat, ChatRequest, MessageQuery, SameColors
from ..protocol import GET, PATCH, POST, ReplyCord
from ..router import RequestContext, bad_request, forbidden, not_found, ok
from ..views import message_view, online_user, request_view, user_view
from . import router
from .common import (MAX_MESSAGE_LEN, after_join, announce_dm_open, announce_pending, announce_request,
                     chat_info_for, chat_item, check_request_limits, limit, post_message)

COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
SEARCH_COUNT_MAX = 50


@router.route(GET, "users/info")
def user_info(ctx: RequestContext) -> ReplyCord:
    return ok({"user": user_view(ctx.user, ctx.hub, full=True)})


def _color_field(ctx: RequestContext, key: str) -> Optional[str]:
    value = ctx.str_field(key, required=False, max_len=7)
    if value is None:
        return None
    if not COLOR_RE.match(value):
        raise bad_request("INVALID_FIELD", f"'{key}' must be an RGB hex code like \"#1a2b3c\"", field=key)
    return value.lower()


@router.route(PATCH, "users/profile")
def update_profile(ctx: RequestContext) -> ReplyCord:
    """The profile is the user's name colours: text_color on text_bg_color."""
    text_color = _color_field(ctx, "text_color")
    text_bg_color = _color_field(ctx, "text_bg_color")
    if text_color is None and text_bg_color is None:
        raise bad_request("MISSING_FIELD", "send 'text_color' and/or 'text_bg_color'", field="text_color")
    which = "text_bg_color" if text_bg_color is not None else "text_color"
    same = bad_request("INVALID_FIELD", "the text colour and the background colour must differ", field=which)
    if (text_color or ctx.user.text_color) == (text_bg_color or ctx.user.text_bg_color):
        raise same
    limit(ctx, ctx.app.profile_limiter, "changing your profile")   # each change is pushed to everyone
    try:
        user = ctx.repo.update_profile(ctx.user.user_id, text_color, text_bg_color)
    except SameColors:
        raise same from None
    if user is None:
        raise not_found("USER_NOT_FOUND", "account no longer exists")
    ctx.hub.update_profile(online_user(user))
    ctx.hub.push_all(ReplyCord.event("profile", {"user": user_view(user, ctx.hub)}),
                     exclude_conn=ctx.peer.conn_id)
    return ok({"user": user_view(user, ctx.hub, full=True)})


@router.route(GET, "users/online")
def online_users(ctx: RequestContext) -> ReplyCord:
    """R4: every client currently connected (logged in), including the caller."""
    me = ctx.user.user_id
    online, seq = ctx.hub.online_snapshot()
    users = [{**u.as_dict(True), "is_self": u.user_id == me} for u in online]
    # presence_seq: event/presence with a seq not above this is older than this list.
    return ok({"count": len(users), "users": users, "presence_seq": seq})


@router.route(GET, "users/search")
def search_users(ctx: RequestContext) -> ReplyCord:
    """Finds people by username (case-insensitive prefix), e.g. to invite or DM
    someone who is not online. An exact match comes first."""
    query = ctx.str_field("query", required=False, min_len=0, max_len=32, default="")
    count = ctx.int_field("count", required=False, minimum=1, maximum=SEARCH_COUNT_MAX, default=20)
    offset = ctx.int_field("offset", required=False, minimum=0, maximum=10_000_000, default=0)
    users, has_more = ctx.repo.search_users(query, offset=offset, limit=count)
    me = ctx.user.user_id
    return ok({"query": query, "offset": offset, "has_more": has_more,
               "users": [{**user_view(u, ctx.hub), "is_self": u.user_id == me} for u in users]})


# ---------------------------------------------------------------------- DMs

def _dm_reply(status: str, chat_id: Optional[UUID], chat: Optional[dict], messages: List,
              request: Optional[ChatRequest]) -> ReplyCord:
    """POST users/{id}/dm always answers with the same keys."""
    return ok({"status": status, "chat_id": chat_id, "chat": chat,
               "messages": [message_view(m) for m in messages],
               "request": request_view(request) if request is not None else None})


@router.route(POST, "users/{user_id:uuid}/dm")
def send_dm(ctx: RequestContext, user_id: UUID) -> ReplyCord:
    """R7: direct message. The first one is a DM request; the room opens when the
    other side accepts it (or at once with DM_REQUIRE_ACCEPT=false)."""
    me = ctx.user
    text = ctx.str_field("message", max_len=MAX_MESSAGE_LEN, multiline=True)
    if user_id == me.user_id:
        raise bad_request("INVALID_TARGET", "you cannot DM yourself")
    target = ctx.repo.get_user(user_id)
    if target is None:
        raise not_found("USER_NOT_FOUND", "no such user")
    limit(ctx, ctx.app.message_limiter, "sending messages")

    # Two people opening their DM at the same moment (both sending, crossing
    # requests) take turns, so the room is announced before anything is posted in it.
    with ctx.app.pair_lock(me.user_id, target.user_id):
        # The other side can act at the same moment (accept, cancel, DM us); each pass
        # looks at the pair's DM again and takes the matching branch.
        for _ in range(3):
            dm = ctx.repo.find_dm(me.user_id, target.user_id)
            if dm is not None and dm.status == "accepted":
                msg = post_message(ctx, dm, text, me.user_id)
                return _dm_reply("sent", dm.chat_id, chat_item(ctx, me.user_id, dm.chat_id), [msg], None)
            if dm is not None and dm.initiator == target.user_id:
                # They already asked us: replying accepts their request.
                opened = _accept_their_request(ctx, dm, target, text)
                if opened is not None:
                    return opened
                continue                    # cancelled, or accepted elsewhere, just now
            if dm is not None:
                raise forbidden("ALREADY_REQUESTED", f"you already have a pending DM request to {target.username}")
            if not ctx.config.dm_require_accept:
                room, created = ctx.repo.create_dm(me.user_id, target.user_id)
                if not created:
                    continue                # someone opened it a moment ago
                with ctx.app.chat_lock(room.chat_id):
                    announce_dm_open(ctx, room, [target.user_id, me.user_id], replay=False)
                    msg = post_message(ctx, room, text, me.user_id)
                return _dm_reply("sent", room.chat_id, chat_item(ctx, me.user_id, room.chat_id), [msg], None)
            check_request_limits(ctx, target.user_id)
            try:
                req = ctx.repo.create_request("dm", me.user_id, target.user_id, message=text)
            except AlreadyRequested:
                continue                    # theirs (or ours) arrived at this very moment
            announce_pending(ctx, req)
            return _dm_reply("pending", None, None, [], req)
        raise forbidden("TRY_AGAIN", "the DM with this person changed while sending; try again")


def _accept_their_request(ctx: RequestContext, dm: Chat, target, text: str) -> Optional[ReplyCord]:
    me = ctx.user.user_id
    with ctx.app.chat_lock(dm.chat_id):
        resolved = ctx.repo.resolve_request(dm.chat_id, "accepted", me)
        if resolved is None:
            return None
        room = ctx.repo.get_chat(dm.chat_id) or dm
        announce_dm_open(ctx, room, [target.user_id, me])
        post_message(ctx, room, text, me)
    announce_request(ctx, resolved, target.user_id)
    messages, _ = ctx.repo.list_messages(dm.chat_id, MessageQuery(count=20))
    return _dm_reply("sent", dm.chat_id, chat_item(ctx, me, dm.chat_id), messages, resolved)


# ---------------------------------------------------------------------- requests and invites

@router.route(GET, "users/requests")
def list_requests(ctx: RequestContext) -> ReplyCord:
    incoming, outgoing = ctx.repo.list_pending_requests(ctx.user.user_id)
    return ok({"incoming": [request_view(r) for r in incoming],
               "outgoing": [request_view(r) for r in outgoing]})


@router.route(POST, "users/requests/{request_id:uuid}/accept")
def accept_request(ctx: RequestContext, request_id: UUID) -> ReplyCord:
    return _answer(ctx, request_id, "accepted")


@router.route(POST, "users/requests/{request_id:uuid}/decline")
def decline_request(ctx: RequestContext, request_id: UUID) -> ReplyCord:
    return _answer(ctx, request_id, "declined")


@router.route(POST, "users/requests/{request_id:uuid}/cancel")
def cancel_request(ctx: RequestContext, request_id: UUID) -> ReplyCord:
    return _answer(ctx, request_id, "cancelled")


def _closed(ctx: RequestContext, request_id: UUID):
    """Why a resolve did not go through: gone (253) or answered already (252)."""
    again = ctx.repo.get_request(request_id)
    if again is None:
        return not_found("REQUEST_NOT_FOUND", "no such request")
    return forbidden("REQUEST_CLOSED", f"this request is already {again.status}")


def _answer(ctx: RequestContext, request_id: UUID, status: str) -> ReplyCord:
    me = ctx.user.user_id
    req = ctx.repo.get_request(request_id)
    # Only the receiver may accept/decline; only the sender may cancel. Declined and
    # cancelled requests are deleted, so they answer like a request that never existed.
    if req is None or (req.from_user if status == "cancelled" else req.to_user) != me:
        raise not_found("REQUEST_NOT_FOUND", "no such request")
    if req.status != "pending":
        raise forbidden("REQUEST_CLOSED", f"this request is already {req.status}")

    result = {}
    with ctx.app.chat_lock(req.chat_id):
        resolved = ctx.repo.resolve_request(request_id, status, me)
        if resolved is None:
            raise _closed(ctx, request_id)
        if status == "accepted":
            if req.kind == "dm":
                room = ctx.repo.get_chat(req.chat_id)
                if room is not None:
                    announce_dm_open(ctx, room, [req.from_user, me])
            else:
                chat = ctx.repo.get_chat(req.chat_id)
                if chat is not None:
                    after_join(ctx, chat, ctx.user)
    result["request"] = request_view(resolved)
    if status == "accepted":
        result["chat"] = chat_item(ctx, me, req.chat_id)
        if req.kind == "invite":
            chat = ctx.repo.get_chat(req.chat_id)
            if chat is not None:
                result["info"] = chat_info_for(ctx, chat)
    announce_request(ctx, resolved, req.to_user if status == "cancelled" else req.from_user)
    return ok(result)
