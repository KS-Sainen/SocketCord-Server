# SocketCord API reference

Every call the backend serves and every packet it pushes: request fields, reply fields, limits,
defaults, errors, pushes, and an example of each. It is written from the code
(`socketcord/handlers/`, `socketcord/views.py`, `socketcord/router.py`, `socketcord/app.py`,
`socketcord/presence.py`). The order of keys inside a JSON object means nothing, and the
examples do not always show the server's order: they list `path` first, while the server sends
it last.

This is **wire contract v2**: register, log in and change your password each take ONE request,
with the password in the message body as **clear text**. The socket is a plain TCP socket with
no TLS, so anyone who can watch the traffic between the client and the server can read the
password. The server hashes it with PBKDF2-HMAC-SHA256 (600,000 rounds, a 16-byte random salt
per account) and stores only that hash; it never stores or logs the password itself, and no
reply ever contains it. This is not a secure design, and it is not presented as one.

The frontend README lists 11 calls (section 2). The backend serves 17 more (section 3), and
answers the summary by POST as well as GET; the team rule says those must be documented in
full, and they are. The binary packet layout, the password hashing and the connection limits
are in [PROTOCOL.md](PROTOCOL.md); the database is described in the
[README](../README.md#database).

| Section | Contents |
| ------- | -------- |
| [1. Conventions](#1-conventions) | requests, reply head, authentication, ids and time, common errors, text rules, rate limits, object shapes |
| [2. The 11 calls of the frontend README](#2-the-11-calls-of-the-frontend-readme) | register, login, profile, info, chats, chat info, messages, send, DM, invite, password |
| [3. Additional calls](#3-additional-calls) | logout, online, search, requests, accept/decline/cancel, create group, all groups, read, join, leave, AI summary, a user's groups, invitees, accept/reject an invite by group |
| [4. Server pushes](#4-server-pushes) | `chat/new_message`, `users/status`, `users/request`, `chats/new`, `chats/group_created`, `chat/members_changed`, `chats/group_deleted`, `users/profile_changed`, `login/revoked` |
| [5. Frontend adapter](#5-frontend-adapter) | API objects to `chat_page.py`, and what the client must do |

All calls. `path` is what a successful reply to that call carries in its head (section 1.2);
a URL the contract v2 table does not name, and every error, answers with the URL itself.

| Method | URL | Auth | `path` | What it does | Section |
| ------ | --- | ---- | ------ | ------------ | ------- |
| POST | `users` | none | `login/created` | register | 2.1 |
| POST | `users/login` | none | `login/success` | log in | 2.2 |
| PATCH | `users/profile` | yes | `profile/updated` | change your username and/or name colours | 2.3 |
| GET | `users/info` | yes | `profile/info` | your account | 2.4 |
| GET | `chats` | yes | `chats/list` | the chats you are in, and invitations | 2.5 |
| GET | `chat/{chat_id}/info` | yes | `chat/info` | a chat's details and members | 2.6 |
| GET | `chat/{chat_id}/messages` | member | `chat/messages` | message history, with filters | 2.7 |
| POST | `chat/{chat_id}/message` | member | `chat/sent` | send a message | 2.8 |
| POST | `users/{user_id}/dm` | yes | `chats/dm` | send a DM (the first one opens the room; a request only with `DM_REQUIRE_ACCEPT=true`) | 2.9 |
| POST | `chat/{chat_id}/invite` | member | `chat/invited` | invite someone to a group | 2.10 |
| PATCH | `users/password` | yes | `profile/password` | change your password | 2.11 |
| PATCH | `users/profile/password` | yes | `profile/password` | the same call under its older URL | 2.11 |
| POST | `users/logout` | yes | `login/logout` | end your session | 3.1 |
| GET | `users/online` | yes | `users/online` | who is online | 3.2 |
| GET | `users/search` | yes | the URL | find users by name | 3.3 |
| GET | `users/requests` | yes | the URL | your pending DM requests and invites | 3.4 |
| POST | `users/requests/{request_id}/accept` | receiver | the URL | accept a DM request or invite | 3.5 |
| POST | `users/requests/{request_id}/decline` | receiver | the URL | decline one | 3.6 |
| POST | `users/requests/{request_id}/cancel` | sender | the URL | take one back | 3.7 |
| POST | `chats` | yes | `chats/created` | create a group | 3.8 |
| GET | `chats/groups` | yes | the URL | every group on the server | 3.9 |
| POST | `chat/{chat_id}/read` | member | `chat/read` | move your read marker | 3.10 |
| POST | `chat/{chat_id}/join` | yes | the URL | join a group by yourself | 3.11 |
| POST | `chat/{chat_id}/leave` | member | the URL | leave a group | 3.12 |
| GET | `chat/{chat_id}/summary` | member | `chat/summary` | AI summary of a chat | 3.13 |
| POST | `chat/{chat_id}/summary` | member | `chat/summary` | the same call by POST (the frontend README's method) | 3.13 |
| GET | `users/{user_id}/groups` | yes | `users/groups` | the groups a user is in | 3.14 |
| GET | `chat/{chat_id}/invitees` | member | the URL | who you can invite to a group | 3.15 |
| POST | `chat/{chat_id}/invite/accept` | invitee | `chat/joined` | accept your invite to that group | 3.16 |
| POST | `chat/{chat_id}/invite/reject` | invitee | `chat/declined` | decline your invite to that group | 3.17 |

---

## 1. Conventions

### 1.1 Requests

A request is one RequestCord packet ([PROTOCOL.md](PROTOCOL.md) section 1): a method code, the
User ID and Session Token (16 raw bytes each, all zeros when logged out), the URL and the message.

| Method | Method code |
| ------ | ----------- |
| POST | `112233445` |
| GET | `354984100` |
| PATCH | `943742156` |

* **URL**: UTF-8, such as `chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/messages`. A leading or
  trailing `/` is ignored, and the fixed words are matched without regard to case. Ids in the URL
  must be UUIDs (any form Python's `uuid.UUID` reads, with or without hyphens); anything else
  matches no call and answers `253 NOT_FOUND`.
* **Message**: a JSON object of at most 32,768 bytes, or nothing (the same as `{}`). GET calls take
  their parameters here too.
* **`rid`** (optional, every call): an integer of at most 20 digits, or a string of at most 64
  characters. The server takes it out of the message before the call reads it and echoes it in the
  reply, so the client can match a reply to its request. A `rid` of another type (`1.5`, `true`,
  an object) or a longer string is ignored and not echoed; the call still runs. A `rid` that
  breaks the content rules of the message (an integer of more than 20 digits, `NaN`, `Infinity`,
  `1e400`, a string with a NUL character or a lone surrogate) makes the whole message invalid:
  the call does not run, and the reply is `250 INVALID_JSON` without `rid`.
* Fields a call does not list are ignored. A field that is absent and a field that is `null` mean
  the same thing: "not given".
* Some fields have two accepted spellings, because the reply's word and the frontend's older
  word differ: `text`/`content` (the message body and the history filter), `background`/
  `text_bg_color`, `message`/`text`/`content` (a DM body) and `message_id`/`id` (the read
  marker). The first spelling is the one the reply uses; if both are sent, the first one wins
  (the colours are the exception: two different colours are refused).

### 1.2 Replies

A reply is one ReplyCord packet: a status code and a JSON object. Its first fields are the
**reply head**:

| Field | Type | Present | Meaning |
| ----- | ---- | ------- | ------- |
| `path` | string | always | The **semantic name of the call**, such as `login/success` or `chat/messages` (the table at the top of this file, and contract v2 section 2). A call the contract does not name answers with the request URL instead, without leading/trailing `/` and cut to 256 characters; so do replies to a URL no route matched, **and every reply whose status is not 200**: the semantic name names the call succeeding, so an error echoes the URL (a wrong password answers `users/login`, never `login/success`). |
| `method` | string | always (not in pushes) | `"POST"`, `"GET"` or `"PATCH"`, also when the packet itself was broken (a checksum or UTF-8 error). For an unknown method code, the code in decimal, such as `"12345"`. |
| `rid` | integer or string | when the request had a valid one | The request's own `rid`. |
| `error` | string | status ≠ 200 | Machine-readable code (sections 1.5 and [PROTOCOL.md](PROTOCOL.md) section 3). |
| `detail` | string | status ≠ 200 | One sentence for people; safe to show in the GUI. |
| `field` | string | some 250 and 252 errors | The request field at fault. |
| `retry_after` | integer | rate-limit, quota and some AI errors | Seconds to wait before trying again (at least 1). |

The call's own fields follow the head. Every field a call lists is always present unless the
call says otherwise; "none" is sent as `null`, not left out.

Requests from one connection are answered one at a time, in the order they were sent. Server
pushes (section 4) can arrive between replies: a push has one of the nine paths in section 4
and **no `method` and no `rid`**, so `"method" not in reply` is the test for "this is a push"
(`socketcord/protocol.py` has the list as `PUSH_PATHS`; no push path is ever a request URL).
Route a reply by its `rid`, or by `path` and `method` (`GET chats` is `chats/list` and `POST
chats` is `chats/created`, so the paths of the named calls are already unique).

Error replies look like this (`path` is the URL that was asked for):

```json
{
  "path": "chat/4f1e.../messages",
  "method": "GET",
  "rid": 7,
  "error": "NOT_A_MEMBER",
  "detail": "join this group first"
}
```

```json
{
  "path": "chat/4f1e.../message",
  "method": "POST",
  "rid": 14,
  "error": "INVALID_FIELD",
  "detail": "'text' cannot be blank",
  "field": "text"
}
```

### 1.3 Authentication

`POST users` and `POST users/login` need no credentials (they ignore the User ID and Session
Token in the header). Every other call needs the `user_id` and `session_token` of the login reply
in the request header.

| Situation | Reply |
| --------- | ----- |
| The User ID or the Session Token is all zeros (no credentials) | `253 NOT_FOUND` "this call needs a User ID and Session Token" (the frontend README's rule) |
| The token is unknown, has ended (logout, a newer login, a password change), has expired, or belongs to another User ID | `251 INVALID_SESSION` |
| This connection is logged in as a different account | `252 CONNECTION_IN_USE`: log in on it to switch, or use another connection |
| The account already has `MAX_CONNECTIONS_PER_USER` (5) logged-in connections and this is another one | `252 TOO_MANY_CONNECTIONS` |

* A `251` reply to a call sent with credentials (every call except `POST users` and
  `POST users/login`) means "the session is gone, show the login page". On `POST users` and
  `POST users/login`, `251` only reports that this attempt failed (wrong username or password,
  or a login that lost a race with another login of the same account): show the error and let
  the user try again.
* The URL is matched before the credentials are checked: an unknown call answers
  `253 NOT_FOUND` "no such call (...)" with or without credentials.
* The first authenticated request on a connection logs that connection in. A client that
  reconnects with a saved token is therefore online again, and if the account was offline,
  everyone else gets `users/status`.
* **One session per account.** A login ends the previous session everywhere
  (`login/revoked`, reason `logged_in_elsewhere`). A password change does NOT: it keeps the
  token, so the caller and its other connections keep working (section 2.11). Several
  connections may share the one token: send the same User ID and Session Token on each of them,
  and never call `users/login` on the extra ones. Details:
  [PROTOCOL.md](PROTOCOL.md) section 4, "Sessions".

### 1.4 Ids and timestamps

* `user_id`, `chat_id` and `request_id` are UUID strings. Replies always write UUIDs in lower
  case with hyphens. Fields that take an id accept any form `uuid.UUID` reads.
* A DM request's `request_id` is the DM's `chat_id`. An invite's `request_id` is its membership
  id, which is not a chat id.
* A message's `id` is a **UUIDv7** made by the server. Its first 48 bits are the Unix time in
  milliseconds, followed by a counter, so a later message always has a larger id, and within one
  chat ids become visible in that order (paging with `after_id` never skips a message). Since the
  server sends every id in lower case with the hyphens in the same places, comparing two message
  ids as strings gives the same order as comparing their times: `a < b` means `a` is older.
  `uuid.UUID(a) < uuid.UUID(b)` gives the same answer.
* Read markers (`last_read_message_id`, `first_unread_id`) are message ids, or `null` when
  nothing has been read.
* **Timestamps in replies** are ISO 8601 in UTC with milliseconds and a **`+00:00` offset,
  never a `Z`**, such as `"2026-11-06T14:11:01.092+00:00"` (contract v2 section 3). A message's
  `time` is the moment stored inside its id. `datetime.fromisoformat(s)` reads them on every
  Python version the project supports (3.9 and later) and gives an aware UTC `datetime`;
  `.astimezone()` converts it to local time for display.
* **Timestamps in requests** (only the `time` filter of `GET chat/{chat_id}/messages`): ISO 8601
  (`"2026-11-05T10:00:00Z"`, `"2026-11-05T17:00:00+07:00"`, `"...+0700"`, `"2026-11-05"`; no zone
  means UTC) or epoch seconds as a JSON number. The server converts the value to UTC first; one
  that lands outside the years 1–9999 in UTC (such as `"0001-01-01T00:00:00+01:00"`) is refused
  with `250 INVALID_FIELD` ("'time' is out of range").

### 1.5 Errors every call can return

Each call in sections 2 and 3 lists its own errors. These can come from any call:

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `URL_CHECKSUM_MISMATCH`, `MESSAGE_CHECKSUM_MISMATCH` | The CRC-32 in the header does not match. No `rid`, because the message cannot be trusted; `method` is still the method's name. The next packet still parses. |
| 250 | `INVALID_ENCODING` | The URL or the message is not UTF-8. No `rid`. |
| 250 | `MESSAGE_TOO_LARGE` | The message is over 32,768 bytes (`rid` is echoed if it can be found). |
| 250 | `INVALID_JSON` | Not a JSON object; nested deeper than 16; a string with a NUL character or a lone surrogate; an integer with more than 20 digits; `NaN`, `Infinity` or a number like `1e400`. `rid` is echoed when the JSON itself could be read and the `rid` is valid (section 1.1). |
| 250 | `MISSING_FIELD` | A required field is absent or `null` (`field` names it). |
| 250 | `INVALID_FIELD` | A field has the wrong JSON type, length or range, a control character, or a bad format (`field` names it; `detail` says what is wrong). |
| 253 | `NOT_FOUND` | No call has this method and URL ("no such call (GET)"), including a URL id that is not a UUID. |
| 253 | `NOT_FOUND` | "the chat or user no longer exists": it was deleted while the call ran. |
| 254 | `DATABASE_UNAVAILABLE` | PostgreSQL cannot be reached, a statement or a lock wait took over 5 s, or a deadlock happened twice in a row. Try again. |
| 254 | `INTERNAL_ERROR` | A bug. The server logs it and keeps serving. |

Every call except `POST users` and `POST users/login` can also return the authentication errors
of section 1.3: `253 NOT_FOUND`, `251 INVALID_SESSION`, `252 CONNECTION_IN_USE`,
`252 TOO_MANY_CONNECTIONS`. The error tables below call these "common errors".

### 1.6 Text fields

* Text is **trimmed** (leading and trailing whitespace removed) before its length is checked and
  before it is stored. The exceptions are `text`/`content` in `POST chat/{chat_id}/message` and
  the passwords, which are kept exactly as sent (a password may be nothing but spaces). Lengths
  count characters, not bytes.
* **Control characters** (Unicode category Cc) are refused with `250 INVALID_FIELD`. Message
  text, DM text and group descriptions may contain newlines (`\n`, `\r`) and tabs (`\t`);
  names, passwords and every other text field may not.
* Integers must be JSON integers: `true`, `5.0` and `"5"` are refused.

### 1.7 Rate limits

Actions that push packets to other people, or fill the database, are limited per account with a
token bucket: the rate refills it, and the burst is how many can be used in a row after a quiet
period. Over the limit the call answers `252 RATE_LIMITED` with `retry_after`. A rate set to 0
turns that limit off.

| Setting | Default rate | Burst | Calls |
| ------- | ------------ | ----- | ----- |
| `MESSAGE_RATE_PER_MINUTE` | 60 a minute | 30 | `POST chat/{chat_id}/message`, `POST users/{user_id}/dm` (every DM) |
| `REQUEST_RATE_PER_HOUR` | 120 an hour | 20 | `POST chat/{chat_id}/invite`, `POST users/{user_id}/dm` when it creates a DM request |
| `GROUP_CREATE_PER_HOUR` | 20 an hour | 5 | `POST chats` |
| `MEMBERSHIP_RATE_PER_MINUTE` | 30 a minute | 10 | `POST chat/{chat_id}/join`, `POST chat/{chat_id}/leave`, `POST chat/{chat_id}/invite/accept` |
| `PROFILE_RATE_PER_MINUTE` | 10 a minute | 5 | `PATCH users/profile` (renames and colour changes) |
| `AI_SUMMARY_PER_MINUTE` | 4 a minute | 3 | `GET` / `POST chat/{chat_id}/summary` |

Other limits:

* **Registrations per address**: `POST users` runs PBKDF2 (`SCRAM_ITERATIONS` rounds) before it
  stores anything, so each client address may send `REGISTER_RATE_PER_MINUTE` (30 a minute, burst
  `REGISTER_BURST` 15; 120 and 40 in `docker-compose.yml`). Over it: `252 RATE_LIMITED` with
  `retry_after`.
* **Failed logins**: 5 per (username, address) within 5 minutes; 50 per address for usernames
  that do not exist, counted per address rather than per name, so made-up names cannot lock
  anybody out. Over it: `252 RATE_LIMITED`. An IPv6 address counts as its whole /64. A correct
  login clears the failures counted for that username from that address. The attempt is counted
  before the password is checked, so requests sent in parallel cannot all slip under the limit.
* **Pending requests**: nobody may have more than `MAX_PENDING_REQUESTS` (100) pending DM requests
  and invites sent, or received: `252 LIMIT_REACHED`. `0` turns this cap off. It is checked when a
  request is sent, so lowering it leaves the requests that already exist in place.
* Answering requests (except `POST chat/{chat_id}/invite/accept`, which counts as a join),
  marking as read, and every GET call except the summary have no limit.

### 1.8 Object shapes

The same object has the same shape everywhere it appears.

**User**: `user` in the register, login, `users/info` and profile replies.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user_id` | string | UUID |
| `username` | string | 3–32 characters from `A–Z a–z 0–9 _ . -`; unique without regard to case. It can change (`PATCH users/profile`, `users/profile_changed`): identify people by `user_id`. |
| `text_color` | string | `"#RRGGBB"` in UPPER case; default `"#171717"` |
| `background` | string | `"#RRGGBB"` in UPPER case; default `"#F2F2F2"`; never equal to `text_color`. Requests may spell it `text_bg_color` and may use either case (section 2.3). |
| `online` | boolean | the account has a logged-in connection (or lost its last one less than 2 s ago) |
| `created_at` | timestamp | when the account was made |

The entries of `GET users/online` and `GET users/search` are a User without `created_at`, plus
`is_self` (boolean: this is you). `users/status` and `users/profile_changed` carry a User without
`created_at`.

**User ref**: a user inside another object (`sender`, `from`, `to`, `created_by`, `invited_by`,
the `user` of `chat/members_changed`): `user_id`, `username`, `text_color`, `background`. A
DM's `peer` and an invite's `user` add `online` (boolean).

**Member**: an entry of `members`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user_id`, `username`, `text_color`, `background` | | as in User |
| `role` | string | `"owner"` for the group's creator (again after leaving and joining back), `"member"` for everyone else and for both people of a DM. The role gives no extra rights. |
| `joined_at` | timestamp | when they joined the group; for a DM, when it was accepted |
| `online` | boolean | |

Members are listed in `joined_at` order, then by `user_id`.

**Message**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `id` | string | UUIDv7 (section 1.4). NOT `message_id`. |
| `chat_id` | string | |
| `kind` | string | `"text"` (written by a user) or `"system"` (`"bob joined the group"`, `"bob left the group"`) |
| `text` | string | 1–4000 characters. In `last_message` of a chat item it is cut to 120 characters (119 and `"…"`). NOT `content`. |
| `time` | timestamp | the moment inside `id`. NOT `created_at`. |
| `sender` | string or null | the sender's username **as a string**, their current one; `null` on a system line |
| `sender_id` | string or null | the sender's `user_id`; `null` on a system line |

A system line therefore has `sender_id: null` and `sender: null`; the GUI shows it as
`[System]`. To draw a sender's colours, look the `sender_id` up in the chat's `members` (or in
`GET users/online`), since the message carries only the name.

**Chat item**: an entry of `GET chats`. Every reply and push that adds a chat to your list
carries it in this shape: `chat` in `POST chats`, `POST chat/{chat_id}/join`,
`POST users/requests/{request_id}/accept`, `POST users/{user_id}/dm`, and `chats/new`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | |
| `kind` | string | `"group"` or `"dm"` |
| `is_dm` | boolean | `kind == "dm"` |
| `name` | string | the group's name; for a DM, the other person's username |
| `description` | string | the group's description; `""` for a DM |
| `role` | string | `"owner"` or `"member"` (always `"member"` in a DM, and while `pending`) |
| `member_count` | integer | members (2 for a DM) |
| `pending` | boolean | `true` for a group invitation you have not answered yet: the chat is in your list, but you are not a member of it yet (section 2.5). `false` for every chat you are really in. |
| `unread` | integer | messages after your read marker that someone else sent (system lines count); `0` while `pending` |
| `last_read_message_id` | string or null | your read marker; `null` while `pending` |
| `last_message` | Message or null | the newest message, text cut to 120 characters; `null` while `pending` (you may not read the group yet) |
| `last_activity` | timestamp | the `time` of the newest message, or when the chat was made if it has none; when the invitation was sent while `pending` |
| `peer` | User ref + `online` | DMs only: the other person |

**Chat info**: `chat` of `GET chat/{chat_id}/info`, and `info` in the join and invite-accept
replies.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id`, `kind`, `is_dm`, `description` | | as in Chat item |
| `name` | string | the group's name; for a DM you are in, the other person's username |
| `created_by` | User ref or null | the group's creator (for a DM, who sent the first DM). While the creator is not a member, its `username`, `text_color` and `background` are `null`. |
| `created_at` | timestamp | when the chat was made (for a DM, when the request was sent) |
| `member_count` | integer | all members |
| `online_count` | integer | members online now |
| `members` | list of Member | at most 500 |
| `members_truncated` | boolean | there are more than 500 members |
| `joined` | boolean | you are a member |
| `last_read_message_id` | string or null | your read marker (`null` if you are not a member) |
| `peer` | User ref + `online` | DMs only: the other person |
| `invited` | list of Invite | a group you are in only (in `GET chat/{chat_id}/info`, and in the join and invite-accept replies, which are made after you joined): pending invites, oldest first, at most 500 |
| `invited_count` | integer | with `invited`: all pending invites |
| `invited_truncated` | boolean | with `invited`: there are more than 500 |

**Invite**: an entry of `invited`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `request_id` | string | the invite's id (for `users/requests/{request_id}/cancel`) |
| `user` | User ref + `online` | who is invited |
| `invited_by` | User ref or null | who invited them |
| `invited_at` | timestamp | |

**Group**: an entry of `GET chats/groups`, `group` in `POST chats`, and `group` in
`chats/group_created` (there without `joined`).

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id`, `kind` (`"group"`), `is_dm` (`false`), `name`, `description`, `created_at`, `created_by` | | as in Chat info |
| `member_count`, `online_count` | integer | |
| `members` | list of Member | in `GET chats/groups` at most 100 per group |
| `members_truncated` | boolean | |
| `joined` | boolean | you are a member |

**Request**: a DM request or a group invite.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `request_id` | string | DM request: the DM's `chat_id`. Invite: the membership id. |
| `kind` | string | `"dm"` or `"invite"` |
| `status` | string | `"pending"`, `"accepted"`, `"declined"` or `"cancelled"` |
| `from` | User ref or null | the sender: who sent the first DM, or who invited |
| `to` | User ref | the receiver, who must answer |
| `created_at` | timestamp | |
| `responded_at` | timestamp or null | `null` while pending |
| `message` | string | `kind` `"dm"` only: the first message, which the DM room starts with |
| `chat` | object | `kind` `"invite"` only: `{"chat_id", "name"}` of the group |

A declined or cancelled request is deleted (a DM request together with its first message), so
`"declined"` and `"cancelled"` appear only in the reply to that answer and in `users/request`.
Afterwards its `request_id` answers `253 REQUEST_NOT_FOUND`. An accepted request stays readable
with `"accepted"`; answering it again gives `252 REQUEST_CLOSED`.

---

## 2. The 11 calls of the frontend README

Each call starts with what changed compared with the README's one-line description.

### 2.1 `POST users`: register

Auth: none. One request.

**Changed**: nothing in the message — the README's `{username, password}` is exactly what this
call takes. The password is in the message body as clear text on an unencrypted socket; the
server derives PBKDF2-HMAC-SHA256(password, 16 random bytes, `SCRAM_ITERATIONS`) and stores only
the hash. A new account has the default colours and no session: log in afterwards.

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `username` | string | yes | 3–32 characters from `A–Z a–z 0–9 _ . -` after trimming; unique without regard to case |
| `password` | string | yes | 1–256 characters, not trimmed (spaces count), no control characters. The server sets no strength rule: any non-empty password is accepted, so whatever the GUI lets the user type is stored. Unicode is normalized with NFKC before hashing, so a composed and a decomposed accent are the same password. |
| `device_id` | string | no | 8–128 characters from `A–Z a–z 0–9 _ . : -`. The reference client sends `scram.device_fingerprint()`: the first 32 hex characters of SHA-256 over `"socketcord-device:"` followed by the MAC address (`uuid.getnode()`) as 12 lower-case hex digits. It is for the one-account-per-device-per-day rule ([PROTOCOL.md](PROTOCOL.md) section 5). |

**Reply** (`path`: `login/created`)

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user_id` | string | the new account |
| `username` | string | the name as stored (the case that was typed, trimmed) |

No session token: the client logs in next. The reply has no password field, and neither has any
other reply in this file.

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | `username` or `password` is missing or malformed (`field` names it) |
| 250 | `INVALID_USERNAME` | the username breaks the rules (`field`: `"username"`) |
| 252 | `USERNAME_TAKEN` | the name is taken, whatever its letter case (`field`: `"username"`) |
| 252 | `RATE_LIMITED` | this address asked to register too often (`REGISTER_RATE_PER_MINUTE`, section 1.7), or this device id or address already made `REG_LIMIT_DEVICE_MAX` / `REG_LIMIT_IP_MAX` accounts within `REG_LIMIT_WINDOW_HOURS`; `retry_after` says how long to wait |
| | common errors | section 1.5 (the authentication errors do not apply) |

**Pushes**: none.

**Example**. `POST users` without credentials:

```json
{"rid": 1, "username": "alice", "password": "correct horse", "device_id": "device-alice-01"}
```

```json
{
  "path": "login/created",
  "method": "POST",
  "rid": 1,
  "user_id": "5e3aa6d9-8936-40ff-b9ee-c3278968b871",
  "username": "alice"
}
```

### 2.2 `POST users/login`: log in

Auth: none. One request.

**Changed**: nothing in the message — `{username, password}`, as in the README. The reply carries
the `user_id` and the `session_token` that every later request puts in its header, the account's
name and colours at the top level, and the whole `user` object. An account has one session: this
login ends the previous one on every other connection, which get `login/revoked` with reason
`logged_in_elsewhere`.

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `username` | string | yes | as in register; letter case does not matter, and the name is trimmed |
| `password` | string | yes | 1–256 characters, as in register |

**Reply** (`path`: `login/success`)

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user_id` | string | the account |
| `session_token` | string | UUID; put it in the header of every later request |
| `expires_at` | timestamp | when the session runs out (`SESSION_TTL_HOURS`, default 168) |
| `username` | string | the name as stored |
| `text_color` | string | `"#RRGGBB"`, upper case |
| `background` | string | `"#RRGGBB"`, upper case |
| `user` | User | the same account as one object, with `online` and `created_at` |

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | `username` or `password` is missing or malformed |
| 250 | `INVALID_USERNAME` | the username cannot be one: it breaks the character or length rules |
| 251 | `INVALID_CREDENTIALS` | **wrong password, or no such account** — the same code and the same `detail` ("wrong username or password") for both, so the reply does not say which accounts exist. An unknown name also costs the same PBKDF2 run, so it cannot be told apart by how long the reply takes. |
| 252 | `RATE_LIMITED` | too many failed logins for this (username, address), or too many for unknown names from this address (section 1.7); `retry_after` |
| 251 | `INVALID_SESSION` | the account logged in somewhere else at the same moment, so this session was already replaced; log in again |
| | common errors | section 1.5 (the authentication errors do not apply) |

**Pushes**: `login/revoked` (reason `logged_in_elsewhere`) to every connection that still used the
previous session; `users/status` to everyone else if the account was offline until now.

**Example**

```json
{"rid": 2, "username": "alice", "password": "correct horse"}
```

```json
{
  "path": "login/success",
  "method": "POST",
  "rid": 2,
  "user_id": "5e3aa6d9-8936-40ff-b9ee-c3278968b871",
  "session_token": "e595f692-c2dc-465a-b330-b9f166133886",
  "expires_at": "2026-11-14T09:34:05.381+00:00",
  "username": "alice",
  "text_color": "#171717",
  "background": "#F2F2F2",
  "user": {
    "user_id": "5e3aa6d9-8936-40ff-b9ee-c3278968b871",
    "username": "alice",
    "text_color": "#171717",
    "background": "#F2F2F2",
    "online": true,
    "created_at": "2026-11-07T09:34:05.380+00:00"
  }
}
```

A wrong password and an unknown name both answer:

```json
{
  "path": "users/login",
  "method": "POST",
  "rid": 6,
  "error": "INVALID_CREDENTIALS",
  "detail": "wrong username or password"
}
```

### 2.3 `PATCH users/profile`: change your username and name colours

Auth: yes.

**Changed**: the profile is the username and the two colours of the team's data model (User Text
Color, User Text Background Color); there is no display name and no bio. The GUI draws every
username in its owner's text colour on its owner's background colour. `background` is accepted as
an older name for `background`.

**Request** (at least one of `username`, `text_color`, `background`, `background`)

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `username` | string | no | the new name: trimmed, then 3–32 characters from `A–Z a–z 0–9 _ . -`, the same rule as `POST users`. Unique without regard to case: a name someone else has (in any case) is refused; your own name in another case (`bob` → `Bob`) is fine, and so is your current name. |
| `text_color` | string | no | `"#rrggbb"`, either letter case; stored in lower case |
| `background` | string | no | the same |
| `background` | string | no | an older name for `background`. Sending both is allowed only with the same colour (letter case aside). |

After the change the two colours must differ. Everything sent is changed in one step: if
anything is refused, nothing changes.

**Effect of a rename**: the account, its id, its password, its session and every chat stay as they
were; everything that shows the user (members, message senders, DM names, requests, the online
list) shows the new name from now on. Stored system lines such as `"bob joined the group"` keep
the old text. Log in with the new name afterwards (any letter case); the old name stops working at
once, and someone else may register it. The password keeps working, because the stored hash does
not depend on the name.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user` | User | your account with the new name and colours |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `INVALID_FIELD` | `username` is not a string, or is empty or over 64 characters after trimming, or has a control character (`field`: `"username"`) |
| 250 | `INVALID_USERNAME` | `username` breaks the 3–32 `A–Z a–z 0–9 _ . -` rule (`field`: `"username"`) |
| 250 | `INVALID_FIELD` | a colour is not a string, is longer than 7 characters, or is not `#` and 6 hex digits (`field` names it) |
| 250 | `INVALID_FIELD` | `background` and `background` are both sent with different colours (`field`: `"background"`) |
| 250 | `MISSING_FIELD` | none of the four fields was sent (`field`: `"text_color"`) |
| 250 | `INVALID_FIELD` | the text colour would equal the background colour (`field`: `"background"` or `"background"`, whichever was sent; else `"text_color"`) |
| 252 | `USERNAME_TAKEN` | another account has that name, in any letter case (`field`: `"username"`; the same error `POST users` gives) |
| 252 | `RATE_LIMITED` | more than `PROFILE_RATE_PER_MINUTE` changes (section 1.7) |
| 253 | `USER_NOT_FOUND` | the account no longer exists |
| | common errors | section 1.5 |

**Pushes**: `users/profile_changed` (section 4.8) to every logged-in connection except this one (your
other connections included), for a rename as for a colour change. `GET users/online` and later
`users/status` show the new name.

**Example**: bob renames himself and changes his colours at once:

```json
{"rid": 5, "username": "Bobby", "text_color": "#FFFFFF", "background": "#1F6FEB"}
```

```json
{
  "path": "profile/updated",
  "method": "PATCH",
  "rid": 5,
  "user": {
    "user_id": "2fb20bce-8f92-4892-9f7f-4349743f8b95",
    "username": "Bobby",
    "text_color": "#FFFFFF",
    "background": "#1F6FEB",
    "online": true,
    "created_at": "2026-11-07T07:22:23.959+00:00"
  }
}
```

carol tries to take alice's name (in another case):

```json
{"rid": 5, "username": "ALICE"}
```

```json
{
  "path": "users/profile",
  "method": "PATCH",
  "rid": 5,
  "error": "USERNAME_TAKEN",
  "detail": "the name 'ALICE' is already taken",
  "field": "username"
}
```

### 2.4 `GET users/info`: your account

Auth: yes. **Changed**: `user` has `text_color` and `background` instead of a display name
and bio.

**Request**: no fields.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user` | User | your account (`online` is `true`) |

**Errors**: common errors only. **Pushes**: none.

**Example**

```json
{
  "path": "profile/info",
  "method": "GET",
  "rid": 5,
  "user": {
    "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
    "username": "alice",
    "text_color": "#171717",
    "background": "#F2F2F2",
    "online": true,
    "created_at": "2026-11-06T14:10:59.433+00:00"
  }
}
```

### 2.5 `GET chats`: the chats you are in

Auth: yes.

**Changed**: each entry is a Chat item (section 1.8) with the fields the GUI's chat list needs
(`chat_id`, `name`, `is_dm`, `pending`, `unread`, `last_activity`), the list also holds the group
invitations you have not answered (`pending`: `true`), and it is paged.

**Request**

| Field | Type | Default | Rules |
| ----- | ---- | ------- | ----- |
| `count` | integer | 200 | 1–200 |
| `offset` | integer | 0 | 0–10,000,000 |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chats` | list of Chat item | the groups you joined, your accepted DMs, and the group invitations you have not answered (`pending`: `true`), latest activity first (for an invitation, when it was sent; on a tie, the newer last message first, then by `chat_id`) |
| `offset` | integer | as requested |
| `has_more` | boolean | more chats follow this page |

A `pending` row is an invitation, not a membership: the group's messages and info still answer
`252 NOT_A_MEMBER` until you accept it with `POST chat/{chat_id}/invite/accept` (section 3.16).
Show it greyed out, with Accept / Reject. The row carries `name`, `member_count` and
`last_activity` (when you were invited), and `unread` `0`, `last_message` `null`,
`last_read_message_id` `null`. At most 50 invitations are listed. Accepting one turns it into an
ordinary row; rejecting it, or the inviter cancelling it, removes it (and pushes
`chat/members_changed` with `change` `"invite_cancelled"`, section 4.6).

Pending **DM** requests are not rows here: a DM room does not exist until it is accepted, and
they only happen on a server started with `DM_REQUIRE_ACCEPT=true`. They are in
`GET users/requests` (section 3.4) and arrive as `users/request` (section 4.3).

**Errors**: `250 INVALID_FIELD` (`count`, `offset`); common errors. **Pushes**: none.

**Example**: alice has a DM with bob and a group.

```json
{
  "path": "chats/list",
  "method": "GET",
  "rid": 38,
  "chats": [
    {
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "dm",
      "is_dm": true,
      "name": "bob",
      "pending": false,
      "description": "",
      "role": "member",
      "member_count": 2,
      "unread": 0,
      "last_read_message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
      "last_message": {
        "id": "01a1b132-e6df-7000-a64f-067660850d0b",
        "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
        "kind": "text",
        "text": "thanks!",
        "time": "2026-11-06T14:11:05.567+00:00",
        "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "sender": "alice"
      },
      "last_activity": "2026-11-06T14:11:05.567+00:00",
      "peer": {
        "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
        "username": "bob",
        "text_color": "#FFFFFF",
        "background": "#1F6FEB",
        "online": true
      }
    },
    {
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "group",
      "is_dm": false,
      "name": "Network Project",
      "pending": false,
      "description": "term project",
      "role": "owner",
      "member_count": 3,
      "unread": 1,
      "last_read_message_id": "01a1b132-d6fa-7000-8c92-ebb7ec7525b9",
      "last_message": {
        "id": "01a1b132-da2d-7000-9580-a1c0b24aabab",
        "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
        "kind": "system",
        "text": "carol joined the group",
        "time": "2026-11-06T14:11:02.317+00:00",
        "sender_id": null,
        "sender": null
      },
      "last_activity": "2026-11-06T14:11:02.317+00:00"
    }
  ],
  "offset": 0,
  "has_more": false
}
```

### 2.6 `GET chat/{chat_id}/info`: a chat's details and members

Auth: yes. Groups: anyone logged in (non-members see `joined: false` and no `invited`). DMs: only
their two people.

**Changed**: members carry their colours, `role` and `online`. For a group you are in, `invited`
lists the pending invites, so "the user list with status" is `members` (with `online`) plus
`invited`. For a DM, `name` and `peer` are the other person.

**Request**: no fields; the id is in the URL.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat` | Chat info | section 1.8 |

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in (other people's DMs, and DM requests that are not accepted yet, look exactly like a chat that does not exist) |
| | common errors | section 1.5 |

**Pushes**: none.

**Example**: a group, asked by a member, with one pending invite:

```json
{
  "path": "chat/info",
  "method": "GET",
  "rid": 21,
  "chat": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "created_by": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-06T14:11:00.277+00:00",
    "member_count": 2,
    "online_count": 2,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "background": "#F2F2F2",
        "role": "owner",
        "joined_at": "2026-11-06T14:11:00.277+00:00",
        "online": true
      },
      {
        "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
        "username": "bob",
        "text_color": "#FFFFFF",
        "background": "#1F6FEB",
        "role": "member",
        "joined_at": "2026-11-06T14:11:00.684+00:00",
        "online": true
      }
    ],
    "members_truncated": false,
    "joined": true,
    "last_read_message_id": "01a1b132-d6fa-7000-8c92-ebb7ec7525b9",
    "invited": [
      {
        "request_id": "04d5ffc9-faa5-48f3-9d60-3b7a3fdae037",
        "user": {
          "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
          "username": "carol",
          "text_color": "#171717",
          "background": "#F2F2F2",
          "online": true
        },
        "invited_by": {
          "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
          "username": "alice",
          "text_color": "#171717",
          "background": "#F2F2F2"
        },
        "invited_at": "2026-11-06T14:11:01.910+00:00"
      }
    ],
    "invited_count": 1,
    "invited_truncated": false
  }
}
```

A DM, asked by one of its two people:

```json
{
  "path": "chat/info",
  "method": "GET",
  "rid": 34,
  "chat": {
    "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "is_dm": true,
    "name": "bob",
    "description": "",
    "created_by": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-06T14:11:04.757+00:00",
    "member_count": 2,
    "online_count": 2,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "background": "#F2F2F2",
        "role": "member",
        "joined_at": "2026-11-06T14:11:05.164+00:00",
        "online": true
      },
      {
        "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
        "username": "bob",
        "text_color": "#FFFFFF",
        "background": "#1F6FEB",
        "role": "member",
        "joined_at": "2026-11-06T14:11:05.164+00:00",
        "online": true
      }
    ],
    "members_truncated": false,
    "joined": true,
    "last_read_message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
    "peer": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB",
      "online": true
    }
  }
}
```

### 2.7 `GET chat/{chat_id}/messages`: message history

Auth: members only.

**Changed**: the README's filters `count`, `content` and `time` work as described below (the
content filter is now spelled `text`, as in the message), plus `before_id` and `after_id` (UUID
message ids). The reply adds your read marker and `first_unread_id` (NOT
`first_unread_message_id`), which tells the GUI where to draw its NEW line.

**Request** (every field optional; all given filters apply together)

| Field | Type | Default | Rules |
| ----- | ---- | ------- | ----- |
| `count` | integer | 50 | 1–200 |
| `text` | string | | 1–50 characters after trimming; messages whose text contains it, ignoring case; `%` and `_` match themselves. `content` is accepted as the older name. |
| `time` | timestamp, or object | | A timestamp: messages strictly before it. An object `{"before": t, "after": t}` (one or both keys, no others): messages strictly before and/or after. Formats in section 1.4. |
| `before_id` | string | | messages with a smaller id (older): "load older" |
| `after_id` | string | | messages with a larger id (newer): "catch up" after a reconnect |

Without a lower bound (`after_id`, `time.after`), or with both a lower and an upper bound, you
get the **newest** `count` messages that match. With only lower bounds you get the **oldest**
`count` messages after that point. Either way the list is oldest first.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | |
| `messages` | list of Message | oldest first |
| `has_more` | boolean | more matching messages exist beyond this page: older ones for a "newest" page, newer ones for an "after" page |
| `last_read_message_id` | string or null | your read marker |
| `first_unread_id` | string or null | the oldest message of the whole chat after your read marker (filters do not change it), or `null` if there is none. Draw NEW above it when it is in `messages`. |

Reading messages does not mark them as read; send `POST chat/{chat_id}/read`.

`first_unread_id` looks at every message after your marker, and `unread` in `GET chats`
only at those someone else sent. The two still agree, because your own messages are never after
your marker: sending one moves the marker to it, and the first message of a DM request counts as
read for the person who sent it.

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 252 | `NOT_A_MEMBER` | a group you have not joined |
| 250 | `INVALID_FIELD` | `count`, `before_id`, `after_id` or `text` is malformed; or `time` is not a timestamp, has a key other than `before`/`after`, or is out of range (`field` is `"time"`, `"time.before"` or `"time.after"`) |
| | common errors | section 1.5 |

**Pushes**: none.

**Example**. Request `GET chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/messages` with an empty
message (bob joined, then alice wrote twice):

```json
{
  "path": "chat/messages",
  "method": "GET",
  "rid": 13,
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "messages": [
    {
      "id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "system",
      "text": "bob joined the group",
      "time": "2026-11-06T14:11:00.684+00:00",
      "sender_id": null,
      "sender": null
    },
    {
      "id": "01a1b132-d564-7000-8685-b94d127a7451",
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "text",
      "text": "hello group",
      "time": "2026-11-06T14:11:01.092+00:00",
      "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "sender": "alice"
    },
    {
      "id": "01a1b132-d6fa-7000-8c92-ebb7ec7525b9",
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "text",
      "text": "  second  ",
      "time": "2026-11-06T14:11:01.498+00:00",
      "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "sender": "alice"
    }
  ],
  "has_more": false,
  "last_read_message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
  "first_unread_id": "01a1b132-d564-7000-8685-b94d127a7451"
}
```

Filter examples: `{"count": 20, "before_id": "01a1b132-d564-7000-8685-b94d127a7451"}` (older
page), `{"after_id": "01a1b132-d564-7000-8685-b94d127a7451"}` (newer messages),
`{"text": "deadline", "time": {"after": "2026-11-01T00:00:00+07:00"}}`.

### 2.8 `POST chat/{chat_id}/message`: send a message

Auth: members only.

**Changed**: the field is `text` (`content`, the README's word, is accepted as well), and the
reply carries the stored message. Every online member gets it at once as `chat/new_message`.

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `text` | string | yes | 1–4000 characters and not only whitespace; stored exactly as sent (not trimmed); newlines and tabs allowed |
| `content` | string | — | the older name for `text`; send one of the two. If both are sent, `content` wins and `text` is ignored. |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `message` | Message | the stored message (`kind` `"text"`) |

Your read marker moves to this message, so your own messages never count as unread.

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 252 | `NOT_A_MEMBER` | a group you have not joined |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | neither `text` nor `content` was sent (`field`: `"content"`), or the one that was sent is not a string, longer than 4000 characters, blank ("'text' cannot be blank") or has a control character (`field` names the spelling you used) |
| 252 | `RATE_LIMITED` | more than `MESSAGE_RATE_PER_MINUTE` (shared with DMs) |
| 253 | `NOT_FOUND` | the chat was deleted while sending |
| | common errors | section 1.5 |

**Pushes**: `chat/new_message` to every logged-in connection of the chat's members, except the
connection that sent it (your other connections get it).

**Example**

```json
{"rid": 12, "text": "hello group"}
```

```json
{
  "path": "chat/sent",
  "method": "POST",
  "rid": 12,
  "message": {
    "id": "01a1b132-d564-7000-8685-b94d127a7451",
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "text",
    "text": "hello group",
    "time": "2026-11-06T14:11:01.092+00:00",
    "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
    "sender": "alice"
  }
}
```

### 2.9 `POST users/{user_id}/dm`: send a direct message

Auth: yes. `{user_id}` is the person you write to.

**Changed**: the backend keeps one DM per pair of people (either direction). By default
(`DM_REQUIRE_ACCEPT=false`) a DM needs no approval: the first DM creates the room at once with your
message, and later ones (from either of you) go into the same room. With `DM_REQUIRE_ACCEPT=true`
on the server, the first DM instead creates a **DM request** that carries your message, and the
room opens when the other person accepts it (section 3.5). The reply always has the same five
keys.

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `message` | string | yes | 1–4000 characters after trimming (unlike `POST chat/{chat_id}/message`, leading and trailing whitespace is removed); newlines and tabs allowed. `text` and `content` are accepted as other names for it; `message` wins if more than one is sent. |

**What happens**, by the state of the pair's DM:

| State | Result | `status` | `request` |
| ----- | ------ | -------- | --------- |
| an open DM room | your message is posted in it | `"sent"` | `null` |
| no DM (default, `DM_REQUIRE_ACCEPT=false`) | the room is created and your message posted | `"sent"` | `null` |
| no DM, `DM_REQUIRE_ACCEPT=true` | a pending DM request that carries your message | `"pending"` | the new request |
| a pending request **they** sent you | your DM accepts it: the room opens with their first message and yours | `"sent"` | their request, now `"accepted"` |
| a pending request **you** sent, `DM_REQUIRE_ACCEPT=true` | refused: `252 ALREADY_REQUESTED` | | |
| a pending request **you** sent, `DM_REQUIRE_ACCEPT=false` (left over from a time the server required approval) | it opens: the room starts with its first message, then yours | `"sent"` | your request, now `"accepted"` |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `status` | string | `"sent"` or `"pending"` |
| `chat_id` | string or null | the DM room (`null` when pending) |
| `chat` | Chat item or null | the room as it appears in your `GET chats` (`null` when pending) |
| `messages` | list of Message | `"sent"` into a room (a new one included): your message. `"sent"` because a pending request opened: the room's newest 20 messages, oldest first, which are the request's first message and yours. `[]` when pending. |
| `request` | Request or null | see the table above. A pending request's `request_id` is also the future room's `chat_id`. |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | `message` (or `text`, or `content`) is missing, not a string, empty after trimming, over 4000 characters, or has a control character |
| 250 | `INVALID_TARGET` | `{user_id}` is you |
| 253 | `USER_NOT_FOUND` | no such user |
| 252 | `RATE_LIMITED` | over `MESSAGE_RATE_PER_MINUTE` (every DM counts); when a request would be created (`DM_REQUIRE_ACCEPT=true`), also over `REQUEST_RATE_PER_HOUR` |
| 252 | `ALREADY_REQUESTED` | `DM_REQUIRE_ACCEPT=true` only: your earlier DM request to them is still pending |
| 252 | `LIMIT_REACHED` | `DM_REQUIRE_ACCEPT=true` only: creating a request, but you already have `MAX_PENDING_REQUESTS` (100) pending requests sent, or they have 100 received |
| 252 | `TRY_AGAIN` | the pair's DM changed three times while the call ran (the other person was accepting, cancelling or writing at that moment); send it again |
| 253 | `NOT_FOUND` | the room or the other user disappeared while sending |
| | common errors | section 1.5 |

**Pushes**:

* Into an open room: `chat/new_message` to the other person (and to your other connections).
* New room (the default): `chats/new` (`dm_opened`) to every connection of both of you, this one
  included (before the reply), then `chat/new_message` with your message to both of you except this
  connection. Nothing older is replayed: the room starts with your message. These are the pushes
  the receiver needs to add the DM to their chat list and show the message.
* A pending request opens (theirs, or a left-over one of yours): `chats/new` (`dm_opened`), then
  `chat/new_message` with the request's first message, both to every connection of both of you, this
  one included (here they arrive before the reply); then `chat/new_message` with yours to both of you
  except this connection; then `users/request` (`accepted`) to them.
* New request (`DM_REQUIRE_ACCEPT=true`): `users/request` (`pending`) to them, only while the
  request is still pending (section 4.3).

**Example**. By default the first DM from alice to carol opens the room at once:

```json
{"rid": 7, "message": "hi carol"}
```

```json
{
  "path": "chats/dm",
  "method": "POST",
  "rid": 7,
  "status": "sent",
  "chat_id": "01ffdaf5-1ce5-4fdc-84be-3bb0e251b30b",
  "chat": {
    "chat_id": "01ffdaf5-1ce5-4fdc-84be-3bb0e251b30b",
    "kind": "dm",
    "is_dm": true,
    "name": "carol",
    "pending": false,
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b4e3-17d2-7000-bddb-d8069783aa66",
    "last_message": {
      "id": "01a1b4e3-17d2-7000-bddb-d8069783aa66",
      "chat_id": "01ffdaf5-1ce5-4fdc-84be-3bb0e251b30b",
      "kind": "text",
      "text": "hi carol",
      "time": "2026-11-07T07:22:24.082+00:00",
      "sender_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
      "sender": "alice"
    },
    "last_activity": "2026-11-07T07:22:24.082+00:00",
    "peer": {
      "user_id": "50245c20-966f-4dc8-a71e-dfe2de0d9802",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2",
      "online": true
    }
  },
  "messages": [
    {
      "id": "01a1b4e3-17d2-7000-bddb-d8069783aa66",
      "chat_id": "01ffdaf5-1ce5-4fdc-84be-3bb0e251b30b",
      "kind": "text",
      "text": "hi carol",
      "time": "2026-11-07T07:22:24.082+00:00",
      "sender_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
      "sender": "alice"
    }
  ],
  "request": null
}
```

With `DM_REQUIRE_ACCEPT=true`, the first DM from alice to bob becomes a request:

```json
{"rid": 27, "message": "hi bob, got a minute?"}
```

```json
{
  "path": "chats/dm",
  "method": "POST",
  "rid": 27,
  "status": "pending",
  "chat_id": null,
  "chat": null,
  "messages": [],
  "request": {
    "request_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "status": "pending",
    "from": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB"
    },
    "created_at": "2026-11-06T14:11:04.757+00:00",
    "responded_at": null,
    "message": "hi bob, got a minute?"
  }
}
```

After bob accepted, alice writes again and it goes into the room:

```json
{"rid": 32, "message": "thanks!"}
```

```json
{
  "path": "chats/dm",
  "method": "POST",
  "rid": 32,
  "status": "sent",
  "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
  "chat": {
    "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "is_dm": true,
    "name": "bob",
    "pending": false,
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
    "last_message": {
      "id": "01a1b132-e6df-7000-a64f-067660850d0b",
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "text",
      "text": "thanks!",
      "time": "2026-11-06T14:11:05.567+00:00",
      "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "sender": "alice"
    },
    "last_activity": "2026-11-06T14:11:05.567+00:00",
    "peer": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB",
      "online": true
    }
  },
  "messages": [
    {
      "id": "01a1b132-e6df-7000-a64f-067660850d0b",
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "text",
      "text": "thanks!",
      "time": "2026-11-06T14:11:05.567+00:00",
      "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "sender": "alice"
    }
  ],
  "request": null
}
```

With `DM_REQUIRE_ACCEPT=true`, carol had sent alice a DM request; alice answers with a DM of her
own, which accepts it:

```json
{"rid": 39, "message": "hey carol!"}
```

```json
{
  "path": "chats/dm",
  "method": "POST",
  "rid": 39,
  "status": "sent",
  "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
  "chat": {
    "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
    "kind": "dm",
    "is_dm": true,
    "name": "carol",
    "pending": false,
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b132-ea0e-7000-b9bd-9c36ac677b1e",
    "last_message": {
      "id": "01a1b132-ea0e-7000-b9bd-9c36ac677b1e",
      "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
      "kind": "text",
      "text": "hey carol!",
      "time": "2026-11-06T14:11:06.382+00:00",
      "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "sender": "alice"
    },
    "last_activity": "2026-11-06T14:11:06.382+00:00",
    "peer": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2",
      "online": true
    }
  },
  "messages": [
    {
      "id": "01a1b132-e87a-7000-871c-86ce2c21aef3",
      "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
      "kind": "text",
      "text": "hey alice",
      "time": "2026-11-06T14:11:05.978+00:00",
      "sender_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "sender": "carol"
    },
    {
      "id": "01a1b132-ea0e-7000-b9bd-9c36ac677b1e",
      "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
      "kind": "text",
      "text": "hey carol!",
      "time": "2026-11-06T14:11:06.382+00:00",
      "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "sender": "alice"
    }
  ],
  "request": {
    "request_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
    "kind": "dm",
    "status": "accepted",
    "from": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-06T14:11:05.978+00:00",
    "responded_at": "2026-11-06T14:11:06.381+00:00",
    "message": "hey alice"
  }
}
```

### 2.10 `POST chat/{chat_id}/invite`: invite someone to a group

Auth: any member of the group.

**Changed**: the invited user is not added. They get a pending invite (`users/request`) and
become a member only when they accept it, or when they join the group by themselves (R10).

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `user_id` | string | yes | UUID of the person to invite |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `status` | string | `"pending"` |
| `request` | Request | the invite (`kind` `"invite"`) |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 252 | `NOT_A_GROUP` | the chat is a DM you are in ("only groups take invites") |
| 252 | `NOT_A_MEMBER` | you are not in the group |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | `user_id` is missing or not a UUID |
| 250 | `INVALID_TARGET` | you invited yourself |
| 253 | `USER_NOT_FOUND` | no such user |
| 252 | `ALREADY_MEMBER` | they are already a member |
| 252 | `RATE_LIMITED` | over `REQUEST_RATE_PER_HOUR` |
| 252 | `LIMIT_REACHED` | you have 100 pending requests sent, or they have 100 received |
| 252 | `ALREADY_INVITED` | they already have a pending invite to this group (from anyone) |
| 253 | `NOT_FOUND` | the group was deleted meanwhile |
| | common errors | section 1.5 |

**Pushes**: `users/request` (`pending`) to the invited user, only while the invite is still
pending (section 4.3).

The invite ends when the invited user accepts or declines it (section 3.5, 3.6), the inviter
cancels it (3.7), the invited user joins by themselves (3.11, which accepts it), or the group is
deleted (the invite disappears without `users/request`).

**Example**

```json
{"rid": 16, "user_id": "007204d5-49fb-41cc-b93c-593d8380c338"}
```

```json
{
  "path": "chat/invited",
  "method": "POST",
  "rid": 16,
  "status": "pending",
  "request": {
    "request_id": "04d5ffc9-faa5-48f3-9d60-3b7a3fdae037",
    "kind": "invite",
    "status": "pending",
    "from": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-06T14:11:01.910+00:00",
    "responded_at": null,
    "chat": {"chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4", "name": "Network Project"}
  }
}
```

### 2.11 `PATCH users/password`: change your password

Auth: yes. One request. `PATCH users/profile/password` is the same call under its older URL;
both answer with `path` `profile/password`.

**Changed**: **no current password is asked for.** The GUI's pop-up has only New and Confirm, so
the call takes `new_password` alone and trusts the session: whoever holds a valid session token
may change the password of that account. The new password travels in the message body as clear
text, like every other password here. The call does **not** rotate the session token, so the
caller and its other connections keep working with the token they already have.

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `new_password` | string | yes | 1–256 characters, not trimmed, no control characters — the same rules as `password` in register. Any other field, `password` included, is ignored. |

The server stores PBKDF2-HMAC-SHA256 of it with a **fresh 16-byte salt** and `SCRAM_ITERATIONS`
rounds, in one transaction that also checks that the account's stored hash and session are still
the ones this request read. The old password stops working at once, everywhere.

**Reply** (`path`: `profile/password`)

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user_id` | string | your account |
| `changed` | boolean | always `true` |

