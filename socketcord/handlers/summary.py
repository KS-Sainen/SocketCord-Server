"""AI summary of a chat (rubric Part 3): GET chat/{chat_id}/summary (POST works too).

Only members can ask, and only for messages they can read anyway (R7/R11): the
same membership check as GET chat/{chat_id}/messages.
"""
from __future__ import annotations

import re
from uuid import UUID

from ..ai import AiError, SummaryJob, build_transcript, chat_label, reader_label
from ..db.base import MessageQuery
from ..protocol import GET, POST, ReplyCord, Status
from ..router import ApiError, RequestContext, bad_request, ok
from . import router
from .common import get_chat_or_404, limit, require_member

COUNT_DEFAULT, COUNT_MAX = 50, 200
MODES = ("recent", "unread")
LANGUAGE_RE = re.compile(r"[A-Za-z][A-Za-z -]{0,31}")   # "auto", "th", "en-US", "Thai", ...


@router.route(GET, "chat/{chat_id:uuid}/summary")
@router.route(POST, "chat/{chat_id:uuid}/summary")   # the frontend README's method: the same call
def chat_summary(ctx: RequestContext, chat_id: UUID) -> ReplyCord:
    chat = get_chat_or_404(ctx, chat_id)
    member = require_member(ctx, chat)
    mode = ctx.str_field("mode", required=False, max_len=16, default="recent")
    if mode not in MODES:
        raise bad_request("INVALID_FIELD", "'mode' must be \"recent\" or \"unread\"", field="mode")
    count = ctx.int_field("count", required=False, minimum=1, maximum=COUNT_MAX, default=COUNT_DEFAULT)
    before_id = ctx.uuid_field("before_id", required=False)
    if before_id is not None and mode != "recent":
        raise bad_request("INVALID_FIELD", "'before_id' only works with mode \"recent\"", field="before_id")
    language = ctx.str_field("language", required=False, max_len=32, default="auto")
    if language.lower() == "auto":
        language = "auto"
    elif not LANGUAGE_RE.fullmatch(language):
        raise bad_request("INVALID_FIELD", "'language' must be \"auto\" or a language name or code such as "
                          "\"th\" or \"English\"", field="language")
    service = ctx.app.ai
    if not service.available:
        raise ApiError(Status.INTERNAL_ERROR, "AI_UNAVAILABLE", service.unavailable_reason)
    limit(ctx, ctx.app.ai_limiter, "asking for summaries")

    # The newest `count` messages (before `before_id`); "unread" keeps the ones
    # after your read marker, so it summarizes the latest unread messages.
    messages, has_more = ctx.repo.list_messages(chat_id, MessageQuery(count=count, before_id=before_id))
    if mode == "unread":
        unread = [m for m in messages if m.message_id > member.last_read_message_id]
        has_more = has_more and len(unread) == len(messages)
        messages = unread
    reply = {"chat_id": chat_id, "mode": mode, "language": language}
    if not messages:
        reply.update(summary=None, message_count=0, omitted=0, has_more=False, engine=None, model=None,
                     cached=False)
        return ok(reply)

    transcript = build_transcript(messages, service.max_input_chars)
    members = ctx.repo.get_members(chat_id) if chat.kind == "dm" else []
    job = SummaryJob(chat_label=chat_label(chat, members), reader_label=reader_label(ctx.user),
                     language=language, transcript=transcript)
    try:
        result, cached = service.summarize(job)
    except AiError as exc:
        extra = {"retry_after": exc.retry_after} if exc.retry_after else {}
        raise ApiError(exc.status, exc.code, exc.detail, **extra) from None
    used = transcript.used
    reply.update(summary=result.text, message_count=len(used), omitted=transcript.omitted,
                 first_message_id=used[0].message_id, last_message_id=used[-1].message_id,
                 from_time=used[0].created_at, to_time=used[-1].created_at,
                 has_more=has_more or transcript.omitted > 0, engine=result.engine, model=result.model,
                 cached=cached, cut_short=result.cut_short)
    return ok(reply)
