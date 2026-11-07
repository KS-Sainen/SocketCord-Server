# SocketCord API reference

Every call the backend serves and every packet it pushes: request fields, reply fields, limits,
defaults, errors, pushes, and an example of each. It is written from the code
(`socketcord/handlers/`, `socketcord/views.py`, `socketcord/router.py`, `socketcord/app.py`,
`socketcord/presence.py`), and the examples are real replies captured from a running server
(long base64 values are shortened, and the PBKDF2 iteration count is shown at its default).
The order of keys inside a JSON object means nothing, and the examples do not always show the
server's order: the push examples list `path` first, while the server sends it last.

The frontend README lists 11 calls (section 2). The backend serves 17 more (section 3), and
answers the summary by POST as well as GET; the team rule says those must be documented in full,
and they are. The binary packet layout, the
challenge-response math and the connection limits are in [PROTOCOL.md](PROTOCOL.md); the
database is described in the [README](../README.md#database).

| Section | Contents |
| ------- | -------- |
| [1. Conventions](#1-conventions) | requests, reply head, authentication, ids and time, common errors, text rules, rate limits, object shapes |
| [2. The 11 calls of the frontend README](#2-the-11-calls-of-the-frontend-readme) | register, login, profile, info, chats, chat info, messages, send, DM, invite, password |
| [3. Additional calls](#3-additional-calls) | logout, online, search, requests, accept/decline/cancel, create group, all groups, read, join, leave, AI summary, a user's groups, invitees, accept/reject an invite by group |
| [4. Server pushes](#4-server-pushes) | `event/message`, `presence`, `request`, `chat`, `group_created`, `group_updated`, `group_deleted`, `profile`, `session_revoked` |
| [5. Frontend adapter](#5-frontend-adapter) | API objects to `chat_page.py`, and what the client must do |

All calls:

| Method | URL | Auth | What it does | Section |
| ------ | --- | ---- | ------------ | ------- |
| POST | `users` | none | register (two steps) | 2.1 |
| POST | `users/login` | none | log in (two steps) | 2.2 |
| PATCH | `users/profile` | yes | change your username and/or name colours | 2.3 |
| GET | `users/info` | yes | your account | 2.4 |
| GET | `chats` | yes | the chats you are in | 2.5 |
| GET | `chat/{chat_id}/info` | yes | a chat's details and members | 2.6 |
| GET | `chat/{chat_id}/messages` | member | message history, with filters | 2.7 |
| POST | `chat/{chat_id}/message` | member | send a message | 2.8 |
| POST | `users/{user_id}/dm` | yes | send a DM (the first one opens the room; a request only with `DM_REQUIRE_ACCEPT=true`) | 2.9 |
| POST | `chat/{chat_id}/invite` | member | invite someone to a group | 2.10 |
| PATCH | `users/profile/password` | yes | change your password (two steps) | 2.11 |
| POST | `users/logout` | yes | end your session | 3.1 |
| GET | `users/online` | yes | who is online | 3.2 |
| GET | `users/search` | yes | find users by name | 3.3 |
| GET | `users/requests` | yes | your pending DM requests and invites | 3.4 |
| POST | `users/requests/{request_id}/accept` | receiver | accept a DM request or invite | 3.5 |
| POST | `users/requests/{request_id}/decline` | receiver | decline one | 3.6 |
| POST | `users/requests/{request_id}/cancel` | sender | take one back | 3.7 |
| POST | `chats` | yes | create a group | 3.8 |
| GET | `chats/groups` | yes | every group on the server | 3.9 |
| POST | `chat/{chat_id}/read` | member | move your read marker | 3.10 |
| POST | `chat/{chat_id}/join` | yes | join a group by yourself | 3.11 |
| POST | `chat/{chat_id}/leave` | member | leave a group | 3.12 |
| GET | `chat/{chat_id}/summary` | member | AI summary of a chat | 3.13 |
| POST | `chat/{chat_id}/summary` | member | the same call by POST (the frontend README's method) | 3.13 |
| GET | `users/{user_id}/groups` | yes | the groups a user is in | 3.14 |
| GET | `chat/{chat_id}/invitees` | member | who you can invite to a group | 3.15 |
| POST | `chat/{chat_id}/invite/accept` | invitee | accept your invite to that group | 3.16 |
| POST | `chat/{chat_id}/invite/reject` | invitee | decline your invite to that group | 3.17 |

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
  the same thing: "not given". The exception is `password`, `old_password` and `new_password` in
  the register, login and password-change calls: if the key is there at all, even as `null`, the
  request is refused with `250 PLAINTEXT_PASSWORD`.

### 1.2 Replies

A reply is one ReplyCord packet: a status code and a JSON object. Its first fields are the
**reply head**:

| Field | Type | Present | Meaning |
| ----- | ---- | ------- | ------- |
| `path` | string | always | The request URL without leading/trailing `/`, first 256 characters. |
| `method` | string | always (not in pushes) | `"POST"`, `"GET"` or `"PATCH"`, also when the packet itself was broken (a checksum or UTF-8 error). For an unknown method code, the code in decimal, such as `"12345"`. |
| `rid` | integer or string | when the request had a valid one | The request's own `rid`. |
| `error` | string | status ≠ 200 | Machine-readable code (sections 1.5 and [PROTOCOL.md](PROTOCOL.md) section 3). |
| `detail` | string | status ≠ 200 | One sentence for people; safe to show in the GUI. |
| `field` | string | some 250 and 252 errors | The request field at fault. |
| `retry_after` | integer | rate-limit, quota and some AI errors | Seconds to wait before trying again (at least 1). |

The call's own fields follow the head. Every field a call lists is always present unless the
call says otherwise; "none" is sent as `null`, not left out.

Requests from one connection are answered one at a time, in the order they were sent. Server
pushes (section 4) can arrive between replies: they have `path` = `event/<name>` and no `method`
or `rid`. Route a packet by `path` and `method` (`GET chats` and `POST chats` share the path
`chats`), or by `rid`.

Error replies look like this:

```json
{
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/messages",
  "method": "GET",
  "rid": 7,
  "error": "NOT_A_MEMBER",
  "detail": "join this group first"
}
```

```json
{
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/message",
  "method": "POST",
  "rid": 14,
  "error": "INVALID_FIELD",
  "detail": "'content' cannot be blank",
  "field": "content"
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
  `POST users/login`, `251` only reports that this attempt failed (wrong password, expired
  challenge, a login that lost a race with another login of the account): show the error and let
  the user try again. The password change answers its own failures (wrong current password,
  expired challenge) with `252`, because the session is still fine.
* The URL is matched before the credentials are checked: an unknown call answers
  `253 NOT_FOUND` "no such call (...)" with or without credentials.
* The first authenticated request on a connection logs that connection in. A client that
  reconnects with a saved token is therefore online again, and if the account was offline,
  everyone else gets `event/presence`.
* **One session per account.** A login ends the previous session everywhere
  (`event/session_revoked`, reason `logged_in_elsewhere`), and a password change replaces the
  token (the new one is in its reply). Several connections may share the one token: send the same
  User ID and Session Token on each of them, and never call `users/login` on the extra ones.
  Details: [PROTOCOL.md](PROTOCOL.md) section 4, "Sessions".

### 1.4 Ids and timestamps

* `user_id`, `chat_id` and `request_id` are UUID strings. Replies always write UUIDs in lower
  case with hyphens. Fields that take an id accept any form `uuid.UUID` reads.
* A DM request's `request_id` is the DM's `chat_id`. An invite's `request_id` is its membership
  id, which is not a chat id.
* `message_id` is a **UUIDv7** made by the server. Its first 48 bits are the Unix time in
  milliseconds, followed by a counter, so a later message always has a larger id, and within one
  chat ids become visible in that order (paging with `after_id` never skips a message). Since the
  server sends every id in lower case with the hyphens in the same places, comparing two message
  ids as strings gives the same order as comparing their times: `a < b` means `a` is older.
  `uuid.UUID(a) < uuid.UUID(b)` gives the same answer.
* Read markers (`last_read_message_id`) are message ids, or `null` when nothing has been read.
* **Timestamps in replies** are ISO 8601 in UTC with milliseconds and a `Z`, such as
  `"2026-11-06T14:11:01.092Z"`. A message's `created_at` is the time stored inside its id.
  * Python 3.11 and later: `datetime.fromisoformat(s)`.
  * Python 3.9 and 3.10, whose `fromisoformat` does not accept `Z`:
    `datetime.fromisoformat(s.replace("Z", "+00:00"))`.
  * Both give an aware UTC `datetime`; `.astimezone()` converts it to local time for display.
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
  before it is stored. The one exception is `content` in `POST chat/{chat_id}/message`, which is
  stored exactly as sent. Lengths count characters, not bytes.
* **Control characters** (Unicode category Cc) are refused with `250 INVALID_FIELD`. Message
  content, DM text and group descriptions may contain newlines (`\n`, `\r`) and tabs (`\t`);
  names and every other text field may not.
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

* **Challenges per address**: register and password-change challenges cost the server a
  Diffie-Hellman step, so each client address may ask for `CHALLENGE_RATE_PER_MINUTE` (30 a
  minute, burst `CHALLENGE_BURST` 15; 120 and 40 in `docker-compose.yml`). Over it:
  `252 TOO_MANY_ATTEMPTS`.
* **Failed passwords**: 5 failed answers per (username, address) within 5 minutes, failed logins
  and failed password changes counted together; 50 per address for usernames that do not exist.
  Over it: `252 TOO_MANY_ATTEMPTS`. An IPv6 address counts as its whole /64. A correct login or
  password change clears the failures counted for that username from that address.
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
| `username` | string | 3–32 characters from `A–Z a–z 0–9 _ . -`; unique without regard to case. It can change (`PATCH users/profile`, `event/profile`): identify people by `user_id`. |
| `text_color` | string | `"#rrggbb"` in lower case; default `"#171717"` |
| `text_bg_color` | string | `"#rrggbb"` in lower case; default `"#f2f2f2"`; never equal to `text_color` |
| `online` | boolean | the account has a logged-in connection (or lost its last one less than 2 s ago) |
| `created_at` | timestamp | when the account was made |

The entries of `GET users/online` and `GET users/search` are a User without `created_at`, plus
`is_self` (boolean: this is you). `event/presence` and `event/profile` carry a User without
`created_at`.

**User ref**: a user inside another object (`sender`, `from`, `to`, `created_by`, `invited_by`,
the `user` of `event/group_updated`): `user_id`, `username`, `text_color`, `text_bg_color`. A
DM's `peer` and an invite's `user` add `online` (boolean).

**Member**: an entry of `members`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user_id`, `username`, `text_color`, `text_bg_color` | | as in User |
| `role` | string | `"owner"` for the group's creator (again after leaving and joining back), `"member"` for everyone else and for both people of a DM. The role gives no extra rights. |
| `joined_at` | timestamp | when they joined the group; for a DM, when it was accepted |
| `online` | boolean | |

Members are listed in `joined_at` order, then by `user_id`.

**Message**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `message_id` | string | UUIDv7 (section 1.4) |
| `chat_id` | string | |
| `kind` | string | `"text"` (written by a user) or `"system"` (`"bob joined the group"`, `"bob left the group"`) |
| `content` | string | 1–4000 characters. In `last_message` of a chat item it is cut to 120 characters (119 and `"…"`). |
| `created_at` | timestamp | the time inside `message_id` |
| `sender` | User ref or null | `null` for system lines; names and colours are the sender's current ones |

**Chat item**: an entry of `GET chats`. Every reply and push that adds a chat to your list
carries it in this shape: `chat` in `POST chats`, `POST chat/{chat_id}/join`,
`POST users/requests/{request_id}/accept`, `POST users/{user_id}/dm`, and `event/chat`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | |
| `kind` | string | `"group"` or `"dm"` |
| `is_dm` | boolean | `kind == "dm"` |
| `name` | string | the group's name; for a DM, the other person's username |
| `description` | string | the group's description; `""` for a DM |
| `role` | string | `"owner"` or `"member"` (always `"member"` in a DM) |
| `member_count` | integer | members (2 for a DM) |
| `unread` | integer | messages after your read marker that someone else sent (system lines count) |
| `last_read_message_id` | string or null | your read marker |
| `last_message` | Message or null | the newest message, content cut to 120 characters |
| `last_activity` | timestamp | `created_at` of the newest message, or when the chat was made if it has none |
| `peer` | User ref + `online` | DMs only: the other person |

**Chat info**: `chat` of `GET chat/{chat_id}/info`, and `info` in the join and invite-accept
replies.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id`, `kind`, `is_dm`, `description` | | as in Chat item |
| `name` | string | the group's name; for a DM you are in, the other person's username |
| `created_by` | User ref or null | the group's creator (for a DM, who sent the first DM). While the creator is not a member, its `username`, `text_color` and `text_bg_color` are `null`. |
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
`event/group_created` (there without `joined`).

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
`"declined"` and `"cancelled"` appear only in the reply to that answer and in `event/request`.
Afterwards its `request_id` answers `253 REQUEST_NOT_FOUND`. An accepted request stays readable
with `"accepted"`; answering it again gives `252 REQUEST_CLOSED`.

---

## 2. The 11 calls of the frontend README

Each call starts with what changed compared with the README's one-line description.

### 2.1 `POST users`: register

Auth: none. Two requests on the same connection.

**Changed**: the README sends `username` and `password`. The backend refuses any message that
contains `password`, `old_password` or `new_password` (`250 PLAINTEXT_PASSWORD`) and uses a
challenge-response instead: the password never crosses the socket, and the password verifier
travels encrypted under a fresh Diffie-Hellman key. `RegisterFlow` in `socketcord/scram.py`
(standard library only) builds both messages; [PROTOCOL.md](PROTOCOL.md) section 4 has the
math. A new account has the default colours and no session: log in afterwards.

**Step 1 request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `username` | string | yes | 3–32 characters from `A–Z a–z 0–9 _ . -` after trimming; unique without regard to case |
| `client_nonce` | string | yes | base64 text, 16–128 characters (`RegisterFlow` sends 18 random bytes, 24 characters) |
| `device_id` | string | no | 8–128 characters from `A–Z a–z 0–9 _ . : -`. `RegisterFlow` sends `scram.device_fingerprint()`: the first 32 hex characters of SHA-256 over `"socketcord-device:"` followed by the MAC address (`uuid.getnode()`) as 12 lower-case hex digits. It is for the one-account-per-device-per-day rule ([PROTOCOL.md](PROTOCOL.md) section 5). |

**Step 1 reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `step` | string | `"challenge"` |
| `challenge_id` | string | UUID. Answer it once, on this connection, within `expires_in` seconds. |
| `salt` | string | base64 of 16 bytes, chosen by the server |
| `iterations` | integer | PBKDF2 rounds (`SCRAM_ITERATIONS`, default 600000) |
| `server_nonce` | string | base64 of 18 bytes |
| `expires_in` | integer | seconds (`CHALLENGE_TTL_SECONDS`, default 60) |
| `dh_group` | string | `"modp2048"` (RFC 3526 group 14) |
| `dh_public` | string | base64 of 256 bytes: the server's Diffie-Hellman public value |

**Step 2 request** (a message with `challenge_id` is step 2)

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `challenge_id` | string | yes | from step 1, 1–64 characters |
| `dh_public` | string | yes | base64 of 256 bytes; the client's public value, in 2 .. p−2 |
| `wrapped_verifier` | string | yes | base64 of 64 bytes: (StoredKey ‖ ServerKey) XOR pad |
| `wrap_tag` | string | yes | base64 of 32 bytes |

The AuthMessage that `wrap_tag` and `server_signature` cover holds the username trimmed and in
lower case (`scram.canonical_username`), whatever letter case was typed; `RegisterFlow` builds it
that way ([PROTOCOL.md](PROTOCOL.md) section 4). The account itself keeps the case as typed.

**Step 2 reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `step` | string | `"done"` |
| `user_id` | string | the new account |
| `user` | User | the new account (`online` is `false`) |
| `server_signature` | string | base64 of 32 bytes; `RegisterFlow.verify(reply)` checks it |

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `PLAINTEXT_PASSWORD` | the message has `password`, `old_password` or `new_password` (`field` names it) |
| 252 | `TOO_MANY_ATTEMPTS` | step 1: this address asked for too many challenges (section 1.7); checked before the request's fields (only the plaintext-password check comes first); `retry_after` |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | a field is missing or malformed |
| 250 | `INVALID_USERNAME` | step 1: the username breaks the rules (`field`: `"username"`) |
| 252 | `USERNAME_TAKEN` | step 1, or step 2 if someone took the name in between (`field`: `"username"`) |
| 252 | `RATE_LIMITED` | step 1 or 2: this device id or address already made `REG_LIMIT_DEVICE_MAX` / `REG_LIMIT_IP_MAX` accounts within `REG_LIMIT_WINDOW_HOURS`; `retry_after` is when the oldest one stops counting |
| 250 | `CONNECTION_CLOSED` | step 1: the connection closed while the challenge was being made, so none was kept; nobody reads this reply |
| 251 | `CHALLENGE_INVALID` | step 2: the challenge is unknown, used, expired, or was issued on another connection |
| 251 | `INVALID_PROOF` | step 2: `wrap_tag` does not match (wrong DH value, or the data was changed on the way) |
| | common errors | section 1.5 (the authentication errors do not apply) |

Step 2 uses up the challenge before it checks anything else, so after any step-2 error start
again with a new `RegisterFlow`.

**Pushes**: none.

**Example**. Step 1, `POST users` without credentials:

```json
{
  "rid": 1,
  "username": "alice",
  "client_nonce": "4ogG4aPY/WxBB/JbBlHvBaLu",
  "device_id": "device-alice-01"
}
```

```json
{
  "path": "users",
  "method": "POST",
  "rid": 1,
  "step": "challenge",
  "challenge_id": "2fe56260-20e5-4c35-a864-5d49bd29535e",
  "salt": "7HEMsKi1LzC5Q/V2mdEKpA==",
  "iterations": 600000,
  "server_nonce": "ZjLBC/HNfR4cAy8FHy4hkdTv",
  "expires_in": 60,
  "dh_group": "modp2048",
  "dh_public": "2/bwyxKLzRVkYKhr...5A6KTA=="
}
```

Step 2:

```json
{
  "rid": 2,
  "challenge_id": "2fe56260-20e5-4c35-a864-5d49bd29535e",
  "dh_public": "6K9/X/p/6mL1jkRe...XP9T3A==",
  "wrapped_verifier": "1mnevW0tqhn9wcmy...FSm3Hg==",
  "wrap_tag": "3fx0rUJd4OF5JRA/na/UWOQMDST1oJp728/z9Ye4hHs="
}
```

```json
{
  "path": "users",
  "method": "POST",
  "rid": 2,
  "step": "done",
  "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
  "user": {
    "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
    "username": "alice",
    "text_color": "#171717",
    "text_bg_color": "#f2f2f2",
    "online": false,
    "created_at": "2026-11-06T14:10:59.433Z"
  },
  "server_signature": "ToXGgX621PDievIuuYE1ajIdatD3R9t1eBYyFbp6hgs="
}
```

### 2.2 `POST users/login`: log in

Auth: none. Two requests on the same connection.

**Changed**: challenge-response instead of `username` and `password` (`LoginFlow` in
`socketcord/scram.py`). The final reply carries the `user_id` and the new `session_token` that
every later request puts in its header, plus `expires_at` and the `user`. An account has one
session: this login ends the previous one on every other connection.

**Step 1 request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `username` | string | yes | as in register; letter case does not matter |
| `client_nonce` | string | yes | base64 text, 16–128 characters |

**Step 1 reply**: `step` (`"challenge"`), `challenge_id`, `salt` (the account's), `iterations`
(the account's), `server_nonce`, `expires_in`, as in register, without the DH fields. A username
that does not exist gets a challenge too, with a stable made-up salt, so this reply does not say
whether the name exists.

**Step 2 request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `challenge_id` | string | yes | from step 1 |
| `client_proof` | string | yes | base64 of 32 bytes: ClientKey XOR HMAC(StoredKey, AuthMessage) |

The AuthMessage holds the username trimmed and in lower case (`scram.canonical_username`),
whatever letter case the user typed; `LoginFlow` builds it that way ([PROTOCOL.md](PROTOCOL.md)
section 4). A proof made over a name with capital letters, as typed, fails with
`251 INVALID_CREDENTIALS`.

**Step 2 reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `step` | string | `"done"` |
| `user_id` | string | put it in the User ID field of every later request |
| `session_token` | string | UUID; put it in the Session Token field of every later request |
| `expires_at` | timestamp | when the session ends (`SESSION_TTL_HOURS`, default 168 hours, after this login) |
| `user` | User | your account (`online` is `true`) |
| `server_signature` | string | base64 of 32 bytes; `LoginFlow.verify(reply)` checks it. If it is wrong, the server does not know this account's verifier: treat it as a fake server. |

**Errors**

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `PLAINTEXT_PASSWORD` | as in register |
| 250 | `MISSING_FIELD`, `INVALID_FIELD`, `INVALID_USERNAME` | a field is missing or malformed |
| 252 | `TOO_MANY_ATTEMPTS` | step 1 or 2: too many failed passwords for this username from this address, or for unknown usernames from this address (section 1.7); `retry_after` |
| 251 | `CHALLENGE_INVALID` | step 2: the challenge is unknown, used, expired or from another connection; or the password was changed between step 1 and step 2 |
| 251 | `INVALID_CREDENTIALS` | step 2: wrong password, or a username that does not exist (the same answer) |
| 251 | `INVALID_SESSION` | step 2: another login of the same account replaced this new session at the same moment |
| 250 | `CONNECTION_CLOSED` | step 1: the connection closed while the challenge was being made, so none was kept; nobody reads this reply |
| | common errors | section 1.5 |

**Pushes** (step 2):

* `event/session_revoked` with reason `logged_in_elsewhere` to every connection that still uses
  the account's previous token; they are logged out. This happens even when this login then
  answers `251 INVALID_SESSION`. A login is never refused for having too many connections: on a
  sixth connection it replaces the session like any login, so the five old connections get
  `logged_in_elsewhere`.
* `event/presence` (online) to everyone else, if the account was offline.
* If this connection was logged in as another account, that account is logged out here first
  (`event/presence` offline at once, if it has no other connection).

**Example**. Step 1:

```json
{"rid": 3, "username": "alice", "client_nonce": "sgUXTApl+1Xcd664E6hlnwna"}
```

```json
{
  "path": "users/login",
  "method": "POST",
  "rid": 3,
  "step": "challenge",
  "challenge_id": "e5332ff9-8c33-410a-a880-43e3c9d5d6f8",
  "salt": "7HEMsKi1LzC5Q/V2mdEKpA==",
  "iterations": 600000,
  "server_nonce": "X2QLufWJ03JMGcQkrdgaQaJ3",
  "expires_in": 60
}
```

Step 2:

```json
{
  "rid": 4,
  "challenge_id": "e5332ff9-8c33-410a-a880-43e3c9d5d6f8",
  "client_proof": "nD4csJpkJXI63dfI/MOYphmUTGshmaLBJWuhszkMGwQ="
}
```

```json
{
  "path": "users/login",
  "method": "POST",
  "rid": 4,
  "step": "done",
  "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
  "session_token": "88cd7e6a-eda2-488f-b247-7b933fda3de5",
  "expires_at": "2026-11-13T14:10:59.434Z",
  "user": {
    "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
    "username": "alice",
    "text_color": "#171717",
    "text_bg_color": "#f2f2f2",
    "online": true,
    "created_at": "2026-11-06T14:10:59.433Z"
  },
  "server_signature": "MKPi9/Mt7vE83Lmz8ulETBqpkb3BoRz/85WtJtG+Fn4="
}
```

### 2.3 `PATCH users/profile`: change your username and name colours

Auth: yes.

**Changed**: the profile is the username and the two colours of the team's data model (User Text
Color, User Text Background Color); there is no display name and no bio. The GUI draws every
username in its owner's text colour on its owner's background colour. `background` is accepted as
another name for `text_bg_color` (the frontend README's word).

**Request** (at least one of `username`, `text_color`, `text_bg_color`, `background`)

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `username` | string | no | the new name: trimmed, then 3–32 characters from `A–Z a–z 0–9 _ . -`, the same rule as `POST users`. Unique without regard to case: a name someone else has (in any case) is refused; your own name in another case (`bob` → `Bob`) is fine, and so is your current name. |
| `text_color` | string | no | `"#rrggbb"`, either letter case; stored in lower case |
| `text_bg_color` | string | no | the same |
| `background` | string | no | the same as `text_bg_color`. Sending both is allowed only with the same colour (letter case aside). |

After the change the two colours must differ. Everything sent is changed in one step: if
anything is refused, nothing changes.

**Effect of a rename**: the account, its id, its password, its session and every chat stay as they
were; everything that shows the user (members, message senders, DM names, requests, the online
list) shows the new name from now on. Stored system lines such as `"bob joined the group"` keep
the old text. Log in with the new name afterwards (any letter case); the old name stops working at
once, also for a login challenge fetched just before the rename, and someone else may register it.
The password keeps working, because the challenge-response verifier does not depend on the name
([PROTOCOL.md](PROTOCOL.md) section 4).

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
| 250 | `INVALID_FIELD` | `text_bg_color` and `background` are both sent with different colours (`field`: `"background"`) |
| 250 | `MISSING_FIELD` | none of the four fields was sent (`field`: `"text_color"`) |
| 250 | `INVALID_FIELD` | the text colour would equal the background colour (`field`: `"text_bg_color"` or `"background"`, whichever was sent; else `"text_color"`) |
| 252 | `USERNAME_TAKEN` | another account has that name, in any letter case (`field`: `"username"`; the same error `POST users` gives) |
| 252 | `RATE_LIMITED` | more than `PROFILE_RATE_PER_MINUTE` changes (section 1.7) |
| 253 | `USER_NOT_FOUND` | the account no longer exists |
| | common errors | section 1.5 |

**Pushes**: `event/profile` (section 4.8) to every logged-in connection except this one (your
other connections included), for a rename as for a colour change. `GET users/online` and later
`event/presence` show the new name.

**Example**: bob renames himself and changes his colours at once:

```json
{"rid": 5, "username": "Bobby", "text_color": "#FFFFFF", "background": "#1F6FEB"}
```

```json
{
  "path": "users/profile",
  "method": "PATCH",
  "rid": 5,
  "user": {
    "user_id": "2fb20bce-8f92-4892-9f7f-4349743f8b95",
    "username": "Bobby",
    "text_color": "#ffffff",
    "text_bg_color": "#1f6feb",
    "online": true,
    "created_at": "2026-11-07T07:22:23.959Z"
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

Auth: yes. **Changed**: `user` has `text_color` and `text_bg_color` instead of a display name
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
  "path": "users/info",
  "method": "GET",
  "rid": 5,
  "user": {
    "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
    "username": "alice",
    "text_color": "#171717",
    "text_bg_color": "#f2f2f2",
    "online": true,
    "created_at": "2026-11-06T14:10:59.433Z"
  }
}
```

### 2.5 `GET chats`: the chats you are in

Auth: yes.

**Changed**: each entry is a Chat item (section 1.8) with the fields `chat_page.set_chats()`
expects (`chat_id`, `name`, `unread`, `last_activity`, `is_dm`), and the list is paged.

**Request**

| Field | Type | Default | Rules |
| ----- | ---- | ------- | ----- |
| `count` | integer | 200 | 1–200 |
| `offset` | integer | 0 | 0–10,000,000 |

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chats` | list of Chat item | the groups you joined and your accepted DMs, latest activity first (on a tie, the newer last message first, then by `chat_id`) |
| `offset` | integer | as requested |
| `has_more` | boolean | more chats follow this page |

Pending DM requests and invites are not chats yet: see `GET users/requests`.

**Errors**: `250 INVALID_FIELD` (`count`, `offset`); common errors. **Pushes**: none.

**Example**: alice has a DM with bob and a group.

```json
{
  "path": "chats",
  "method": "GET",
  "rid": 38,
  "chats": [
    {
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "dm",
      "is_dm": true,
      "name": "bob",
      "description": "",
      "role": "member",
      "member_count": 2,
      "unread": 0,
      "last_read_message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
      "last_message": {
        "message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
        "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
        "kind": "text",
        "content": "thanks!",
        "created_at": "2026-11-06T14:11:05.567Z",
        "sender": {
          "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
          "username": "alice",
          "text_color": "#171717",
          "text_bg_color": "#f2f2f2"
        }
      },
      "last_activity": "2026-11-06T14:11:05.567Z",
      "peer": {
        "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
        "username": "bob",
        "text_color": "#ffffff",
        "text_bg_color": "#1f6feb",
        "online": true
      }
    },
    {
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "group",
      "is_dm": false,
      "name": "Network Project",
      "description": "term project",
      "role": "owner",
      "member_count": 3,
      "unread": 1,
      "last_read_message_id": "01a1b132-d6fa-7000-8c92-ebb7ec7525b9",
      "last_message": {
        "message_id": "01a1b132-da2d-7000-9580-a1c0b24aabab",
        "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
        "kind": "system",
        "content": "carol joined the group",
        "created_at": "2026-11-06T14:11:02.317Z",
        "sender": null
      },
      "last_activity": "2026-11-06T14:11:02.317Z"
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
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/info",
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
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-06T14:11:00.277Z",
    "member_count": 2,
    "online_count": 2,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2",
        "role": "owner",
        "joined_at": "2026-11-06T14:11:00.277Z",
        "online": true
      },
      {
        "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
        "username": "bob",
        "text_color": "#ffffff",
        "text_bg_color": "#1f6feb",
        "role": "member",
        "joined_at": "2026-11-06T14:11:00.684Z",
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
          "text_bg_color": "#f2f2f2",
          "online": true
        },
        "invited_by": {
          "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
          "username": "alice",
          "text_color": "#171717",
          "text_bg_color": "#f2f2f2"
        },
        "invited_at": "2026-11-06T14:11:01.910Z"
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
  "path": "chat/99b362bb-971b-4f39-a15a-b95612bcd979/info",
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
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-06T14:11:04.757Z",
    "member_count": 2,
    "online_count": 2,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2",
        "role": "member",
        "joined_at": "2026-11-06T14:11:05.164Z",
        "online": true
      },
      {
        "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
        "username": "bob",
        "text_color": "#ffffff",
        "text_bg_color": "#1f6feb",
        "role": "member",
        "joined_at": "2026-11-06T14:11:05.164Z",
        "online": true
      }
    ],
    "members_truncated": false,
    "joined": true,
    "last_read_message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
    "peer": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb",
      "online": true
    }
  }
}
```

### 2.7 `GET chat/{chat_id}/messages`: message history

Auth: members only.

**Changed**: the README's filters `count`, `content` and `time` work as described below, plus
`before_id` and `after_id` (UUID message ids). The reply adds your read marker and
`first_unread_message_id`, which tells the GUI where to draw its NEW line.

**Request** (every field optional; all given filters apply together)

| Field | Type | Default | Rules |
| ----- | ---- | ------- | ----- |
| `count` | integer | 50 | 1–200 |
| `content` | string | | 1–50 characters after trimming; messages whose content contains it, ignoring case; `%` and `_` match themselves |
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
| `first_unread_message_id` | string or null | the oldest message of the whole chat after your read marker (filters do not change it), or `null` if there is none. Draw NEW above it when it is in `messages`. |

Reading messages does not mark them as read; send `POST chat/{chat_id}/read`.

`first_unread_message_id` looks at every message after your marker, and `unread` in `GET chats`
only at those someone else sent. The two still agree, because your own messages are never after
your marker: sending one moves the marker to it, and the first message of a DM request counts as
read for the person who sent it.

**Errors**, checked in this order:

| Status | Code | When |
| ------ | ---- | ---- |
| 253 | `CHAT_NOT_FOUND` | no such chat, or a DM you are not in |
| 252 | `NOT_A_MEMBER` | a group you have not joined |
| 250 | `INVALID_FIELD` | `count`, `before_id`, `after_id` or `content` is malformed; or `time` is not a timestamp, has a key other than `before`/`after`, or is out of range (`field` is `"time"`, `"time.before"` or `"time.after"`) |
| | common errors | section 1.5 |

**Pushes**: none.

**Example**. Request `GET chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/messages` with an empty
message (bob joined, then alice wrote twice):

```json
{
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/messages",
  "method": "GET",
  "rid": 13,
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "messages": [
    {
      "message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "system",
      "content": "bob joined the group",
      "created_at": "2026-11-06T14:11:00.684Z",
      "sender": null
    },
    {
      "message_id": "01a1b132-d564-7000-8685-b94d127a7451",
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "text",
      "content": "hello group",
      "created_at": "2026-11-06T14:11:01.092Z",
      "sender": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
    },
    {
      "message_id": "01a1b132-d6fa-7000-8c92-ebb7ec7525b9",
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "text",
      "content": "  second  ",
      "created_at": "2026-11-06T14:11:01.498Z",
      "sender": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
    }
  ],
  "has_more": false,
  "last_read_message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
  "first_unread_message_id": "01a1b132-d564-7000-8685-b94d127a7451"
}
```

Filter examples: `{"count": 20, "before_id": "01a1b132-d564-7000-8685-b94d127a7451"}` (older
page), `{"after_id": "01a1b132-d564-7000-8685-b94d127a7451"}` (newer messages),
`{"content": "deadline", "time": {"after": "2026-11-01T00:00:00+07:00"}}`.

### 2.8 `POST chat/{chat_id}/message`: send a message

Auth: members only.

**Changed**: the field is `content`, and the reply carries the stored message. Every online
member gets it at once as `event/message`.

**Request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `content` | string | yes | 1–4000 characters and not only whitespace; stored exactly as sent (not trimmed); newlines and tabs allowed |

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
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | `content` is missing, not a string, longer than 4000 characters, blank ("'content' cannot be blank") or has a control character |
| 252 | `RATE_LIMITED` | more than `MESSAGE_RATE_PER_MINUTE` (shared with DMs) |
| 253 | `NOT_FOUND` | the chat was deleted while sending |
| | common errors | section 1.5 |

**Pushes**: `event/message` to every logged-in connection of the chat's members, except the
connection that sent it (your other connections get it).

**Example**

```json
{"rid": 12, "content": "hello group"}
```

```json
{
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/message",
  "method": "POST",
  "rid": 12,
  "message": {
    "message_id": "01a1b132-d564-7000-8685-b94d127a7451",
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "text",
    "content": "hello group",
    "created_at": "2026-11-06T14:11:01.092Z",
    "sender": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    }
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
| `message` | string | yes | 1–4000 characters after trimming (unlike `POST chat/{chat_id}/message`, leading and trailing whitespace is removed); newlines and tabs allowed |

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
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | `message` is missing, not a string, empty after trimming, over 4000 characters, or has a control character |
| 250 | `INVALID_TARGET` | `{user_id}` is you |
| 253 | `USER_NOT_FOUND` | no such user |
| 252 | `RATE_LIMITED` | over `MESSAGE_RATE_PER_MINUTE` (every DM counts); when a request would be created (`DM_REQUIRE_ACCEPT=true`), also over `REQUEST_RATE_PER_HOUR` |
| 252 | `ALREADY_REQUESTED` | `DM_REQUIRE_ACCEPT=true` only: your earlier DM request to them is still pending |
| 252 | `LIMIT_REACHED` | `DM_REQUIRE_ACCEPT=true` only: creating a request, but you already have `MAX_PENDING_REQUESTS` (100) pending requests sent, or they have 100 received |
| 252 | `TRY_AGAIN` | the pair's DM changed three times while the call ran (the other person was accepting, cancelling or writing at that moment); send it again |
| 253 | `NOT_FOUND` | the room or the other user disappeared while sending |
| | common errors | section 1.5 |

**Pushes**:

* Into an open room: `event/message` to the other person (and to your other connections).
* New room (the default): `event/chat` (`dm_opened`) to every connection of both of you, this one
  included (before the reply), then `event/message` with your message to both of you except this
  connection. Nothing older is replayed: the room starts with your message. These are the pushes
  the receiver needs to add the DM to their chat list and show the message.
* A pending request opens (theirs, or a left-over one of yours): `event/chat` (`dm_opened`), then
  `event/message` with the request's first message, both to every connection of both of you, this
  one included (here they arrive before the reply); then `event/message` with yours to both of you
  except this connection; then `event/request` (`accepted`) to them.
* New request (`DM_REQUIRE_ACCEPT=true`): `event/request` (`pending`) to them, only while the
  request is still pending (section 4.3).

**Example**. By default the first DM from alice to carol opens the room at once:

```json
{"rid": 7, "message": "hi carol"}
```

```json
{
  "path": "users/50245c20-966f-4dc8-a71e-dfe2de0d9802/dm",
  "method": "POST",
  "rid": 7,
  "status": "sent",
  "chat_id": "01ffdaf5-1ce5-4fdc-84be-3bb0e251b30b",
  "chat": {
    "chat_id": "01ffdaf5-1ce5-4fdc-84be-3bb0e251b30b",
    "kind": "dm",
    "is_dm": true,
    "name": "carol",
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b4e3-17d2-7000-bddb-d8069783aa66",
    "last_message": {
      "message_id": "01a1b4e3-17d2-7000-bddb-d8069783aa66",
      "chat_id": "01ffdaf5-1ce5-4fdc-84be-3bb0e251b30b",
      "kind": "text",
      "content": "hi carol",
      "created_at": "2026-11-07T07:22:24.082Z",
      "sender": {
        "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
    },
    "last_activity": "2026-11-07T07:22:24.082Z",
    "peer": {
      "user_id": "50245c20-966f-4dc8-a71e-dfe2de0d9802",
      "username": "carol",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2",
      "online": true
    }
  },
  "messages": [
    {
      "message_id": "01a1b4e3-17d2-7000-bddb-d8069783aa66",
      "chat_id": "01ffdaf5-1ce5-4fdc-84be-3bb0e251b30b",
      "kind": "text",
      "content": "hi carol",
      "created_at": "2026-11-07T07:22:24.082Z",
      "sender": {
        "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
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
  "path": "users/4acc2abf-57e9-426d-a52c-90d65a77743b/dm",
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
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb"
    },
    "created_at": "2026-11-06T14:11:04.757Z",
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
  "path": "users/4acc2abf-57e9-426d-a52c-90d65a77743b/dm",
  "method": "POST",
  "rid": 32,
  "status": "sent",
  "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
  "chat": {
    "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "is_dm": true,
    "name": "bob",
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
    "last_message": {
      "message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "text",
      "content": "thanks!",
      "created_at": "2026-11-06T14:11:05.567Z",
      "sender": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
    },
    "last_activity": "2026-11-06T14:11:05.567Z",
    "peer": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb",
      "online": true
    }
  },
  "messages": [
    {
      "message_id": "01a1b132-e6df-7000-a64f-067660850d0b",
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "text",
      "content": "thanks!",
      "created_at": "2026-11-06T14:11:05.567Z",
      "sender": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
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
  "path": "users/007204d5-49fb-41cc-b93c-593d8380c338/dm",
  "method": "POST",
  "rid": 39,
  "status": "sent",
  "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
  "chat": {
    "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
    "kind": "dm",
    "is_dm": true,
    "name": "carol",
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b132-ea0e-7000-b9bd-9c36ac677b1e",
    "last_message": {
      "message_id": "01a1b132-ea0e-7000-b9bd-9c36ac677b1e",
      "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
      "kind": "text",
      "content": "hey carol!",
      "created_at": "2026-11-06T14:11:06.382Z",
      "sender": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
    },
    "last_activity": "2026-11-06T14:11:06.382Z",
    "peer": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2",
      "online": true
    }
  },
  "messages": [
    {
      "message_id": "01a1b132-e87a-7000-871c-86ce2c21aef3",
      "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
      "kind": "text",
      "content": "hey alice",
      "created_at": "2026-11-06T14:11:05.978Z",
      "sender": {
        "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
        "username": "carol",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
    },
    {
      "message_id": "01a1b132-ea0e-7000-b9bd-9c36ac677b1e",
      "chat_id": "ea89b58e-fc8c-4c78-b69e-b1a0cbd1f937",
      "kind": "text",
      "content": "hey carol!",
      "created_at": "2026-11-06T14:11:06.382Z",
      "sender": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
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
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-06T14:11:05.978Z",
    "responded_at": "2026-11-06T14:11:06.381Z",
    "message": "hey alice"
  }
}
```

### 2.10 `POST chat/{chat_id}/invite`: invite someone to a group

Auth: any member of the group.

**Changed**: the invited user is not added. They get a pending invite (`event/request`) and
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

**Pushes**: `event/request` (`pending`) to the invited user, only while the invite is still
pending (section 4.3).

The invite ends when the invited user accepts or declines it (section 3.5, 3.6), the inviter
cancels it (3.7), the invited user joins by themselves (3.11, which accepts it), or the group is
deleted (the invite disappears without `event/request`).

**Example**

```json
{"rid": 16, "user_id": "007204d5-49fb-41cc-b93c-593d8380c338"}
```

```json
{
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/invite",
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
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-06T14:11:01.910Z",
    "responded_at": null,
    "chat": {"chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4", "name": "Network Project"}
  }
}
```

### 2.11 `PATCH users/profile/password`: change your password

Auth: yes. Two requests on the same connection.

**Changed**: challenge-response (`ChangePasswordFlow` in `socketcord/scram.py`). Step 2 proves the
current password with a proof that also covers both Diffie-Hellman values and the encrypted new
verifier, so nobody in the middle can swap them. The change also **replaces the session token**:
the final reply carries the new one, and every other connection that used the old token is
logged out.

**Step 1 request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `client_nonce` | string | yes | base64 text, 16–128 characters |

**Step 1 reply**: `step` (`"challenge"`), `challenge_id`, `salt` and `iterations` (the current
password's), `server_nonce`, `expires_in`, `dh_group`, `dh_public` as in register, plus:

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `new_salt` | string | base64 of 16 bytes for the new password |
| `new_iterations` | integer | PBKDF2 rounds for the new password (`SCRAM_ITERATIONS`) |

**Step 2 request**

| Field | Type | Required | Rules |
| ----- | ---- | -------- | ----- |
| `challenge_id` | string | yes | from step 1 |
| `client_proof` | string | yes | base64 of 32 bytes: proof with the current password over the full AuthMessage |
| `dh_public` | string | yes | base64 of 256 bytes |
| `wrapped_verifier` | string | yes | base64 of 64 bytes: the new password's StoredKey ‖ ServerKey, encrypted |
| `wrap_tag` | string | yes | base64 of 32 bytes |

The AuthMessage holds your `user_id` in lower case with hyphens, as replies write it;
`ChangePasswordFlow` builds it that way ([PROTOCOL.md](PROTOCOL.md) section 4).

**Step 2 reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `step` | string | `"done"` |
| `user_id` | string | your account |
| `session_token` | string | the account's **new** session token. Put it in the header from now on (`RequestCord.set_auth(user_id, session_token)`). |
| `expires_at` | timestamp | when the new session ends (`SESSION_TTL_HOURS` after the change) |
| `logged_out_connections` | integer | how many other connections used the old token and were logged out |
| `server_signature` | string | base64 of 32 bytes, made with the old password's ServerKey; `ChangePasswordFlow.verify(reply)` checks it |

The old token keeps working **on this connection only, for 30 seconds**, so requests sent before
the reply arrived still go through. Everywhere else it stops at once.

**Errors**. Failures answer 252, not 251, because the session is still valid. The one 251,
`INVALID_SESSION`, means the session is gone:

| Status | Code | When |
| ------ | ---- | ---- |
| 250 | `PLAINTEXT_PASSWORD` | the message has `password`, `old_password` or `new_password` |
| 252 | `TOO_MANY_ATTEMPTS` | step 1: this address asked for too many challenges; step 1 or 2: too many failed password checks for this account from this address (section 1.7); `retry_after` |
| 250 | `MISSING_FIELD`, `INVALID_FIELD` | a field is missing or malformed |
| 252 | `CHALLENGE_INVALID` | step 2: the challenge is unknown, used, expired, from another connection, or from an earlier session of this account; or the password was changed in between ("start again") |
| 252 | `INVALID_CREDENTIALS` | step 2: the current password is wrong |
| 250 | `INVALID_PROOF` | step 2: the encrypted new verifier failed its integrity check |
| 251 | `INVALID_SESSION` | step 2: the session ended while the change ran (a logout, or a login elsewhere); or the change went through, but a login with the new password replaced the new session at that same moment |
| 250 | `CONNECTION_CLOSED` | step 1: the connection closed while the challenge was being made, so none was kept; nobody reads this reply |
| | common errors | section 1.5 |

After any step-2 error, start again with a new `ChangePasswordFlow`.

**Pushes** (step 2): `event/session_revoked` with reason `password_changed` to every other
connection that used the old token.

**Example** (with credentials). Step 1:

```json
{"rid": 15, "client_nonce": "VlG5TGjc/42L20yPa9XnNQby"}
```

```json
{
  "path": "users/profile/password",
  "method": "PATCH",
  "rid": 15,
  "step": "challenge",
  "challenge_id": "59effda0-51fe-4e62-b56f-edce74fb9ed0",
  "salt": "D9YAbW+LifqfEFyx/6sprg==",
  "iterations": 600000,
  "server_nonce": "U/In2VAJ+K2yRkg1xwFafRmg",
  "expires_in": 60,
  "dh_group": "modp2048",
  "dh_public": "2/bwyxKLzRVkYKhr...5A6KTA==",
  "new_salt": "Nx4xKCRFMb/5icnO3ZTFaw==",
  "new_iterations": 600000
}
```

Step 2:

```json
{
  "rid": 16,
  "challenge_id": "59effda0-51fe-4e62-b56f-edce74fb9ed0",
  "dh_public": "hFpEm94aN5thnnfl...oTSTzQ==",
  "wrapped_verifier": "mz/Y+1yavemQYHhv...LCkZiQ==",
  "wrap_tag": "S+J1ybl8uqrqmCHPNPeiZTol4jos16QXBC/QAu4F/oY=",
  "client_proof": "uWwlLLzo4Qw4q14txQFsXfij6HuV3awZ1sE1kWOoM5U="
}
```

```json
{
  "path": "users/profile/password",
  "method": "PATCH",
  "rid": 16,
  "step": "done",
  "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
  "session_token": "f5bbc395-c838-4367-910f-0845861df3d7",
  "expires_at": "2026-11-13T14:11:10.512Z",
  "logged_out_connections": 1,
  "server_signature": "ZbyCNY96LZel9M/DJ7XkvbdZHIcqQCBSNlxDIezqKO0="
}
```

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

**Pushes**: `event/session_revoked` with reason `logged_out` to every other connection that used
the same token; `event/presence` (offline) to everyone else at once, if the account has no
logged-in connection left.

**Example**: `POST users/logout` with an empty message:

```json
{"path": "users/logout", "method": "POST", "rid": 46, "logged_out": true}
```

### 3.2 `GET users/online`: who is online

Auth: yes. Course requirement R4: the list includes you.

**Request**: no fields.

**Reply**

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `count` | integer | number of entries |
| `users` | list | every account with a logged-in connection, sorted by username ignoring case; each is a User without `created_at`, plus `is_self` (boolean). Someone who lost their last connection less than 2 s ago is still listed. |
| `presence_seq` | integer | the `seq` of the newest `event/presence` sent so far; ignore any `event/presence` whose `seq` is not greater |

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
      "text_bg_color": "#f2f2f2",
      "online": true,
      "is_self": true
    },
    {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb",
      "online": true,
      "is_self": false
    },
    {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2",
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
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb",
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
        "text_bg_color": "#f2f2f2"
      },
      "to": {
        "user_id": "fd507003-8c4e-4d14-a2b6-56650d0e04c9",
        "username": "bob",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      },
      "created_at": "2026-11-06T14:11:25.032Z",
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
        "text_bg_color": "#f2f2f2"
      },
      "to": {
        "user_id": "fd507003-8c4e-4d14-a2b6-56650d0e04c9",
        "username": "bob",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      },
      "created_at": "2026-11-06T14:11:25.033Z",
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

* DM request: `event/chat` (`dm_opened`), then `event/message` with the first message, both to
  every connection of both people, this one included (here they arrive before the reply); then
  `event/request` (`accepted`) to the sender.
* Invite: `event/message` (the system line) to every online member except this connection (your
  other connections get it; the reply carries the line as `chat.last_message`),
  `event/group_updated` (`joined`) to everyone, this connection included, and `event/request`
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
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb"
    },
    "created_at": "2026-11-06T14:11:04.757Z",
    "responded_at": "2026-11-06T14:11:05.164Z",
    "message": "hi bob, got a minute?"
  },
  "chat": {
    "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "is_dm": true,
    "name": "alice",
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 1,
    "last_read_message_id": null,
    "last_message": {
      "message_id": "01a1b132-e3b5-7000-8839-be47fdf56d69",
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "text",
      "content": "hi bob, got a minute?",
      "created_at": "2026-11-06T14:11:04.757Z",
      "sender": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
    },
    "last_activity": "2026-11-06T14:11:04.757Z",
    "peer": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2",
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

**Pushes**: `event/request` (`declined`) to the sender.

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
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb"
    },
    "created_at": "2026-11-06T14:11:07.226Z",
    "responded_at": "2026-11-06T14:11:07.227Z",
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

**Pushes**: `event/request` (`cancelled`) to the receiver.

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
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-06T14:11:07.634Z",
    "responded_at": "2026-11-06T14:11:07.634Z",
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

**Pushes**: `event/group_created` to every logged-in connection except this one.

**Example**

```json
{"rid": 10, "name": "Network Project", "description": "term project"}
```

```json
{
  "path": "chats",
  "method": "POST",
  "rid": 10,
  "chat": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "role": "owner",
    "member_count": 1,
    "unread": 0,
    "last_read_message_id": null,
    "last_message": null,
    "last_activity": "2026-11-06T14:11:00.277Z"
  },
  "group": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "created_at": "2026-11-06T14:11:00.277Z",
    "created_by": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "member_count": 1,
    "online_count": 1,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2",
        "role": "owner",
        "joined_at": "2026-11-06T14:11:00.277Z",
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
      "created_at": "2026-11-06T14:11:00.277Z",
      "created_by": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      },
      "member_count": 1,
      "online_count": 1,
      "members": [
        {
          "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
          "username": "alice",
          "text_color": "#171717",
          "text_bg_color": "#f2f2f2",
          "role": "owner",
          "joined_at": "2026-11-06T14:11:00.277Z",
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
| `message_id` | string | no | UUID of the newest message you have seen; default: the chat's newest message |

**Effect**: your read marker moves forward to the newest message of this chat whose id is at or
below `message_id` (without `message_id`: the chat's newest message). The marker is therefore
always a message of this chat: an id from another chat, or one that is no message at all, marks
this chat's messages up to that id as read and does not become the marker itself. The marker
never moves back: if that message is older than your marker, nothing changes. `unread` in
`GET chats` and `first_unread_message_id` count from this marker.

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
| 250 | `INVALID_FIELD` | `message_id` is not a UUID |
| | common errors | section 1.5 |

**Pushes**: none. Your other connections are not told; their `GET chats` shows the new counts.

**Example**: `POST chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/read` with no `message_id`:

```json
{"rid": 19}
```

```json
{
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/read",
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

**Pushes**: `event/message` (the system line) to every online member except this connection (your
other connections get it; the reply carries the line as `chat.last_message`);
`event/group_updated` (`joined`) to everyone, this connection included (it arrives just before
the reply); and `event/request` (`accepted`) to the inviter if a pending invite was settled.

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
    "description": "term project",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
    "last_message": {
      "message_id": "01a1b132-d3cc-7000-8d75-cf656ede128e",
      "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
      "kind": "system",
      "content": "bob joined the group",
      "created_at": "2026-11-06T14:11:00.684Z",
      "sender": null
    },
    "last_activity": "2026-11-06T14:11:00.684Z"
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
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-06T14:11:00.277Z",
    "member_count": 2,
    "online_count": 2,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2",
        "role": "owner",
        "joined_at": "2026-11-06T14:11:00.277Z",
        "online": true
      },
      {
        "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
        "username": "bob",
        "text_color": "#ffffff",
        "text_bg_color": "#1f6feb",
        "role": "member",
        "joined_at": "2026-11-06T14:11:00.684Z",
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

**Pushes**: if the group was deleted, `event/group_deleted` to everyone (you included).
Otherwise `event/message` (the system line) to the remaining online members and
`event/group_updated` (`left`) to everyone, you included. Pending invites of a deleted group
disappear without `event/request`.

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
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/summary",
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
  "from_time": "2026-11-06T14:11:00.684Z",
  "to_time": "2026-11-06T14:11:02.317Z",
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
  "path": "chat/ce90105a-ad2f-4914-97bf-37aa3e306db4/summary",
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
  "from_time": "2026-11-06T14:11:00.684Z",
  "to_time": "2026-11-06T14:11:02.317Z",
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
  "path": "chat/d3e539df-54fd-434c-82fe-c0fcb466cf9f/summary",
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

**Pushes**: none. Keep a shown list current with `event/group_updated` (`joined` / `left`, whose
`user` is that user) and `event/group_deleted`, or ask again.

**Example**: carol asks for alice's groups:

```json
{
  "path": "users/a5a12c62-a12b-4c9c-aa45-4e2fad035b70/groups",
  "method": "GET",
  "rid": 6,
  "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
  "user": {
    "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
    "username": "alice",
    "text_color": "#171717",
    "text_bg_color": "#f2f2f2",
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
      "created_at": "2026-11-07T07:22:24.080Z",
      "member_count": 1,
      "role": "owner",
      "joined_at": "2026-11-07T07:22:24.080Z",
      "joined": false
    },
    {
      "chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030",
      "kind": "group",
      "is_dm": false,
      "name": "Network Project",
      "description": "term project",
      "created_at": "2026-11-07T07:22:24.080Z",
      "member_count": 2,
      "role": "owner",
      "joined_at": "2026-11-07T07:22:24.080Z",
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
| `users` | list | each `{"user_id", "username", "text_color", "text_bg_color", "online"}` (a User without `created_at`); at most 50 |

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
      "text_bg_color": "#f2f2f2",
      "online": true
    },
    {
      "user_id": "c2cd251f-8294-4fd9-ba19-0601dcd08fdd",
      "username": "dave",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2",
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

**Pushes**: the same as accepting an invite in section 3.5: `event/message` (the system line) to
every online member except this connection, `event/group_updated` (`joined`) to everyone, this
connection included, and `event/request` (`accepted`) to the inviter.

**Example**: carol accepts alice's invite to "Network Project"
(`POST chat/50899369-a177-46e6-8cd5-1bb6da9c2030/invite/accept`, empty message):

```json
{
  "path": "chat/50899369-a177-46e6-8cd5-1bb6da9c2030/invite/accept",
  "method": "POST",
  "rid": 7,
  "chat": {
    "chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "role": "member",
    "member_count": 3,
    "unread": 0,
    "last_read_message_id": "01a1b4e3-17e5-7000-8a61-735f1072fb12",
    "last_message": {
      "message_id": "01a1b4e3-17e5-7000-8a61-735f1072fb12",
      "chat_id": "50899369-a177-46e6-8cd5-1bb6da9c2030",
      "kind": "system",
      "content": "carol joined the group",
      "created_at": "2026-11-07T07:22:24.101Z",
      "sender": null
    },
    "last_activity": "2026-11-07T07:22:24.101Z"
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
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-07T07:22:24.080Z",
    "member_count": 3,
    "online_count": 3,
    "members": [
      {
        "user_id": "a5a12c62-a12b-4c9c-aa45-4e2fad035b70",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2",
        "role": "owner",
        "joined_at": "2026-11-07T07:22:24.080Z",
        "online": true
      },
      {
        "user_id": "2fb20bce-8f92-4892-9f7f-4349743f8b95",
        "username": "Bobby",
        "text_color": "#ffffff",
        "text_bg_color": "#1f6feb",
        "role": "member",
        "joined_at": "2026-11-07T07:22:24.081Z",
        "online": true
      },
      {
        "user_id": "50245c20-966f-4dc8-a71e-dfe2de0d9802",
        "username": "carol",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2",
        "role": "member",
        "joined_at": "2026-11-07T07:22:24.101Z",
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
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "50245c20-966f-4dc8-a71e-dfe2de0d9802",
      "username": "carol",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-07T07:22:24.101Z",
    "responded_at": "2026-11-07T07:22:24.101Z",
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

**Pushes**: `event/request` (`declined`) to the inviter.

**Example**: dave declines alice's invite to "Book Club":

```json
{
  "path": "chat/d423ebd8-5858-48d3-a1f7-878576ba8aaf/invite/reject",
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
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "c2cd251f-8294-4fd9-ba19-0601dcd08fdd",
      "username": "dave",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-07T07:22:24.102Z",
    "responded_at": "2026-11-07T07:22:24.102Z",
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

A push is a reply packet nobody asked for: status 200, `path` = `event/<name>`, and no `method`
or `rid`. Pushes go only to logged-in connections. In this section:

* "everyone" means every logged-in connection on the server;
* "not to the acting connection" means the connection whose request caused the push does not get
  it, while that user's other connections do.

Pushes about one chat leave the server in the order the changes were committed. For example,
`event/group_created` comes before any `event/message` or `event/group_updated` of that group,
and a `"pending"` `event/request` never arrives after a newer state of the same request. On the
connection that made a call, the pushes that call causes for that connection arrive before its
reply. A client that stops reading is disconnected once more than 8 MB of pushes wait for it
([PROTOCOL.md](PROTOCOL.md) section 1).

### 4.1 `event/message`: a new message in a chat

**When**: a message is stored: `POST chat/{chat_id}/message`, `POST users/{user_id}/dm` into a
room, the system lines of joining and leaving, and, when a DM room opens, the first message that
the DM request carried.

**To**: every logged-in connection of the chat's members (the two people of a DM), except:

* a text message is not sent to the connection that sent it (its reply carries it);
* a join line (`"bob joined the group"`) is not sent to the connection that joined (its reply
  carries the line as `chat.last_message`); the joiner's other connections get it;
* a leave line goes to the members who remain, so none of the leaver's connections get it.

When a DM request is accepted, its first message goes to every connection of both people, the
acting connection included, right after `event/chat`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the chat |
| `chat_kind` | string | `"group"` or `"dm"` |
| `message` | Message | |

```json
{
  "path": "event/message",
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "chat_kind": "group",
  "message": {
    "message_id": "01a1b132-d564-7000-8685-b94d127a7451",
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "text",
    "content": "hello group",
    "created_at": "2026-11-06T14:11:01.092Z",
    "sender": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    }
  }
}
```

### 4.2 `event/presence`: someone came online or went offline

**When**:

* online: an account that had no logged-in connection gets one (a login, or the first
  authenticated request on a connection);
* offline: its last connection is logged out. This is immediate for a logout, a revoked or
  expired session, or a connection switching accounts. When the last connection closes, offline
  comes 2 s later, and not at all if the account reconnects within those 2 s.

**To**: everyone (for "online", not the connection that just logged in).

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `user` | User without `created_at` | `online` in it equals the field below |
| `online` | boolean | |
| `seq` | integer | grows by one with every presence push, server-wide (it starts again at 1 when the server restarts). Ignore a push whose `seq` is not greater than the newest you have, including `presence_seq` of `GET users/online`. |

```json
{
  "path": "event/presence",
  "user": {
    "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
    "username": "bob",
    "text_color": "#171717",
    "text_bg_color": "#f2f2f2",
    "online": true
  },
  "online": true,
  "seq": 2
}
```

```json
{
  "path": "event/presence",
  "user": {
    "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
    "username": "alice",
    "text_color": "#171717",
    "text_bg_color": "#f2f2f2",
    "online": false
  },
  "online": false,
  "seq": 5
}
```

### 4.3 `event/request`: a DM request or invite was made or answered

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
`chat.chat_id` arrives in `event/group_deleted`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `request` | Request | the request with its new status |

```json
{
  "path": "event/request",
  "request": {
    "request_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "status": "pending",
    "from": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb"
    },
    "created_at": "2026-11-06T14:11:04.757Z",
    "responded_at": null,
    "message": "hi bob, got a minute?"
  }
}
```

```json
{
  "path": "event/request",
  "request": {
    "request_id": "04d5ffc9-faa5-48f3-9d60-3b7a3fdae037",
    "kind": "invite",
    "status": "accepted",
    "from": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "to": {
      "user_id": "007204d5-49fb-41cc-b93c-593d8380c338",
      "username": "carol",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "created_at": "2026-11-06T14:11:01.910Z",
    "responded_at": "2026-11-06T14:11:02.317Z",
    "chat": {"chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4", "name": "Network Project"}
  }
}
```

### 4.4 `event/chat`: a DM room opened

**When**: the first DM between two people (`POST users/{user_id}/dm` with the default
`DM_REQUIRE_ACCEPT=false`); or a pending DM request opens: its receiver accepts it or writes back,
or (with `DM_REQUIRE_ACCEPT=false`) its sender writes again.

**To**: every logged-in connection of both people, the acting connection included, each person
with the chat as it appears in their own `GET chats`. It always arrives before the room's first
`event/message`, so the client knows the `chat_id` first; on the acting connection it also
arrives before the reply. An accepted DM request's first message follows it as `event/message`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `reason` | string | `"dm_opened"` (the only value) |
| `chat` | Chat item | |

alice's copy when bob accepts her DM request (her first message is already read for her):

```json
{
  "path": "event/chat",
  "reason": "dm_opened",
  "chat": {
    "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
    "kind": "dm",
    "is_dm": true,
    "name": "bob",
    "description": "",
    "role": "member",
    "member_count": 2,
    "unread": 0,
    "last_read_message_id": "01a1b132-e3b5-7000-8839-be47fdf56d69",
    "last_message": {
      "message_id": "01a1b132-e3b5-7000-8839-be47fdf56d69",
      "chat_id": "99b362bb-971b-4f39-a15a-b95612bcd979",
      "kind": "text",
      "content": "hi bob, got a minute?",
      "created_at": "2026-11-06T14:11:04.757Z",
      "sender": {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2"
      }
    },
    "last_activity": "2026-11-06T14:11:04.757Z",
    "peer": {
      "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
      "username": "bob",
      "text_color": "#ffffff",
      "text_bg_color": "#1f6feb",
      "online": true
    }
  }
}
```

### 4.5 `event/group_created`: a new group

**When**: `POST chats`. **To**: everyone except the creating connection.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `group` | Group without `joined` | |

```json
{
  "path": "event/group_created",
  "group": {
    "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
    "kind": "group",
    "is_dm": false,
    "name": "Network Project",
    "description": "term project",
    "created_at": "2026-11-06T14:11:00.277Z",
    "created_by": {
      "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
      "username": "alice",
      "text_color": "#171717",
      "text_bg_color": "#f2f2f2"
    },
    "member_count": 1,
    "online_count": 1,
    "members": [
      {
        "user_id": "08d17d12-713f-45c0-986b-46af83c8968d",
        "username": "alice",
        "text_color": "#171717",
        "text_bg_color": "#f2f2f2",
        "role": "owner",
        "joined_at": "2026-11-06T14:11:00.277Z",
        "online": true
      }
    ],
    "members_truncated": false
  }
}
```

### 4.6 `event/group_updated`: someone joined or left a group

**When**: a join (`POST chat/{chat_id}/join`, or accepting an invite with
`POST users/requests/{request_id}/accept` or `POST chat/{chat_id}/invite/accept`) or a leave that
does not delete the group. **To**: everyone, the actor included.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the group |
| `name` | string | its name |
| `member_count` | integer | members after the change |
| `change` | string | `"joined"` or `"left"` |
| `user` | User ref | who joined or left |

It is a small change notice, not the member list; fetch `GET chat/{chat_id}/info` for that.

```json
{
  "path": "event/group_updated",
  "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4",
  "name": "Network Project",
  "member_count": 2,
  "change": "joined",
  "user": {
    "user_id": "4acc2abf-57e9-426d-a52c-90d65a77743b",
    "username": "bob",
    "text_color": "#ffffff",
    "text_bg_color": "#1f6feb"
  }
}
```

### 4.7 `event/group_deleted`: a group is gone

**When**: its last member left; its messages and pending invites are deleted with it. **To**:
everyone, the actor included.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `chat_id` | string | the deleted group |

```json
{"path": "event/group_deleted", "chat_id": "ce90105a-ad2f-4914-97bf-37aa3e306db4"}
```

### 4.8 `event/profile`: someone changed their name or colours

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
  "path": "event/profile",
  "user": {
    "user_id": "2fb20bce-8f92-4892-9f7f-4349743f8b95",
    "username": "Bobby",
    "text_color": "#ffffff",
    "text_bg_color": "#1f6feb",
    "online": true
  }
}
```

### 4.9 `event/session_revoked`: this connection was logged out

**When**: the server logs a connection out by itself. The push comes first; then the connection
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
| `password_changed` | a password change on another connection replaced the token |
| `expired` | the session reached its `expires_at` (checked every 5 s, and on each request) |
| `too_many_connections` | rare: a login found `MAX_CONNECTIONS_PER_USER` connections of the account logged in on a session it was not replacing, and this was the oldest. That can only happen while the account's session changes at that same moment (another login, a logout, a password change). An ordinary login on a sixth connection does not cause it: it replaces the session, and the old connections get `logged_in_elsewhere`. |

Treat every reason as "logged out". After `logged_in_elsewhere`, do not log in again
automatically: two clients that do would keep logging each other out.

```json
{"path": "event/session_revoked", "reason": "logged_in_elsewhere"}
```

---

## 5. Frontend adapter

How the replies feed the data-in methods of `chat_page.py` (frontend commit `b43be55`), and what
the client has to do so that the server's view and the GUI agree.

### 5.1 API objects to `chat_page.py`

| `chat_page.py` method | Feed it from | Mapping |
| --------------------- | ------------ | ------- |
| `set_user_style(username, text_color, background)` | any User, User ref or Member: the login reply's `user`, `members`, message `sender`, `event/profile`, `event/presence` | `(u["username"], u["text_color"], u["text_bg_color"])`. Call it before `set_members` / `set_messages`, since it redraws them. After a rename (`event/profile`) the old name is gone: look the person up by `user_id`. |
| `set_profile(username)` | the login reply's `user` (or `GET users/info`) | `user["username"]` |
| `set_chats(chats, selected_id)` | `GET chats` → `chats` | each Chat item already has `chat_id`, `name`, `unread`, `is_dm` and `last_activity`; convert `last_activity` to a `datetime` (below). The GUI adds `[DM]` in front of DM names itself. |
| `set_members(members)` | `GET chat/{chat_id}/info` → `chat["members"]` | `{"username": m["username"], "online": m["online"], "is_me": m["user_id"] == my_user_id}`. Pending invites (`chat["invited"]`) have no place in the current GUI. |
| `set_messages(messages, first_unread_id)` | `GET chat/{chat_id}/messages` | `messages` mapped as below; `first_unread_id = reply["first_unread_message_id"]`. The NEW line is drawn only if that message is in the list. |
| `append_message(message)` | `event/message` for the open chat; the `message` in your own `POST chat/{chat_id}/message` reply | mapped as below; add `"status": "Sent"` to your own |
| `set_summary(text)` | `GET chat/{chat_id}/summary` | `reply["summary"]`, or a placeholder such as "Nothing to summarize" when it is `null`; when `engine` is `"offline"` the text says it is not written by AI |
| `set_chat_title(title)` | Chat item / Chat info | `name` (selecting a chat in the list already sets it) |

```python
from datetime import datetime


def ts(s):
    """API timestamp -> local datetime, on Python 3.9 and later."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone() if s else None


def chat_row(c):
    """GET chats item (or event/chat "chat") -> a set_chats() row."""
    return {"chat_id": c["chat_id"], "name": c["name"], "unread": c["unread"],
            "last_activity": ts(c["last_activity"]), "is_dm": c["is_dm"]}


def message_row(m, my_user_id):
    """Message -> a set_messages() / append_message() row."""
    sender = m["sender"]
    return {"id": m["message_id"],
            "sender": sender["username"] if sender else "(system)",   # system lines have no sender
            "text": m["content"],
            "time": ts(m["created_at"]),
            "own": sender is not None and sender["user_id"] == my_user_id}
```

`"(system)"` cannot be anyone's username (parentheses are not allowed in names). Keep every id as
the string the server sent; the demo data's integer ids are only for the preview.

The GUI's placeholder hooks map to these calls:

| Hook in `chat_page.py` | Call |
| ---------------------- | ---- |
| `edit_profile` / `request_edit_profile(username, text_color, background)` | `PATCH users/profile` with `{"username", "text_color", "background"}` (send only what changed; `background` = `text_bg_color`) |
| `change_password` / `request_change_password` | `PATCH users/profile/password` with `ChangePasswordFlow` (two steps; never a `password` field), then `RequestCord.set_auth(user_id, session_token)` |
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
   when an `event/message` arrives for the chat on screen. The server marks as read only your own
   messages and the history from before you joined; without this call `unread` and the NEW line
   never go away.
2. **Logged out.** A `251` reply to a call sent with credentials (every call except `POST users`
   and `POST users/login`), or `event/session_revoked` with any reason, means this connection is
   no longer logged in: clear the credentials (`RequestCord.clear_auth()`) and show the login
   page. The socket stays open, so the user can log in again on it. A `251` from `POST users` or
   `POST users/login` only means that attempt failed: show the error and let the user try again.
3. **Never log in automatically after `logged_in_elsewhere`.** Tell the user the account was
   opened somewhere else and wait for them. Two clients that log in by themselves would keep
   logging each other out.
4. **Pending requests and invites.** After login, `GET users/requests` and show `incoming`
   (accept, decline) and `outgoing` (cancel). Keep the list current with `event/request`
   (`pending`: add; `accepted`, `declined`, `cancelled`: remove), and remove invites whose
   `chat.chat_id` arrives in `event/group_deleted`. An invite can be answered by its group too
   (`POST chat/{chat_id}/invite/accept` / `reject`). After accepting, add the reply's `chat` to the
   chat list. An accepted DM request also arrives as `event/chat` on every connection of both
   people, the accepting connection included (there it comes before the reply).
5. **DMs open at once by default.** With the default `DM_REQUIRE_ACCEPT=false`, the first DM
   creates the room (`event/chat` reaches both people) and nothing waits for approval. A server
   run with `DM_REQUIRE_ACCEPT=true` turns the first DM into a request that the receiver must
   accept (item 4).
6. **A long timeout for the summary.** Wait up to 90 s for `GET chat/{chat_id}/summary`, or send
   it on a second connection that uses the same User ID and Session Token (never `users/login`
   there). The connection's other requests wait behind it.
7. **New token after a password change.** Put `session_token` from the change's final reply into
   the header at once (`RequestCord.set_auth`). The old token works only on that connection and
   only for 30 s.
8. **Your own messages.** The connection that sent a text message gets no `event/message` for it:
   append the `message` from the reply (match it by `rid` if the GUI shows "sending" first).
   Likewise, the connection that joins a group gets no `event/message` for its own join line: the
   reply's `chat.last_message` is that line.
9. **Chat list updates.** `event/message` for a chat that is not open: increase its `unread`
   (unless you sent it) and move it to the top with the new `last_activity`. A chat is added (or
   replaced, if it is listed already) by `event/chat`, which reaches every connection of both
   people, the acting one included, and by the replies of `POST chats`,
   `POST chat/{chat_id}/join`, `POST chat/{chat_id}/invite/accept`,
   `POST users/requests/{request_id}/accept` and `POST users/{user_id}/dm` (status `"sent"`). Your other connections learn about a group you
   created or joined on another connection from `event/group_created` whose `created_by` is you
   and `event/group_updated` `"joined"` whose `user` is you; neither carries a Chat item, so fetch
   the row with `GET chats`. The leave reply, `event/group_updated` `"left"` whose `user` is you,
   and `event/group_deleted` remove a chat. `GET chats` is always the truth if in doubt.
10. **Routing.** Route on `path` and `method`, and on `rid` for replies; `event/...` paths are
    pushes. Convert every timestamp with `ts()` before giving it to the GUI.