There is no `session_token` in this reply: the old one is still the right one.

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | `new_password` is missing, not a string, empty, longer than 256 characters, or has a control character |
| 251 | `INVALID_SESSION` | the session ended while the change ran (a logout, or a login elsewhere); log in again |
| 252 | `TRY_AGAIN` | another connection of this account changed the password at the same moment; read the state and try again |
| | common errors | section 1.5 |

**Pushes**: none. Other devices are not logged out and are not told, because the session token
did not change; they keep working until the next login somewhere else ends the session.

**Example** (with credentials)

```json
{"rid": 4, "new_password": "a new one"}
```

```json
{
  "path": "profile/password",
  "method": "PATCH",
  "rid": 4,
  "user_id": "5e3aa6d9-8936-40ff-b9ee-c3278968b871",
  "changed": true
}
```

After this, `POST users/login` with the new password works and the old one answers
`251 INVALID_CREDENTIALS`.

---

## 3. Additional calls

These calls are not among the README's first 11. They are needed by the course requirements
(online list, groups, joining, DM requests) and by the GUI (logout, read markers, the AI summary,
"See Groups" on a member, the "Add people" search, and the Accept / Reject buttons of an invite:
sections 3.14 to 3.17).

### 3.1 `POST users/logout`: end your session

Auth: yes.

