"""Turns records into the JSON objects sent to clients (one place, so every reply agrees).

Every embedded user has the same shape: {user_id, username, text_color,
text_bg_color}, plus "online" where presence matters. Read markers are sent as
null for "nothing read" (NO_MESSAGE inside the server).
"""
from __future__ import annotations

from typing import List, Optional

from .db.base import (NO_MESSAGE, Chat, ChatListItem, ChatRequest, GroupListItem, Member, MemberGroup, Message,
                      User, UserRef, UUID)
from .presence import OnlineUser, PresenceHub

PREVIEW_LEN = 120


def online_user(user: User) -> OnlineUser:
    return OnlineUser(user.user_id, user.username, user.text_color, user.text_bg_color)


def marker(message_id: Optional[UUID]) -> Optional[UUID]:
    return None if message_id is None or message_id == NO_MESSAGE else message_id


def user_ref(ref: Optional[UserRef], hub: Optional[PresenceHub] = None) -> Optional[dict]:
    if ref is None:
        return None
    d = {"user_id": ref.user_id, "username": ref.username, "text_color": ref.text_color,
         "text_bg_color": ref.text_bg_color}
    if hub is not None:
        d["online"] = hub.is_online(ref.user_id)
    return d


def ref_of(user: User) -> UserRef:
    return UserRef(user.user_id, user.username, user.text_color, user.text_bg_color)


def user_view(user: User, hub: PresenceHub, full: bool = False) -> dict:
    d = user_ref(ref_of(user), hub)
    if full:
        d["created_at"] = user.created_at
    return d


def member_view(m: Member, hub: PresenceHub) -> dict:
    return {"user_id": m.user_id, "username": m.username, "text_color": m.text_color,
            "text_bg_color": m.text_bg_color, "role": m.role, "joined_at": m.joined_at,
            "online": hub.is_online(m.user_id)}


def message_view(m: Message, preview: bool = False) -> dict:
    content = m.content
    if preview and len(content) > PREVIEW_LEN:
        content = content[:PREVIEW_LEN - 1] + "…"
    return {"message_id": m.message_id, "chat_id": m.chat_id, "kind": m.kind, "content": content,
            "created_at": m.created_at, "sender": user_ref(m.sender)}


def chat_item_view(item: ChatListItem, hub: PresenceHub) -> dict:
    """A chat as it appears in GET chats (and wherever a chat joins the caller's list)."""
    chat = item.chat
    d = {"chat_id": chat.chat_id, "kind": chat.kind, "is_dm": chat.kind == "dm", "name": chat.name,
         "description": chat.description, "role": item.role, "member_count": item.member_count,
         "unread": item.unread, "last_read_message_id": marker(item.last_read_message_id),
         "last_message": message_view(item.last_message, preview=True) if item.last_message else None,
         "last_activity": item.last_message.created_at if item.last_message else chat.created_at}
    if chat.kind == "dm":
        d["peer"] = user_ref(item.peer, hub)
        d["name"] = item.peer.username if item.peer is not None else "(deleted user)"
    return d


def _created_by(chat: Chat, members: List[Member]) -> Optional[dict]:
    """{user_id, username, text_color, text_bg_color}; username (and colours) are null
    once the creator has left the group."""
    if chat.created_by is None:
        return None
    creator = next((m for m in members if m.user_id == chat.created_by), None)
    if creator is None:
        return {"user_id": chat.created_by, "username": None, "text_color": None, "text_bg_color": None}
    return user_ref(UserRef(creator.user_id, creator.username, creator.text_color, creator.text_bg_color))


def invite_view(r: ChatRequest, hub: PresenceHub) -> dict:
    return {"request_id": r.request_id, "user": user_ref(r.receiver, hub), "invited_by": user_ref(r.sender),
            "invited_at": r.created_at}


def chat_info_view(chat: Chat, members: List[Member], hub: PresenceHub, me: Optional[Member],
                   max_members: Optional[int] = None, invited: Optional[List[ChatRequest]] = None,
                   invited_total: int = 0) -> dict:
    shown = members if max_members is None else members[:max_members]
    d = {"chat_id": chat.chat_id, "kind": chat.kind, "is_dm": chat.kind == "dm", "name": chat.name,
         "description": chat.description, "created_by": _created_by(chat, members), "created_at": chat.created_at,
         "member_count": len(members), "online_count": sum(1 for m in members if hub.is_online(m.user_id)),
         "members": [member_view(m, hub) for m in shown], "members_truncated": len(shown) < len(members),
         "joined": me is not None, "last_read_message_id": marker(me.last_read_message_id) if me else None}
    if invited is not None:
        d["invited"] = [invite_view(r, hub) for r in invited]
        d["invited_count"] = invited_total
        d["invited_truncated"] = len(invited) < invited_total
    if chat.kind == "dm" and me is not None:
        other = next((m for m in members if m.user_id != me.user_id), None)
        d["name"] = other.username if other else "(deleted user)"
        d["peer"] = (user_ref(UserRef(other.user_id, other.username, other.text_color, other.text_bg_color), hub)
                     if other else None)
    return d


def group_view(item: GroupListItem, hub: PresenceHub, viewer: Optional[UUID],
               max_members: Optional[int] = None) -> dict:
    chat = item.chat
    shown = item.members if max_members is None else item.members[:max_members]
    return {"chat_id": chat.chat_id, "kind": chat.kind, "is_dm": False, "name": chat.name,
            "description": chat.description, "created_at": chat.created_at,
            "created_by": _created_by(chat, item.members), "member_count": len(item.members),
            "online_count": sum(1 for m in item.members if hub.is_online(m.user_id)),
            "members": [member_view(m, hub) for m in shown],
            "members_truncated": len(shown) < len(item.members),
            "joined": viewer is not None and any(m.user_id == viewer for m in item.members)}


def member_group_view(g: MemberGroup) -> dict:
    """An entry of GET users/{user_id}/groups: a group, that user's place in it, and
    whether the caller is in it too (`joined`)."""
    chat = g.chat
    return {"chat_id": chat.chat_id, "kind": chat.kind, "is_dm": False, "name": chat.name,
            "description": chat.description, "created_at": chat.created_at, "member_count": g.member_count,
            "role": g.role, "joined_at": g.joined_at, "joined": g.viewer_is_member}


def request_view(r: ChatRequest) -> dict:
    d = {"request_id": r.request_id, "kind": r.kind, "status": r.status,
         "from": user_ref(r.sender), "to": user_ref(r.receiver),
         "created_at": r.created_at, "responded_at": r.responded_at}
    if r.kind == "dm":
        d["message"] = r.message
    else:
        d["chat"] = {"chat_id": r.chat_id, "name": r.chat_name}
    return d