**Request**: no fields.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `logged_out` | boolean | always `true` |

The account's session ends: its token stops working on every connection. This connection is
logged out too, but the socket stays open, so you can log in again on it or close it.

**Errors**: common errors only.

**Pushes**: `login/revoked` with reason `logged_out` to every other connection that used
the same token; `users/status` (offline) to everyone else at once, if the account has no
logged-in connection left.

**Example**: `POST users/logout` with an empty message:

```json
{"path": "login/logout", "method": "POST", "rid": 46, "logged_out": true}
```

### 3.2 `GET users/online`: who is online

Auth: yes. Course requirement R4: the list includes you.

**Request**: no fields.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `count` | integer | number of entries |
| `users` | list | every account with a logged-in connection, sorted by username ignoring case; each is a User without `created_at`, plus `is_self` (boolean). Someone who lost their last connection less than 2 s ago is still listed. |
| `presence_seq` | integer | the `seq` of the newest `users/status` sent so far; ignore any `users/status` whose `seq` is not greater |

**Errors**: common errors only. **Pushes**: none.

**Example**

```json
{
  "path": "users/online",
  "method": "GET",
  "rid": 6,
  "count": 3,
  "users": [
    {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2",
      "online": true,
      "is_self": true
    },
    {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB",
      "online": true,
      "is_self": false
    },
    {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2",
      "online": true,
      "is_self": false
    }
  ],
  "presence_seq": 3
}
```

### 3.3 `GET users/search`: find users by name

Auth: yes. Finds anyone with an account, online or not, for example to invite them or DM them.

**Request**

| Field | Type | Default | Rules |
| ----- | ---- | ------- | ----- |
| `query` | string | `""` | 0–32 characters after trimming. Matches usernames that start with it, ignoring case; `_`, `.`, `-` and `%` match themselves. `""` matches everyone. |
| `count` | integer | 20 | 1–50 |
| `offset` | integer | 0 | 0–10,000,000 |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `query` | string | the query as used (trimmed) |
| `offset` | integer | as requested |
| `has_more` | boolean | more matches follow this page |
| `users` | list | each a User without `created_at`, plus `is_self`. An exact match (ignoring case) comes first, then the rest by lower-case username in code-point order (`dan`, `dan.b`, `dan_c`, `dana`). You are included when you match. |

**Errors**: `250 INVALID_FIELD` (`query`, `count`, `offset`); common errors. **Pushes**: none.

**Example**

```json
{"rid": 7, "query": "b"}
```

```json
{
  "path": "users/search",
  "method": "GET",
  "rid": 7,
  "query": "b",
  "offset": 0,
  "has_more": false,
  "users": [
    {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB",
      "online": true,
      "is_self": false
    }
  ]
}
```

### 3.4 `GET users/requests`: your pending DM requests and invites

Auth: yes.

**Request**: no fields.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `incoming` | list of Request | pending DM requests and invites addressed to you (accept or decline them) |
| `outgoing` | list of Request | pending ones you sent (cancel them) |

Both lists hold every pending request, oldest first (by `created_at`, then `request_id`); this
call does not cut them short. Their length is held down only by the `MAX_PENDING_REQUESTS` check
made when a request is sent (normally 100, section 1.7), so a list can be longer after that
setting was lowered or turned off, or when several requests were sent at the same moment.

**Errors**: common errors only. **Pushes**: none.

**Example**: bob has a DM request from alice and an invite from carol:

```json
{
  "path": "users/requests",
  "method": "GET",
  "rid": 6,
  "incoming": [
    {
      "request_id": "13485fdb-feeb-45f4-9195-63b67366fd60",
      "kind": "dm",
      "status": "pending",
      "from": {
        "user_id": "21381bcb-f07d-4310-9e63-353629ac9fef",
        "username": "alice",
        "text_color": "#171717",
        "background": "#F2F2F2"
      },
      "to": {
        "user_id": "fd507003-8c4e-4d14-a2b6-56650d0e04c9",
        "username": "bob",
        "text_color": "#171717",
        "background": "#F2F2F2"
      },
      "created_at": "2026-11-06T14:11:25.032+00:00",
      "responded_at": null,
      "message": "hi bob, got a minute?"
    },
    {
      "request_id": "09c8e629-727b-4311-b4f7-39b94de09377",
      "kind": "invite",
      "status": "pending",
      "from": {
        "user_id": "06917463-4dec-44d8-a06f-67a413b75628",
        "username": "carol",
        "text_color": "#171717",
        "background": "#F2F2F2"
      },
      "to": {
        "user_id": "fd507003-8c4e-4d14-a2b6-56650d0e04c9",
        "username": "bob",
        "text_color": "#171717",
        "background": "#F2F2F2"
      },
      "created_at": "2026-11-06T14:11:25.033+00:00",
      "responded_at": null,
      "chat": {"chat_id": "00a342ef-338e-4da6-bae3-8af0deed368e", "name": "Lab 7"}
    }
  ],
  "outgoing": []
}
```

### 3.5 `POST users/requests/{request_id}/accept`: accept a DM request or invite

Auth: the receiver (`to`) of the request. An invite can also be accepted by its group's id
(section 3.16).

**Request**: no fields.

**Effect**: a DM request opens the DM room; its first message is the request's message, which
counts as unread for you (and as read for its sender). An invite makes you a member of the group:
the system line `"<you> joined the group"` is added, and your read marker moves to the newest
message, so the history from before you joined is not unread.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `request` | Request | `status` `"accepted"`, with `responded_at` |
| `chat` | Chat item or null | the DM room or the group as it appears in your `GET chats` (`null` only if it was deleted at that moment) |
| `info` | Chat info | invites only: the group's details and members, exactly as `GET chat/{chat_id}/info` returns them (with `invited`, `invited_count` and `invited_truncated`); left out if the group was deleted at that moment |

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `REQUEST_NOT_FOUND` | no such request; it is not addressed to you; it was declined or cancelled (those are deleted); or its group was deleted |
| 252 | `REQUEST_CLOSED` | it is already accepted ("this request is already accepted") |
| 253 | `NOT_FOUND` | `{request_id}` is not a UUID ("no such call") |
| | common errors | section 1.5 |

**Pushes**:

* DM request: `chats/new` (`dm_opened`), then `chat/new_message` with the first message, both to
  every connection of both people, this one included (here they arrive before the reply); then
  `users/request` (`accepted`) to the sender.
* Invite: `chat/new_message` (the system line) to every online member except this connection (your
  other connections get it; the reply carries the line as `chat.last_message`),
  `chat/members_changed` (`joined`) to everyone, this connection included, and `users/request`
  (`accepted`) to the inviter.

**Example**: bob accepts alice's DM request (`POST users/requests/99b362bb-971b-4f39-a15a-b95612bcd979/accept`):

```json
{
  "path": "users/requests/99b362bb-971b-4f39-a15a-b95612bcd979/accept",
  "method": "POST",
  "rid": 28,
  "request": {
    "request_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "status": "accepted",
    "from": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB"
    },
    "created_at": "2026-11-06T14:11:04.757+00:00",
    "responded_at": "2026-11-06T14:11:05.164+00:00",
    "message": "hi bob, got a minute?"
  },
  "chat": {
    "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "is_dm": true,
    "name": "alice",
    "pending": false,
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 1,
    "last_read_message_id": null,
    "last_message": {
      "id": "01a1b132-e3b5-7000-8839-be47fdf56d69",
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "text",
      "text": "hi bob, got a minute?",
      "time": "2026-11-06T14:11:04.757+00:00",
      "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "sender": "alice"
    },
    "last_activity": "2026-11-06T14:11:04.757+00:00",
    "peer": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2",
      "online": true
    }
  }
}
```

Accepting an invite answers the same way, with `kind` `"invite"`, the group in `chat`, and the
group's Chat info in `info`.

### 3.6 `POST users/requests/{request_id}/decline`: decline a DM request or invite

Auth: the receiver (`to`) of the request. An invite can also be declined by its group's id
(section 3.17).

**Request**: no fields.

**Effect**: the request is deleted. A declined DM request takes its DM and first message with it;
the sender may send a new one later.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `request` | Request | `status` `"declined"`, with `responded_at` |

**Errors**: as for accept (`253 REQUEST_NOT_FOUND`, `252 REQUEST_CLOSED`); common errors.

**Pushes**: `users/request` (`declined`) to the sender.

**Example**: bob declines a DM request from dave:

```json
{
  "path": "users/requests/91fe3a1f-8cf2-4911-8b91-dd574be2e9a7/decline",
  "method": "POST",
  "rid": 29,
  "request": {
    "request_id": "91fe3a1f-8cf2-4911-8b91-dd574be2e9a7",
    "kind": "dm",
    "status": "declined",
    "from": {
      "user_id": "cd952df1-fa2b-4930-b3b4-9f0c397e4fca",
      "username": "dave",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB"
    },
    "created_at": "2026-11-06T14:11:07.226+00:00",
    "responded_at": "2026-11-06T14:11:07.227+00:00",
    "message": "hi"
  }
}
```

### 3.7 `POST users/requests/{request_id}/cancel`: take back a DM request or invite

Auth: the sender (`from`) of the request.

**Request**: no fields.

**Effect**: the request is deleted, as with decline.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `request` | Request | `status` `"cancelled"`, with `responded_at` |

**Errors**: `253 REQUEST_NOT_FOUND` (no such request, you did not send it, or it is already
declined, cancelled or deleted with its group); `252 REQUEST_CLOSED` (already accepted); common
errors.

**Pushes**: `users/request` (`cancelled`) to the receiver.

**Example**: dave cancels his DM request to carol:

```json
{
  "path": "users/requests/c98da720-d0b7-4212-8d0c-0b83ff068ee3/cancel",
  "method": "POST",
  "rid": 7,
  "request": {
    "request_id": "c98da720-d0b7-4212-8d0c-0b83ff068ee3",
    "kind": "dm",
    "status": "cancelled",
    "from": {
      "user_id": "cd952df1-fa2b-4930-b3b4-9f0c397e4fca",
      "username": "dave",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-06T14:11:07.634+00:00",
    "responded_at": "2026-11-06T14:11:07.634+00:00",
    "message": "yo"
  }
}
```

### 3.8 `POST chats`: create a group

Auth: yes. Course requirement R8: the new group has only its creator as a member.

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `name` | string | yes | 1–64 characters after trimming, no control characters; does not have to be unique |
| `description` | string | no | 0–500 characters after trimming, default `""`; newlines and tabs allowed |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat` | Chat item | the group as it appears in your `GET chats` (`role` `"owner"`) |
| `group` | Group | the group as it appears in `GET chats/groups` (`joined` `true`) |

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | `name` or `description` is missing (name only), not a string, too long, blank, or has a control character |
| 252 | `RATE_LIMITED` | more than `GROUP_CREATE_PER_HOUR` (every new group is pushed to everyone online) |
| | common errors | section 1.5 |

**Pushes**: `chats/group_created` to every logged-in connection except this one.

**Example**

```json
{"rid": 10, "name": "Network Project", "description": "term project"}
```

```json
{
  "path": "chats/created",
  "method": "POST",
  "rid": 10,
  "chat": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "pending": false,
    "description": "term project",
    "role": "owner",
    "member_count": 1,
    "unread": 0,
    "last_read_message_id": null,
    "last_message": null,
    "last_activity": "2026-11-06T14:11:00.277+00:00"
  },
  "group": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "created_at": "2026-11-06T14:11:00.277+00:00",
    "created_by": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "member_count": 1,
    "online_count": 1,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "background": "#F2F2F2",
        "role": "owner",
        "joined_at": "2026-11-06T14:11:00.277+00:00",
        "online": true
      }
    ],
    "members_truncated": false,
    "joined": true
  }
}
```

### 3.9 `GET chats/groups`: every group on the server

Auth: yes. Course requirement R9: all groups with their members, whoever created them.

**Request**

| Field | Type | Default | Rules |
| ----- | ---- | ------- | ----- |
| `count` | integer | 100 | 1–200 |
| `offset` | integer | 0 | 0–10,000,000 |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `groups` | list of Group | oldest group first (by `created_at`, then `chat_id`); at most 100 members per group (`members_truncated`; `member_count` is always exact) |
| `offset` | integer | as requested |
| `has_more` | boolean | more groups follow this page |

A group always has at least one member: it is deleted when its last member leaves.

**Errors**: `250 INVALID_FIELD` (`count`, `offset`); common errors. **Pushes**: none.

**Example**: carol, who is in no group yet, lists the groups:

```json
{
  "path": "chats/groups",
  "method": "GET",
  "rid": 5,
  "groups": [
    {
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "group",
      "is_dm": false,
      "name": "Network Project",
      "description": "term project",
      "created_at": "2026-11-06T14:11:00.277+00:00",
      "created_by": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "background": "#F2F2F2"
      },
      "member_count": 1,
      "online_count": 1,
      "members": [
        {
          "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
          "username": "alice",
          "text_color": "#171717",
          "background": "#F2F2F2",
          "role": "owner",
          "joined_at": "2026-11-06T14:11:00.277+00:00",
          "online": true
        }
      ],
      "members_truncated": false,
      "joined": false
    }
  ],
  "offset": 0,
  "has_more": false
}
```

### 3.10 `POST chat/{chat_id}/read`: move your read marker

Auth: members only.

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `message_id` | string | no | UUID of the newest message you have seen; default: the chat's newest message. `id` (the message's own field name) is accepted as well. |

**Effect**: your read marker moves forward to the newest message of this chat whose id is at or
below `message_id` (without `message_id`: the chat's newest message). The marker is therefore
always a message of this chat: an id from another chat, or one that is no message at all, marks
this chat's messages up to that id as read and does not become the marker itself. The marker
never moves back: if that message is older than your marker, nothing changes. `unread` in
`GET chats` and `first_unread_id` count from this marker.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | |
| `last_read_message_id` | string or null | your marker after the call (`null` while nothing is read, for example in a chat without messages) |

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 252 | `NOT_A_MEMBER` | a group you have not joined |
| 250 | `INVALID_FIELD` | `message_id` (or `id`) is not a UUID |
| | common errors | section 1.5 |

**Pushes**: none. Your other connections are not told; their `GET chats` shows the new counts.

**Example**: `POST chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/read` with no `message_id`:

```json
{"rid": 19}
```

```json
{
  "path": "chat/read",
  "method": "POST",
  "rid": 19,
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "last_read_message_id": "01a1b132-d6fa-7000-8c92-ebb7ec7525b9"
}
```

### 3.11 `POST chat/{chat_id}/join`: join a group by yourself

Auth: yes. Course requirement R10: a client joins a group by itself.

**Request**: no fields.

**Effect**: you become a member. If you had a pending invite to this group, it becomes accepted
(the same as `POST chat/{chat_id}/invite/accept`, section 3.16). The system line
`"<you> joined the group"` is added, and your read marker moves to the newest message.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat` | Chat item | the group as it appears in your `GET chats` |
| `info` | Chat info | the group's details and members, exactly as `GET chat/{chat_id}/info` returns them (with `invited`, `invited_count` and `invited_truncated`) |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 252 | `NOT_A_GROUP` | a DM you are in ("DM rooms cannot be joined") |
| 252 | `ALREADY_MEMBER` | you are already in the group |
| 252 | `RATE_LIMITED` | more than `MEMBERSHIP_RATE_PER_MINUTE` joins and leaves |
| 253 | `NOT_FOUND` | the group was deleted meanwhile |
| | common errors | section 1.5 |

**Pushes**: `chat/new_message` (the system line) to every online member except this connection (your
other connections get it; the reply carries the line as `chat.last_message`);
`chat/members_changed` (`joined`) to everyone, this connection included (it arrives just before
the reply); and `users/request` (`accepted`) to the inviter if a pending invite was settled.

**Example**: bob joins alice's group:

```json
{
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/join",
  "method": "POST",
  "rid": 10,
  "chat": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "pending": false,
    "description": "term project",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
    "last_message": {
      "id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "system",
      "text": "bob joined the group",
      "time": "2026-11-06T14:11:00.684+00:00",
      "sender_id": null,
      "sender": null
    },
    "last_activity": "2026-11-06T14:11:00.684+00:00"
  },
  "info": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "created_by": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-06T14:11:00.277+00:00",
    "member_count": 2,
    "online_count": 2,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "background": "#F2F2F2",
        "role": "owner",
        "joined_at": "2026-11-06T14:11:00.277+00:00",
        "online": true
      },
      {
        "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
        "username": "bob",
        "text_color": "#FFFFFF",
        "background": "#1F6FEB",
        "role": "member",
        "joined_at": "2026-11-06T14:11:00.684+00:00",
        "online": true
      }
    ],
    "members_truncated": false,
    "joined": true,
    "last_read_message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
    "invited": [],
    "invited_count": 0,
    "invited_truncated": false
  }
}
```

### 3.12 `POST chat/{chat_id}/leave`: leave a group

Auth: members only.

**Request**: no fields.

**Effect**: your membership and read marker are deleted. If you were the last member, the group
is deleted with its messages and pending invites. Otherwise the system line
`"<you> left the group"` is added. If the creator joins again later, their `role` is `"owner"`
again.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | |
| `left` | boolean | always `true` |
| `chat_deleted` | boolean | `true` if you were the last member and the group is gone |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 252 | `NOT_A_GROUP` | a DM you are in ("DM rooms cannot be left") |
| 252 | `NOT_A_MEMBER` | you are not in the group |
| 252 | `RATE_LIMITED` | more than `MEMBERSHIP_RATE_PER_MINUTE` joins and leaves |
| | common errors | section 1.5 |

**Pushes**: if the group was deleted, `chats/group_deleted` to everyone (you included).
Otherwise `chat/new_message` (the system line) to the remaining online members and
`chat/members_changed` (`left`) to everyone, you included. Pending invites of a deleted group
disappear without `users/request`.

**Example**

```json
{
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/leave",
  "method": "POST",
  "rid": 37,
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "left": true,
  "chat_deleted": false
}
```

### 3.13 `GET chat/{chat_id}/summary`: AI summary of a chat

Auth: members only (the same check as `GET chat/{chat_id}/messages`). This is the rubric's Part 3
feature; the README's "AI summary" section explains the design.

**`POST chat/{chat_id}/summary`** (the method the frontend README lists) is the same call: the
same request fields, reply, errors, rate limit and cache. Only the reply head's `method` says
`"POST"`.

**Request**

| Field | Type | Default | Rules |
| ----- | ---- | ------- | ----- |
| `mode` | string | `"recent"` | `"recent"` or `"unread"` (lower case) |
| `count` | integer | 50 | 1–200 |
| `before_id` | string | | UUID; only with `"recent"`: summarize messages older than this one |
| `language` | string | `"auto"` | `"auto"` (any letter case): the language most messages are in. Otherwise 1–32 characters, a letter followed by letters, spaces or hyphens, such as `"th"`, `"en-US"`, `"Thai"`. |

**What is summarized**: the newest `count` messages of the chat (older than `before_id`, if
given). With `"unread"`, only those after your read marker. Asking for a summary does not mark
anything as read.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | |
| `mode` | string | as asked |
| `language` | string | `"auto"`, or as asked |
| `summary` | string or null | plain text, `- ` bullets, about 150 words at most; `null` when there was nothing to summarize (no AI call is made) |
| `message_count` | integer | messages the summary covers |
| `omitted` | integer | older messages of the window left out because the transcript would pass `AI_MAX_INPUT_CHARS` |
| `has_more` | boolean | older messages exist outside what was covered, `omitted` ones included. With `"unread"`, older messages beyond the window count only when every message in the window was unread, and they may already be read. |
| `engine` | string or null | `"claude"`; `"offline"` (not AI: used with `AI_PROVIDER=offline`, and with `auto` when there is no API key or the AI library could not be set up); or `null` when `summary` is `null` |
| `model` | string or null | the model that wrote it, such as `"claude-opus-5-5"` (a fallback model if one stepped in); `null` for the offline engine |
| `cached` | boolean | `true` if the same summary (same messages, reader and language) was made within `AI_CACHE_SECONDS` (600) and reused at no cost |
| `first_message_id`, `last_message_id` | string | the first and last message covered; only when `summary` is not `null` |
| `from_time`, `to_time` | timestamp | their times; only when `summary` is not `null` |
| `cut_short` | boolean | the model hit its output limit; only when `summary` is not `null` |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 252 | `NOT_A_MEMBER` | a group you have not joined |
| 250 | `INVALID_FIELD` | `mode`, `count`, `before_id` or `language` breaks its rule, or `before_id` was sent with `"unread"` |
| 254 | `AI_UNAVAILABLE` | summaries are turned off (`AI_PROVIDER=off`) or not set up on this server; no `retry_after` |
| 252 | `RATE_LIMITED` | more than `AI_SUMMARY_PER_MINUTE` |
| 252 | `AI_BUSY` | `AI_MAX_CONCURRENT` summaries are being written right now; `retry_after` 5. (A summary that is in the cache is still answered.) |
| 252 | `AI_QUOTA_USED` | the server already made `AI_DAILY_LIMIT` paid Claude calls this UTC day; `retry_after` counts to the next UTC midnight |
| 252 | `AI_DECLINED` | the model declined to summarize these messages |
| 254 | `AI_UNAVAILABLE` | the AI service refused the server's key or model setting (no `retry_after`), rate-limited the server (`retry_after` from the service, default 10), failed with a server error, timed out, or could not be reached (`retry_after` 5) |
| 254 | `AI_FAILED` | the AI service rejected the request or answered with something unusable (no `retry_after`), or returned an empty summary (`retry_after` 5) |
| | common errors | section 1.5 |

**Pushes**: none.

**Waiting**: a summary usually takes a few seconds, and at most about 2 × `AI_TIMEOUT_SECONDS`
(30 s) when the AI call is retried. Requests from one connection are answered in order, so the
connection's next requests wait behind it (pushes still arrive). Give this call a long reply
timeout (the reference client waits 90 s), or send it on a second connection that uses the same
User ID and Session Token (never `users/login` there: a login ends the account's other
connections).

**Example**

```json
{"rid": 31, "mode": "recent", "count": 50}
```

A reply written by Claude:

```json
{
  "path": "chat/summary",
  "method": "GET",
  "rid": 31,
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "mode": "recent",
  "language": "auto",
  "summary": "- bob and carol joined the group.\n- alice said hello; no topic has been discussed yet.",
  "message_count": 4,
  "omitted": 0,
  "first_message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
  "last_message_id": "01a1b132-da2d-7000-9580-a1c0b24aabab",
  "from_time": "2026-11-06T14:11:00.684+00:00",
  "to_time": "2026-11-06T14:11:02.317+00:00",
  "has_more": false,
  "engine": "claude",
  "model": "claude-opus-5-5",
  "cached": false,
  "cut_short": false
}
```

The same request on a server without an API key (offline engine):

```json
{
  "path": "chat/summary",
  "method": "GET",
  "rid": 31,
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "mode": "recent",
  "language": "auto",
  "summary": "Offline summary (not written by AI: the server has no AI key) of 4 messages, 2026-11-06 14:11 to 14:11 UTC.\nWho wrote: alice 2\nLatest:\n- alice: hello group\n- alice: second",
  "message_count": 4,
  "omitted": 0,
  "first_message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
  "last_message_id": "01a1b132-da2d-7000-9580-a1c0b24aabab",
  "from_time": "2026-11-06T14:11:00.684+00:00",
  "to_time": "2026-11-06T14:11:02.317+00:00",
  "has_more": false,
  "engine": "offline",
  "model": null,
  "cached": false,
  "cut_short": false
}
```

Nothing to summarize (`"unread"` with no unread messages):

```json
{
  "path": "chat/summary",
  "method": "GET",
  "rid": 6,
  "chat_id": "d3e539df-54fd-434c-82fe-c0fcb466cf9f",
  "mode": "unread",
  "language": "auto",
  "summary": null,
  "message_count": 0,
  "omitted": 0,
  "has_more": false,
  "engine": null,
  "model": null,
  "cached": false
}
```

### 3.14 `GET users/{user_id}/groups`: the groups a user is in

Auth: yes. `{user_id}` is anyone's id, yours included (the GUI's "See Groups" on a member).

Groups and their members are public on this server: `GET chats/groups` (section 3.9) shows every
group with its member list to everyone logged in (R9). So this call lists every group the user is
an accepted member of, whoever asks. Pending invites are not memberships and are not listed.

**Request**: no fields.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user_id` | string | `{user_id}` |
| `user` | User ref + `online` | that user, with their current name and colours |
| `count` | integer | number of entries in `groups` |
| `groups` | list | every group they are in, by name without regard to case (then by `chat_id`); no paging |

An entry of `groups`:

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the group |
| `kind`, `is_dm` | | always `"group"`, `false` |
| `name`, `description`, `created_at` | | as in Group (section 1.8) |
| `member_count` | integer | accepted members |
| `role` | string | **that user's** role in it: `"owner"` (the creator) or `"member"` |
| `joined_at` | timestamp | when **that user** joined it |
| `joined` | boolean | **you** (the caller) are a member too |

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `USER_NOT_FOUND` | no such user |
| 253 | `NOT_FOUND` | `{user_id}` is not a UUID ("no such call") |
| | common errors | section 1.5 |

**Pushes**: none. Keep a shown list current with `chat/members_changed` (`joined` / `left`, whose
`user` is that user) and `chats/group_deleted`, or ask again.

**Example**: carol asks for alice's groups:

```json
{
  "path": "users/groups",
  "method": "GET",
  "rid": 6,
  "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
  "user": {
    "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
    "username": "alice",
    "text_color": "#171717",
    "background": "#F2F2F2",
    "online": true
  },
  "count": 2,
  "groups": [
    {
      "chat_id": "d423ebd8-5858-48d3-a1f7-878576ba8aaf",
      "kind": "group",
      "is_dm": false,
      "name": "Book Club",
      "description": "",
      "created_at": "2026-11-07T07:22:24.080+00:00",
      "member_count": 1,
      "role": "owner",
      "joined_at": "2026-11-07T07:22:24.080+00:00",
      "joined": false
    },
    {
      "chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030",
      "kind": "group",
      "is_dm": false,
      "name": "Network Project",
      "description": "term project",
      "created_at": "2026-11-07T07:22:24.080+00:00",
      "member_count": 2,
      "role": "owner",
      "joined_at": "2026-11-07T07:22:24.080+00:00",
      "joined": false
    }
  ]
}
```

### 3.15 `GET chat/{chat_id}/invitees`: who you can invite to a group

Auth: any member of the group. Feeds the GUI's "Add people" search; invite the picked person with
`POST chat/{chat_id}/invite` (section 2.10).

**Who is listed**: people who are **online now** (as in `GET users/online`) **or** have an
**open DM with you** (an accepted DM room; a pending DM request does not count), whose username
contains `query`, except:

* you;
* the group's members;
* anyone with a pending invite to this group (from any member).

Sorted by username without regard to case; at most 50 people.

**Request**

| Field | Type | Default | Rules |
| ----- | ---- | ------- | ----- |
| `query` | string | `""` | 0–32 characters after trimming. Matches usernames that contain it anywhere, ignoring case; `_`, `.`, `-` and `%` match themselves. `""` matches everyone. |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the group |
| `query` | string | the query as used (trimmed) |
| `count` | integer | number of entries in `users` |
| `users` | list | each `{"user_id", "username", "text_color", "background", "online"}` (a User without `created_at`); at most 50 |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 250 | `NOT_A_GROUP` | the chat is a DM you are in ("only groups take invites") |
| 252 | `NOT_A_MEMBER` | you are not in the group |
| 250 | `INVALID_FIELD` | `query` is not a string, is over 32 characters, or has a control character (`field`: `"query"`) |
| | common errors | section 1.5 |

**Pushes**: none. The list is a snapshot: someone may come online, or be invited by another
member, a moment later. `POST chat/{chat_id}/invite` still checks everything (an already invited
person gives `252 ALREADY_INVITED`).

**Example**: alice (in "Network Project" with Bobby) searches with an empty query; carol and dave
are online:

```json
{"rid": 8, "query": ""}
```

```json
{
  "path": "chat/50899369-a177-46e6-8cd5-1bb6da9c2030/invitees",
  "method": "GET",
  "rid": 8,
  "chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030",
  "query": "",
  "count": 2,
  "users": [
    {
      "user_id": "50245c20-966f-4dc8-a71e-dfe2de0d9802",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2",
      "online": true
    },
    {
      "user_id": "c2cd251f-8294-4fd9-ba19-0601dcd08fdd",
      "username": "dave",
      "text_color": "#171717",
      "background": "#F2F2F2",
      "online": true
    }
  ]
}
```

With `{"query": "CA"}` only carol is left. dave, who is not a member, gets:

```json
{
  "path": "chat/50899369-a177-46e6-8cd5-1bb6da9c2030/invitees",
  "method": "GET",
  "rid": 7,
  "error": "NOT_A_MEMBER",
  "detail": "join this group first"
}
```

### 3.16 `POST chat/{chat_id}/invite/accept`: accept your invite to a group

Auth: yes; you must have a pending invite to the group.

The same as `POST users/requests/{request_id}/accept` (section 3.5) on that invite, addressed by
the group's `chat_id` instead of the invite's `request_id`, and the same as joining the group by
yourself while invited (section 3.11).

**Request**: no fields.

**Effect**: the invite becomes your membership. The system line `"<you> joined the group"` is
added, and your read marker moves to the newest message, so the history from before you joined
is not unread.

**Reply**: the reply of `POST chat/{chat_id}/join`, plus the accepted invite.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat` | Chat item | the group as it appears in your `GET chats` |
| `info` | Chat info | the group's details and members, exactly as `GET chat/{chat_id}/info` returns them (with `invited`, `invited_count` and `invited_truncated`) |
| `request` | Request | the invite, `status` `"accepted"`, with `responded_at` |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 253 | `REQUEST_NOT_FOUND` | you have no pending invite to this chat: never invited; the invite was declined, cancelled or deleted with its group; you are already a member ("you are already in this group; there is no invitation to answer"); or the chat is a DM you are in. Remove the invite from the list. |
| 252 | `RATE_LIMITED` | more than `MEMBERSHIP_RATE_PER_MINUTE` joins, leaves and accepted invites (section 1.7) |
| 253 | `REQUEST_NOT_FOUND` | the inviter cancelled it at that very moment ("the invitation was cancelled or answered a moment ago") |
| 253 | `NOT_FOUND` | `{chat_id}` is not a UUID ("no such call"), or the group was deleted meanwhile |
| | common errors | section 1.5 |

**Pushes**: the same as accepting an invite in section 3.5: `chat/new_message` (the system line) to
every online member except this connection, `chat/members_changed` (`joined`) to everyone, this
connection included, and `users/request` (`accepted`) to the inviter.

**Example**: carol accepts alice's invite to "Network Project"
(`POST chat/50899369-a177-46e6-8cd5-1bb6da9c2030/invite/accept`, empty message):

```json
{
  "path": "chat/joined",
  "method": "POST",
  "rid": 7,
  "chat": {
    "chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "pending": false,
    "description": "term project",
    "role": "member",
    "member_count": 3,
    "unread": 0,
    "last_read_message_id": "01a1b4e3-17e5-7000-8a61-735f1072fb12",
    "last_message": {
      "id": "01a1b4e3-17e5-7000-8a61-735f1072fb12",
      "chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030",
      "kind": "system",
      "text": "carol joined the group",
      "time": "2026-11-07T07:22:24.101+00:00",
      "sender_id": null,
      "sender": null
    },
    "last_activity": "2026-11-07T07:22:24.101+00:00"
  },
  "info": {
    "chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "created_by": {
      "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-07T07:22:24.080+00:00",
    "member_count": 3,
    "online_count": 3,
    "members": [
      {
        "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
        "username": "alice",
        "text_color": "#171717",
        "background": "#F2F2F2",
        "role": "owner",
        "joined_at": "2026-11-07T07:22:24.080+00:00",
        "online": true
      },
      {
        "user_id": "2fb20bce-8f92-4892-9f7f-4349743f8b95",
        "username": "Bobby",
        "text_color": "#FFFFFF",
        "background": "#1F6FEB",
        "role": "member",
        "joined_at": "2026-11-07T07:22:24.081+00:00",
        "online": true
      },
      {
        "user_id": "50245c20-966f-4dc8-a71e-dfe2de0d9802",
        "username": "carol",
        "text_color": "#171717",
        "background": "#F2F2F2",
        "role": "member",
        "joined_at": "2026-11-07T07:22:24.101+00:00",
        "online": true
      }
    ],
    "members_truncated": false,
    "joined": true,
    "last_read_message_id": "01a1b4e3-17e5-7000-8a61-735f1072fb12",
    "invited": [],
    "invited_count": 0,
    "invited_truncated": false
  },
  "request": {
    "request_id": "e11125d6-a6d7-4aad-9cd0-5f123b0ce93d",
    "kind": "invite",
    "status": "accepted",
    "from": {
      "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "50245c20-966f-4dc8-a71e-dfe2de0d9802",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-07T07:22:24.101+00:00",
    "responded_at": "2026-11-07T07:22:24.101+00:00",
    "chat": {"chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030", "name": "Network Project"}
  }
}
```

Asking again, now that carol is a member:

```json
{
  "path": "chat/50899369-a177-46e6-8cd5-1bb6da9c2030/invite/accept",
  "method": "POST",
  "rid": 8,
  "error": "REQUEST_NOT_FOUND",
  "detail": "you are already in this group; there is no invitation to answer"
}
```

### 3.17 `POST chat/{chat_id}/invite/reject`: decline your invite to a group

Auth: yes; you must have a pending invite to the group.

The same as `POST users/requests/{request_id}/decline` (section 3.6) on that invite, addressed by
the group's `chat_id`.

**Request**: no fields.

**Effect**: the invite is deleted; you do not join. Any member may invite you again later.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the group |
| `status` | string | always `"declined"` |
| `request` | Request | the invite, `status` `"declined"`, with `responded_at` |

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 253 | `REQUEST_NOT_FOUND` | you have no pending invite to this chat (as in section 3.16), or it was cancelled or answered at that very moment |
| 253 | `NOT_FOUND` | `{chat_id}` is not a UUID ("no such call") |
| | common errors | section 1.5 |

**Pushes**: `users/request` (`declined`) to the inviter.

**Example**: dave declines alice's invite to "Book Club":

```json
{
  "path": "chat/declined",
  "method": "POST",
  "rid": 5,
  "chat_id": "d423ebd8-5858-48d3-a1f7-878576ba8aaf",
  "status": "declined",
  "request": {
    "request_id": "a977db68-dc0b-4282-949b-1611910a6b5b",
    "kind": "invite",
    "status": "declined",
    "from": {
      "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "c2cd251f-8294-4fd9-ba19-0601dcd08fdd",
      "username": "dave",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-07T07:22:24.102+00:00",
    "responded_at": "2026-11-07T07:22:24.102+00:00",
    "chat": {"chat_id": "d423ebd8-5858-48d3-a1f7-878576ba8aaf", "name": "Book Club"}
  }
}
```

Again, with nothing left to decline:

```json
{
  "path": "chat/d423ebd8-5858-48d3-a1f7-878576ba8aaf/invite/reject",
  "method": "POST",
  "rid": 6,
  "error": "REQUEST_NOT_FOUND",
  "detail": "you have no pending invitation to this group"
}
```

---

## 4. Server pushes

A push is a reply packet nobody asked for: status 200, one of the nine `path` values below, and
**no `method` and no `rid`** — `"method" not in reply` is the test for "this is a push", and the
same list is `PUSH_PATHS` in `socketcord/protocol.py`. No push path is ever a request URL, so a
client may also route on `path` alone. Pushes go only to logged-in connections.

| `path` | When | Payload | Section |
| ------ | ---- | ------- | ------- |
| `chat/new_message` | somebody posted in a chat you are in | `chat_id`, `message` | 4.1 |
| `users/status` | somebody went online or offline | `user_id`, `online`, `user`, `seq` | 4.2 |
| `users/request` | a DM request or an invitation was made or answered | `request` | 4.3 |
| `chats/new` | you were invited to a group, or a DM was opened with you | `chat` | 4.4 |
| `chats/group_created` | a new group exists | `group` | 4.5 |
| `chat/members_changed` | a member joined, left, renamed or changed colours, or your invitation was cancelled | `chat_id`, `change` | 4.6 |
| `chats/group_deleted` | a group's last member left | `chat_id` | 4.7 |
| `users/profile_changed` | somebody changed their username or colours | `user` | 4.8 |
| `login/revoked` | your session ended somewhere else | `reason` | 4.9 |

The first four are the ones contract v2 names; the other five are the extra ones the GUI needs,
under the same naming style. In this section:

* "everyone" means every logged-in connection on the server;
* "not to the acting connection" means the connection whose request caused the push does not get
  it, while that user's other connections do.

Pushes about one chat leave the server in the order the changes were committed. For example,
`chats/group_created` comes before any `chat/new_message` or `chat/members_changed` of that group,
and a `"pending"` `users/request` never arrives after a newer state of the same request. On the
connection that made a call, the pushes that call causes for that connection arrive before its
reply. A client that stops reading is disconnected once more than 8 MB of pushes wait for it
([PROTOCOL.md](PROTOCOL.md) section 1).

### 4.1 `chat/new_message`: a new message in a chat

**When**: a message is stored: `POST chat/{chat_id}/message`, `POST users/{user_id}/dm` into a
room, the system lines of joining and leaving, and, when a DM room opens, the first message that
the DM request carried.

**To**: every logged-in connection of the chat's members (the two people of a DM), except:

* a text message is not sent to the connection that sent it (its reply carries it);
* a join line (`"bob joined the group"`) is not sent to the connection that joined (its reply
  carries the line as `chat.last_message`); the joiner's other connections get it;
* a leave line goes to the members who remain, so none of the leaver's connections get it.

When a DM request is accepted, its first message goes to every connection of both people, the
acting connection included, right after `chats/new`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the chat |
| `chat_kind` | string | `"group"` or `"dm"` |
| `message` | Message | |

```json
{
  "path": "chat/new_message",
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "chat_kind": "group",
  "message": {
    "id": "01a1b132-d564-7000-8685-b94d127a7451",
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "text",
    "text": "hello group",
    "time": "2026-11-06T14:11:01.092+00:00",
    "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
    "sender": "alice"
  }
}
```

### 4.2 `users/status`: someone came online or went offline

**When**:

* online: an account that had no logged-in connection gets one (a login, or the first
  authenticated request on a connection);
* offline: its last connection is logged out. This is immediate for a logout, a revoked or
  expired session, or a connection switching accounts. When the last connection closes, offline
  comes 2 s later, and not at all if the account reconnects within those 2 s.

**To**: everyone (for "online", not the connection that just logged in).

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user_id` | string | who it is about (contract v2 section 4) |
| `online` | boolean | |
| `user` | User without `created_at` | the same person as one object, with their current name and colours (`online` in it equals the field above); use it to add somebody to the online list without another call |
| `seq` | integer | grows by one with every presence push, server-wide (it starts again at 1 when the server restarts). Ignore a push whose `seq` is not greater than the newest you have, including `presence_seq` of `GET users/online`. |

```json
{
  "path": "users/status",
  "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
  "online": true,
  "user": {
    "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
    "username": "bob",
    "text_color": "#171717",
    "background": "#F2F2F2",
    "online": true
  },
  "seq": 2
}
```

```json
{
  "path": "users/status",
  "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
  "online": false,
  "user": {
    "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
    "username": "alice",
    "text_color": "#171717",
    "background": "#F2F2F2",
    "online": false
  },
  "seq": 5
}
```

### 4.3 `users/request`: a DM request or invite was made or answered

**To**, by cause (never to the acting connection):

| Cause | To | `status` |
| ----- | -- | -------- |
| `POST users/{user_id}/dm` creates a DM request (`DM_REQUIRE_ACCEPT=true`) | the receiver | `"pending"` |
| `POST chat/{chat_id}/invite` | the invited user | `"pending"` |
| `POST users/requests/{request_id}/accept` | the sender | `"accepted"` |
| `POST users/requests/{request_id}/decline` | the sender | `"declined"` |
| `POST users/requests/{request_id}/cancel` | the receiver | `"cancelled"` |
| `POST chat/{chat_id}/invite/accept` | the inviter | `"accepted"` |
| `POST chat/{chat_id}/invite/reject` | the inviter | `"declined"` |
| the receiver of a pending DM request sends the sender a DM, which accepts it | the sender | `"accepted"` |
| with `DM_REQUIRE_ACCEPT=false`, the sender of a left-over pending DM request sends another DM, which opens it | the receiver | `"accepted"` |
| the invited user joins the group by themselves | the inviter | `"accepted"` |

The answering user's own other connections are not told. A `"pending"` push is sent only while
the request is still pending, so it never arrives after a newer state of that request; a request
that was answered or cancelled at once may never be announced as `"pending"` at all. When a group
is deleted, its pending invites disappear without this push: drop the invites whose
`chat.chat_id` arrives in `chats/group_deleted`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `request` | Request | the request with its new status |

```json
{
  "path": "users/request",
  "request": {
    "request_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "status": "pending",
    "from": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB"
    },
    "created_at": "2026-11-06T14:11:04.757+00:00",
    "responded_at": null,
    "message": "hi bob, got a minute?"
  }
}
```

```json
{
  "path": "users/request",
  "request": {
    "request_id": "04d5ffc9-faa5-48f3-9d60-3b7a3fdae037",
    "kind": "invite",
    "status": "accepted",
    "from": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "to": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "created_at": "2026-11-06T14:11:01.910+00:00",
    "responded_at": "2026-11-06T14:11:02.317+00:00",
    "chat": {"chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4", "name": "Network Project"}
  }
}
```

### 4.4 `chats/new`: a chat joined your list

**When**: two cases.

1. **A DM opened**: the first DM between two people (`POST users/{user_id}/dm` with the default
   `DM_REQUIRE_ACCEPT=false`); or a pending DM request opens: its receiver accepts it or writes
   back, or (with `DM_REQUIRE_ACCEPT=false`) its sender writes again. `reason` is `"dm_opened"`.
2. **You were invited to a group** (`POST chat/{chat_id}/invite`): the row has `pending`: `true`
   and there is no `reason`. You are not a member yet; accept it with
   `POST chat/{chat_id}/invite/accept` (section 3.16) or reject it with `.../invite/reject`. The
   same row is in `GET chats`, so a client that was offline when the invitation arrived still
   sees it (section 2.5). `users/request` (section 4.3) carries the same event as a Request
   object, for clients that work with requests instead of rows.

**To**: for a DM, every logged-in connection of both people, the acting connection included, each
person with the chat as it appears in their own `GET chats`; for an invitation, every connection
of the invited person. It always arrives before the room's first `chat/new_message`, so the client
knows the `chat_id` first; on the acting connection it also arrives before the reply. An accepted
DM request's first message follows it as `chat/new_message`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat` | Chat item | `pending` is `true` for an invitation, `false` for a DM |
| `reason` | string | `"dm_opened"` for a DM; absent for an invitation |

alice's copy when bob accepts her DM request (her first message is already read for her):

```json
{
  "path": "chats/new",
  "reason": "dm_opened",
  "chat": {
    "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "is_dm": true,
    "name": "bob",
    "pending": false,
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b132-e3b5-7000-8839-be47fdf56d69",
    "last_message": {
      "id": "01a1b132-e3b5-7000-8839-be47fdf56d69",
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "text",
      "text": "hi bob, got a minute?",
      "time": "2026-11-06T14:11:04.757+00:00",
      "sender_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "sender": "alice"
    },
    "last_activity": "2026-11-06T14:11:04.757+00:00",
    "peer": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#FFFFFF",
      "background": "#1F6FEB",
      "online": true
    }
  }
}
```

### 4.5 `chats/group_created`: a new group

**When**: `POST chats`. **To**: everyone except the creating connection.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `group` | Group without `joined` | |

```json
{
  "path": "chats/group_created",
  "group": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "created_at": "2026-11-06T14:11:00.277+00:00",
    "created_by": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "background": "#F2F2F2"
    },
    "member_count": 1,
    "online_count": 1,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "background": "#F2F2F2",
        "role": "owner",
        "joined_at": "2026-11-06T14:11:00.277+00:00",
        "online": true
      }
    ],
    "members_truncated": false
  }
}
```

### 4.6 `chat/members_changed`: a chat's people changed

**When**: four cases, told apart by `change`.

| `change` | When | To |
| -------- | ---- | -- |
| `"joined"` | a join: `POST chat/{chat_id}/join`, or accepting an invite with `POST users/requests/{request_id}/accept` or `POST chat/{chat_id}/invite/accept` | everyone, the actor included |
| `"left"` | `POST chat/{chat_id}/leave`, when the group survives it | everyone, the actor included |
| `"profile"` | a member renamed themselves or changed their colours (`PATCH users/profile`): the member lists of every chat they are in now show something else | the members of that chat, except the acting connection |
| `"invite_cancelled"` | your pending invitation to this group is gone: the inviter cancelled it (`POST users/requests/{request_id}/cancel`), or you rejected it on another device | the invited person's connections, except the acting one |

**What the client does with it**: on `"invite_cancelled"`, remove that chat's `pending` row from
the chat list (the invitation no longer exists; opening it would answer `253 REQUEST_NOT_FOUND`).
On the other three, re-read the chat's members with `GET chat/{chat_id}/info` if that chat is
open, and `GET chats` if you want the new `member_count` in the list.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the group |
| `change` | string | `"joined"`, `"left"`, `"profile"` or `"invite_cancelled"` |
| `name` | string | its name (`"joined"` and `"left"` only) |
| `member_count` | integer | members after the change (`"joined"` and `"left"` only) |
| `user` | User ref | who joined or left (`"joined"` and `"left"` only) |

It is a small change notice, not the member list; fetch `GET chat/{chat_id}/info` for that.

```json
{
  "path": "chat/members_changed",
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "name": "Network Project",
  "member_count": 2,
  "change": "joined",
  "user": {
    "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
    "username": "bob",
    "text_color": "#FFFFFF",
    "background": "#1F6FEB"
  }
}
```

```json
{"path": "chat/members_changed", "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
 "change": "invite_cancelled"}
```

### 4.7 `chats/group_deleted`: a group is gone

**When**: its last member left; its messages and pending invites are deleted with it. **To**:
everyone, the actor included.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the deleted group |

```json
{"path": "chats/group_deleted", "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4"}
```

### 4.8 `users/profile_changed`: someone changed their name or colours

**When**: `PATCH users/profile` (a rename, a colour change, or both). **To**: everyone except the
acting connection.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user` | User without `created_at` | with the new username and colours |

Find the person by `user_id`, not by name: after a rename `username` is new. Update every place
that shows them (member lists, message senders, the name of your DM with them, the online list).

alice's copy when bob renames himself to Bobby and changes his colours (section 2.3):

```json
{
  "path": "users/profile_changed",
  "user": {
    "user_id": "2fb20bce-8f92-4892-9f7f-4349743f8b95",
    "username": "Bobby",
    "text_color": "#FFFFFF",
    "background": "#1F6FEB",
    "online": true
  }
}
```

### 4.9 `login/revoked`: this connection was logged out

**When**: the server logs a connection out by itself. A password change is NOT one of these
cases: it keeps the session token (section 2.11). The push comes first; then the connection
is no longer logged in, and its next authenticated request gets `251 INVALID_SESSION`. The socket
stays open.

**To**: the connection being logged out.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `reason` | string | one of the values below |

| `reason` | Cause |
| -------- | ----- |
| `logged_in_elsewhere` | the account logged in on another connection (`POST users/login`); an account has one session, so the old token stopped working |
| `logged_out` | `POST users/logout` on another connection that shared this token |
| `expired` | the session reached its `expires_at` (checked every 5 s, and on each request) |
| `too_many_connections` | rare: a login found `MAX_CONNECTIONS_PER_USER` connections of the account logged in on a session it was not replacing, and this was the oldest. That can only happen while the account's session changes at that same moment (another login or a logout). An ordinary login on a sixth connection does not cause it: it replaces the session, and the old connections get `logged_in_elsewhere`. |

Treat every reason as "logged out". After `logged_in_elsewhere`, do not log in again
automatically: two clients that do would keep logging each other out.

```json
{"path": "login/revoked", "reason": "logged_in_elsewhere"}
```

---

## 5. Frontend adapter

How the replies feed the data-in methods of `chat_page.py` (frontend commit `b43be55`), and what
the client has to do so that the server's view and the GUI agree.

### 5.1 API objects to `chat_page.py`

| `chat_page.py` method | Feed it from | Mapping |
| --------------------- | ------------ | ------- |
| `set_user_style(username, text_color, background)` | any User, User ref or Member: the login reply's `user`, `members`, message `sender`, `users/profile_changed`, `users/status` | `(u["username"], u["text_color"], u["background"])`. Call it before `set_members` / `set_messages`, since it redraws them. After a rename (`users/profile_changed`) the old name is gone: look the person up by `user_id`. |
| `set_profile(username)` | the login reply's `user` (or `GET users/info`) | `user["username"]` |
| `set_chats(chats, selected_id)` | `GET chats` → `chats` | each Chat item already has `chat_id`, `name`, `unread`, `is_dm` and `last_activity`; convert `last_activity` to a `datetime` (below). The GUI adds `[DM]` in front of DM names itself. |
| `set_members(members)` | `GET chat/{chat_id}/info` → `chat["members"]` | `{"username": m["username"], "online": m["online"], "is_me": m["user_id"] == my_user_id}`. Pending invites (`chat["invited"]`) have no place in the current GUI. |
| `set_messages(messages, first_unread_id)` | `GET chat/{chat_id}/messages` | `messages` mapped as below; `first_unread_id = reply["first_unread_id"]`. The NEW line is drawn only if that message is in the list. |
| `append_message(message)` | `chat/new_message` for the open chat; the `message` in your own `POST chat/{chat_id}/message` reply | mapped as below; add `"status": "Sent"` to your own |
| `set_summary(text)` | `GET chat/{chat_id}/summary` | `reply["summary"]`, or a placeholder such as "Nothing to summarize" when it is `null`; when `engine` is `"offline"` the text says it is not written by AI |
| `set_chat_title(title)` | Chat item / Chat info | `name` (selecting a chat in the list already sets it) |

Contract v2 spells the message fields the way the GUI wants them, so most of the old adapter
is gone: `id`, `text`, `time`, `sender`, `sender_id`, `background` need no renaming. What is
left is parsing the timestamps and marking your own messages.

```python
from datetime import datetime


def ts(s):
    """API timestamp -> local datetime, on Python 3.9 and later."""
    return datetime.fromisoformat(s).astimezone() if s else None


def chat_row(c):
    """GET chats item (or chats/new "chat") -> a set_chats() row."""
    return {"chat_id": c["chat_id"], "name": c["name"], "unread": c["unread"],
            "last_activity": ts(c["last_activity"]), "is_dm": c["is_dm"],
            "pending": c["pending"]}       # an invitation you have not answered yet


def message_row(m, my_user_id):
    """Message -> a set_messages() / append_message() row."""
    return {"id": m["id"],
            "sender": m["sender"] or "[System]",       # a system line has sender null
            "text": m["text"],
            "time": ts(m["time"]),
            "own": m["sender_id"] == my_user_id}
```

`"[System]"` cannot be anyone's username (brackets are not allowed in names). Keep every id as
the string the server sent; the demo data's integer ids are only for the preview.

The GUI's placeholder hooks map to these calls:

| Hook in `chat_page.py` | Call |
| ---------------------- | ---- |
| `edit_profile` / `request_edit_profile(username, text_color, background)` | `PATCH users/profile` with `{"username", "text_color", "background"}` (send only what changed; `background` is accepted too) |
| `change_password` / `request_change_password` | `PATCH users/password` with `{"new_password"}` — one request, no current password, and the session token stays as it is |
| `logout` | `POST users/logout`, then `RequestCord.clear_auth()` and the login page |
| `new_group` | `POST chats`; add the reply's `chat` to the list |
| `add_people_to_group` / `request_invitees(chat_id, query)` | `GET chat/{chat_id}/invitees` with `{"query"}`, then `POST chat/{chat_id}/invite` with the picked `user_id` |
| `request_user_groups(username)` ("See Groups") | `GET users/{user_id}/groups` with the member's `user_id` (from the member list); `groups` → `[{"chat_id", "name"}]` |
| `request_accept_invite(chat_id)` / `request_reject_invite(chat_id)` | `POST chat/{chat_id}/invite/accept` / `.../invite/reject` |
| `request_message(username, greeting)` | `POST users/{user_id}/dm` with `{"message": greeting}`; with the default settings it opens (or reuses) the DM at once |
| `send_message` | `POST chat/{chat_id}/message`; `clear_input()` and `append_message()` on 200 |
| `select_chat(chat_id)` | `GET chat/{chat_id}/info` and `GET chat/{chat_id}/messages`, then `POST chat/{chat_id}/read` |
| `generate_summary` | `GET` (or `POST`) `chat/{chat_id}/summary` |

### 5.2 Client duties

1. **Read markers.** Send `POST chat/{chat_id}/read` after showing a chat's messages, and again
   when an `chat/new_message` arrives for the chat on screen. The server marks as read only your own
   messages and the history from before you joined; without this call `unread` and the NEW line
   never go away.
2. **Logged out.** A `251` reply to a call sent with credentials (every call except `POST users`
   and `POST users/login`), or `login/revoked` with any reason, means this connection is
   no longer logged in: clear the credentials (`RequestCord.clear_auth()`) and show the login
   page. The socket stays open, so the user can log in again on it. A `251` from `POST users` or
   `POST users/login` only means that attempt failed: show the error and let the user try again.
3. **Never log in automatically after `logged_in_elsewhere`.** Tell the user the account was
   opened somewhere else and wait for them. Two clients that log in by themselves would keep
   logging each other out.
4. **Invitations.** A group invitation is a `pending` row of `GET chats` (section 2.5) and
   arrives as `chats/new`; answer it with `POST chat/{chat_id}/invite/accept` or
   `.../invite/reject`, and replace the row with the accept reply's `chat`. Remove the row on
   `chat/members_changed` with `change` `"invite_cancelled"` (the inviter took it back, or you
   rejected it elsewhere) and on `chats/group_deleted` for that `chat_id`. A client that prefers
   request objects can use `GET users/requests` and `users/request` instead; that is also where
   DM requests live, on a server run with `DM_REQUIRE_ACCEPT=true` (item 5).
5. **DMs open at once by default.** With the default `DM_REQUIRE_ACCEPT=false`, the first DM
   creates the room (`chats/new` reaches both people) and nothing waits for approval. A server
   run with `DM_REQUIRE_ACCEPT=true` turns the first DM into a request that the receiver must
   accept (item 4).
6. **A long timeout for the summary.** Wait up to 90 s for `GET chat/{chat_id}/summary`, or send
   it on a second connection that uses the same User ID and Session Token (never `users/login`
   there). The connection's other requests wait behind it.
7. **The password change keeps your session.** `PATCH users/password` answers
   `{"user_id", "changed": true}` and no token: keep sending the token you already have, on every
   connection. Nothing is logged out.
8. **Your own messages.** The connection that sent a text message gets no `chat/new_message` for it:
   append the `message` from the reply (match it by `rid` if the GUI shows "sending" first).
   Likewise, the connection that joins a group gets no `chat/new_message` for its own join line: the
   reply's `chat.last_message` is that line.
9. **Chat list updates.** `chat/new_message` for a chat that is not open: increase its `unread`
   (unless you sent it) and move it to the top with the new `last_activity`. A chat is added (or
   replaced, if it is listed already) by `chats/new`, which reaches every connection of both
   people, the acting one included, and by the replies of `POST chats`,
   `POST chat/{chat_id}/join`, `POST chat/{chat_id}/invite/accept`,
   `POST users/requests/{request_id}/accept` and `POST users/{user_id}/dm` (status `"sent"`). Your other connections learn about a group you
   created or joined on another connection from `chats/group_created` whose `created_by` is you
   and `chat/members_changed` `"joined"` whose `user` is you; neither carries a Chat item, so fetch
   the row with `GET chats`. The leave reply, `chat/members_changed` `"left"` whose `user` is you,
   and `chats/group_deleted` remove a chat. `GET chats` is always the truth if in doubt.
10. **Routing.** Route a reply on its `rid`, or on `path` and `method` (section 1.2); an error
    answers with the request URL, so `rid` is the only way to route one. A packet with no
    `method` is a push (section 4). Convert every timestamp with `ts()` before giving it to
    the GUI.
