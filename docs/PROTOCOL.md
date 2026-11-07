# SocketCord protocol, as implemented by the backend

This document describes what the backend expects and what it sends back.
The packet layout is the one in the frontend's README and `models.py`, and
it is unchanged. The additions are a few fields in the JSON message, server
pushes, and some new calls. Section 2 lists every change. Every call and
push, field by field, is in [API.md](API.md).

This is **wire contract v2**: register, log in and change your password each
take one request, with the password in the message body as clear text. The
socket has no TLS, so the password is readable on the wire; the server stores
a salted PBKDF2-HMAC-SHA256 hash and never the password. Section 4 says
exactly what that does and does not protect.

All integers are big-endian (network byte order). The transport is one
long-lived TCP connection per client; the default port is 8080.

---

## 1. Packets

### Request (client → server), 48-byte header

| Size (bytes) | Field            | Notes                                          |
| ------------ | ---------------- | ---------------------------------------------- |
| 4            | Method Code      | `POST = 112233445`, `GET = 354984100`, `PATCH = 943742156` |
| 16           | User ID          | raw UUID bytes (`uuid.bytes`), all zero when logged out |
| 16           | Session Token    | raw UUID bytes, all zero when logged out       |
| 2            | URL Size         | bytes of the UTF-8 URL                         |
| 2            | Message Size     | bytes of the UTF-8 message                     |
| 4            | URL Checksum     | `zlib.crc32(url_bytes)`                        |
| 4            | Message Checksum | `zlib.crc32(message_bytes)`                    |
| variable     | URL              | e.g. `chat/<chat_id>/messages`; a leading/trailing `/` is ignored |
| variable     | Message          | a JSON object, or empty (= `{}`)               |

### Reply (server → client), 8-byte header

| Size (bytes) | Field                  |
| ------------ | ---------------------- |
| 1            | Status Code            |
| 3            | Reply Message Size     |
| 4            | Reply Message Checksum (`zlib.crc32`) |
| variable     | Reply Message (JSON object) |

Every reply message carries these fields:

| Field    | When                     | Meaning |
| -------- | ------------------------ | ------- |
| `path`   | always                   | The semantic name of the call this reply answers (e.g. `login/success` for `POST users/login`, `chat/messages` for `GET chat/{id}/messages`); the table at the top of [API.md](API.md) has all of them, and a call the contract does not name answers with its URL, without leading/trailing `/`. A reply whose status is not 200 carries the request URL instead, since the semantic name is the name of the call succeeding: a wrong password answers `users/login`, not `login/success`. A push has one of the nine push paths (section 6) and no `method`. This is the "context" that lets the client route a reply. |
| `method` | every reply (not pushes) | The request's method: `"POST"`, `"GET"` or `"PATCH"`, also when the packet's checksum or UTF-8 check failed. `GET chats` and `POST chats` share a path, so route on both. For an unknown method code it is the code in decimal (e.g. `"12345"`). |
| `rid`    | if the request had a valid one | The request's own `rid` echoed back (an integer of at most 20 digits, or a string of at most 64 characters). Use it to match a reply to the request that caused it, e.g. to mark a chat bubble as "✓ Sent". |
| `error`  | status ≠ 200             | Machine-readable code, e.g. `USERNAME_TAKEN` (table in section 3). |
| `detail` | status ≠ 200             | One sentence for humans. Safe to show in the status label. |

Some errors add `field` (the request field at fault) or `retry_after`
(seconds); see section 3. A server push carries only `path` and its own
fields: no `method`, no `rid`.

### Framing and flow control (both directions)

* TCP is a byte stream. One `recv()` can return half a packet, exactly one
  packet, several packets, or the end of one plus the start of the next. Both
  sides keep a buffer and only parse once `header + sizes` bytes are there.
  The server's parser is `socketcord.protocol.RequestParser`; the frontend's
  `proc_buffer` does the same for replies.
* A packet whose checksum does not match is answered with `250` and
  `URL_CHECKSUM_MISMATCH` or `MESSAGE_CHECKSUM_MISMATCH`. That reply has no
  `rid`, because the message could not be trusted; its `method` is still the
  method's name. The sizes in the header are still used, so the next packet
  parses normally.
* Requests from one connection are handled one at a time, in order, so
  replies come back in request order. Pushes can arrive between replies.
* Flow control: the server does not start a connection's next request while
  more than 1 MB of reply to it is still unsent, and it stops reading the
  socket while 32 requests (or 1 MB of them) are waiting. TCP's receive window
  then fills and the client's `send()` blocks until the server catches up.
  Nothing is refused or reordered.
* A client that stops reading is disconnected once more than 8 MB of pushes
  wait for it, or when nothing could be sent to it for 60 s. Replies do not
  count toward the 8 MB, so a client that keeps reading is never dropped.
* Connections must make progress. The server closes a connection that sends
  no complete request within 30 s of connecting, or sits on a half-sent
  packet for 30 s, or stays idle for 5 minutes without logging in.
  Logged-in connections may idle forever; TCP keepalive (about 2 minutes)
  notices peers that vanished.
* One address may hold at most 64 connections (`MAX_CONNECTIONS_PER_IP`; 512
  in the compose file, where Docker can show every client as one address).
  One account may have at most 5 logged-in connections
  (`MAX_CONNECTIONS_PER_USER`). When the server is full, the longest-idle
  connection that never logged in is dropped to make room.
* Half-close: if a client calls `shutdown(SHUT_WR)` after sending, it still
  gets a reply to everything it sent before the server closes the connection.

### Message content rules

The message must be a JSON object of at most 32 KB (`250 MESSAGE_TOO_LARGE`)
and at most 16 levels deep. Strings may not contain NUL characters or
unpaired UTF-16 surrogates (`"\ud800"`), integers may have at most 20
digits, and numbers must be finite (no `NaN`, `Infinity`, `1e400`). A
message that breaks these rules gets `250 INVALID_JSON` and is not
processed. Whenever the `rid` can still be read, the error reply echoes it;
a `rid` that itself breaks a rule (an integer of more than 20 digits, `NaN`,
a string with a NUL) is not echoed.
Replies echo at most 256 characters of the URL in `path`.

Text fields refuse control characters (`250 INVALID_FIELD`). Message
content, DM text and group descriptions may contain newlines and tabs;
names and every other field may not. Text fields are trimmed before their
length is checked, except the message body of `POST chat/{chat_id}/message`
and the passwords, which are kept exactly as sent.

---

## 2. What changed compared with the frontend README

The binary packet layout is unchanged. Everything else that differs:

1. **Reply context.** Every reply has `path` (the name of the call) and
   `method` (`"POST"`, `"GET"`, `"PATCH"`), and echoes `rid` when the request
   carried one.
2. **Server pushes.** The server can send a reply packet nobody asked for,
   with status 200, one of nine semantic paths, and no `method` and no `rid`
   ([API.md](API.md) section 4). This delivers new messages, presence
   changes, invitations, new DM rooms, group changes and logouts as they
   happen.
3. **Semantic reply paths.** `path` is the name of the call, not the URL:
   `POST users/login` answers `login/success`, `GET chats` answers
   `chats/list`. An error answers with the request URL, so it never claims the
   path of the call succeeding. Calls the contract does not name keep their URL.
4. **One-step passwords.** `POST users`, `POST users/login` and
   `PATCH users/password` take the password in the message body, in clear
   text (section 4). The password change asks for no current password and
   does not rotate the session token.
5. **Credentials.** A protected call with no User ID/Session Token (all
   zeros) gets `253 Not Found`, as the README says. A call with credentials
   that are wrong, expired or revoked gets `251 Unauthorized`, so the client
   knows to show the login page again.
6. **Data model.** The database is the team's six tables (`users`, `chats`,
   `groups`, `dms`, `group_memberships`, `messages`; see
   `socketcord/db/schema.sql`), and the API follows it:
   * Users have a username and two colours, `text_color` and `background`
     (`"#RRGGBB"` in replies, default `"#171717"` on `"#F2F2F2"`, the
     frontend's own defaults; requests take either case, and
     `text_bg_color` as another name for `background`). There is no display
     name and no bio.
     `PATCH users/profile` sets the colours and can rename the user (the
     name stays unique without regard to case), and every user object in a
     reply carries them, so the GUI can paint names without extra calls.
   * Every id is a UUID string. Message ids are UUIDv7: they grow with time,
     so `before_id`, `after_id` and `last_read_message_id` page and count by
     them, and two ids compare correctly as lower-case strings.
   * By default the first DM opens the DM room at once. With
     `DM_REQUIRE_ACCEPT=true` a DM starts as a **DM request**: a pending `dms` row whose
     `request_id` is the DM's chat id. A group invite is a pending
     `group_memberships` row whose `request_id` is its membership id. The
     receiver accepts or declines, the sender may cancel. Declined and
     cancelled requests are deleted, so answering one again gives
     `253 REQUEST_NOT_FOUND`; answering an accepted one gives
     `252 REQUEST_CLOSED`.
7. **One session per account.** A login replaces the account's session and
   logs out every connection that used the old token (`login/revoked`, reason
   `logged_in_elsewhere`). A password change keeps the token (section 4,
   Sessions).
8. **Reply shapes.**
   * Every reply that adds a chat to your list (`POST chats`,
     `POST chat/{id}/join`, accepting a request, `POST users/{id}/dm`, and
     the `chats/new` push) carries it in the same shape as an item of
     `GET chats`, with `is_dm`, `pending`, `unread` and `last_activity`.
     `GET chats` also lists group invitations you have not answered, as rows
     with `pending: true`.
   * `POST users/{id}/dm` always answers with `status` (`"sent"` or
     `"pending"`), `chat_id`, `chat`, `messages` and `request`.
   * `GET chat/{id}/messages` also returns `last_read_message_id` and
     `first_unread_id` (where the GUI draws its NEW line). A message is
     `{"id", "sender_id", "sender", "text", "time"}`: `sender` is the
     username as a string, `null` on a system line.
   * `GET chat/{id}/info` shows a group's pending invites (`invited`) to its
     members. The `info` in the replies of `POST chat/{id}/join` and of
     accepting an invite is that same object.
   * The `GET chat/{id}/messages` filters `count`, `text` (the README's
     `content`, which still works) and `time` work as the README says, plus
     `before_id` and `after_id`.
9. **New calls**, beyond the README's 11: `POST users/logout`,
   `GET users/online`, `GET users/search`, `GET users/requests`,
   `POST users/requests/{id}/accept|decline|cancel`, `POST chats`,
   `GET chats/groups`, `POST chat/{id}/read`, `POST chat/{id}/join`,
   `POST chat/{id}/leave`, `GET chat/{id}/summary` (also by POST),
   `GET users/{id}/groups`, `GET chat/{id}/invitees`,
   `POST chat/{id}/invite/accept|reject`. All are documented in full in
   [API.md](API.md) section 3.

---

## 3. Status codes and error codes

| Status | Name                  |
| ------ | --------------------- |
| 200    | OK                    |
| 250    | Bad Request           |
| 251    | Unauthorized          |
| 252    | Forbidden             |
| 253    | Not Found             |
| 254    | Internal Server Error |

Every `error` code the backend sends:

| Code | Status | When |
| ---- | ------ | ---- |
| `MISSING_FIELD` | 250 | A required field is absent or `null`. Has `field`. |
| `INVALID_FIELD` | 250 | A field has the wrong JSON type, length or range, a control character, or a bad format. Has `field`. |
| `INVALID_JSON` | 250 | The message is not a JSON object or breaks the content rules of section 1. |
| `MESSAGE_TOO_LARGE` | 250 | The message is over 32 KB. |
| `URL_CHECKSUM_MISMATCH`, `MESSAGE_CHECKSUM_MISMATCH` | 250 | A CRC-32 does not match. No `rid`. |
| `INVALID_ENCODING` | 250 | The URL or message is not UTF-8. No `rid`. |
| `INVALID_USERNAME` | 250 | Register, login or rename (`PATCH users/profile`): the username breaks the rules. Has `field`. |
| `INVALID_TARGET` | 250 | A DM to yourself, or inviting yourself. |
| `INVALID_SESSION` | 251 | The session token is unknown, ended (logout or a newer login), expired, or not this User ID's; also a login whose new session another login of the same account replaced at the same moment, and a password change whose session ended while it ran. |
| `INVALID_CREDENTIALS` | 251 | Login: wrong password, or no such username — the same code and the same sentence for both. Also a login whose account changed its password at that same moment ("log in again"). |
| `USERNAME_TAKEN` | 252 | Register, or rename with `PATCH users/profile`: another account has the name (any letter case). Has `field`. |
| `RATE_LIMITED` | 252 | A per-account limit ([API.md](API.md) section 1.7); too many failed logins for a username from one address (or for unknown names from one address); too many registrations from one address; or the one-account-per-day rule at registration. Has `retry_after`. |
| `LIMIT_REACHED` | 252 | A DM request or invite would give the sender more than `MAX_PENDING_REQUESTS` pending requests sent, or the receiver more than that received. |
| `CONNECTION_IN_USE` | 252 | This connection is logged in as another account. |
| `TOO_MANY_CONNECTIONS` | 252 | The account already has `MAX_CONNECTIONS_PER_USER` logged-in connections, and this connection tried to use its session. (A login is never refused this way: it replaces the session.) |
| `NOT_A_MEMBER` | 252 | Reading, writing, marking as read, inviting, listing invitees, summarizing or leaving a group you are not in. |
| `NOT_A_GROUP` | 252 / 250 | Join, leave or invite on a DM you are in (252); `GET chat/{id}/invitees` on one (250). |
| `ALREADY_MEMBER` | 252 | Joining a group you are in, or inviting one of its members. |
| `ALREADY_INVITED` | 252 | Inviting someone who already has a pending invite to that group. |
| `ALREADY_REQUESTED` | 252 | A DM to someone who has not answered your DM request yet. |
| `REQUEST_CLOSED` | 252 | Accepting, declining or cancelling a request that is already accepted. |
| `TRY_AGAIN` | 252 | `POST users/{id}/dm`: the pair's DM changed three times while the call ran. `PATCH users/password`: another connection of the account changed the password at the same moment. Send it again. |
| `AI_BUSY` | 252 | Too many summaries are being written right now. `retry_after` 5. |
| `AI_QUOTA_USED` | 252 | The server's paid summaries for this UTC day are used up. `retry_after` counts to midnight UTC. |
| `AI_DECLINED` | 252 | The model declined to summarize these messages. |
| `NOT_FOUND` | 253 | No call has this method and URL (also a URL id that is not a UUID); a protected call without credentials; or the chat or user was deleted while the call ran. |
| `CHAT_NOT_FOUND` | 253 | No such chat. Also every DM you are not in, and DM requests that are not accepted yet, so nobody can tell other people's DMs from missing chats. |
| `USER_NOT_FOUND` | 253 | No such user. |
| `REQUEST_NOT_FOUND` | 253 | No such request, it is not yours to answer, or it was declined, cancelled or deleted with its group (those rows are deleted). For `POST chat/{id}/invite/accept` and `.../reject`: you have no pending invite to that group (also when you are already a member). |
| `INTERNAL_ERROR` | 254 | A bug; the server logs it and keeps serving. |
| `DATABASE_UNAVAILABLE` | 254 | PostgreSQL cannot be reached, a statement or lock wait took over 5 s, or a deadlock repeated. |
| `AI_UNAVAILABLE` | 254 | Summaries are off or not set up, or the AI service refused the key or model, rate-limited the server, failed, timed out or could not be reached. Has `retry_after` when waiting can help. |
| `AI_FAILED` | 254 | The AI service rejected the request or answered with nothing usable. Has `retry_after` (5) for an empty answer. |

`REQUEST_NOT_FOUND` and `REQUEST_CLOSED` tell a client why an answer to a
request did not go through: `253 REQUEST_NOT_FOUND` means the request no
longer exists (or was never yours), so remove it from the list;
`252 REQUEST_CLOSED` means it was accepted, so the DM room or group
membership exists.

A 251 reply to a call sent with credentials (every call except `POST users`
and `POST users/login`) means "this session is gone, show the login page".
On those two calls, 251 only reports that the attempt failed (wrong username
or password, or a login that lost a race with another login of the account):
show the error and let the user try again.

---

## 4. Passwords and sessions

### The password travels in clear text

`POST users`, `POST users/login` and `PATCH users/password` each take **one**
request, with the password in the JSON message:

```
POST users         {"username": "alice", "password": "correct horse", "device_id": "..."}
POST users/login   {"username": "alice", "password": "correct horse"}
PATCH users/password  {"new_password": "a new one"}      (while logged in)
```

The socket is a plain TCP socket. There is no TLS, so **anyone who can read
the traffic between the client and the server can read the password**, and so
can anyone who can read a packet capture of it afterwards. The same is true
of the session token, of every message and of every username. This is not a
secure design. It is the design the team chose for the deadline, and it is
written down here rather than dressed up.

An earlier version of this backend used a SCRAM-SHA-256 challenge-response
(two requests per flow, with a Diffie-Hellman exchange for the verifier), so
that the password never crossed the socket. Contract v2 replaced it with the
one-step flow above because the client side was too complicated to finish in
time. The challenge store, the nonces, the proofs, the DH wrapping and their
rate limiters are gone from the code.

### What the server stores

The password is hashed as soon as it arrives and only the hash is stored:

```
salt           = 16 random bytes, new for every account and every change
iterations     = SCRAM_ITERATIONS (default 600,000)
SaltedPassword = PBKDF2-HMAC-SHA256(NFKC(password), salt, iterations)   # 32 bytes
ClientKey      = HMAC(SaltedPassword, "Client Key")
StoredKey      = SHA256(ClientKey)
ServerKey      = HMAC(SaltedPassword, "Server Key")
```

`users.password_hash` holds all four values as one string, in the format
PostgreSQL itself uses for SCRAM passwords:

```
SCRAM-SHA-256$<iterations>:<base64 salt>$<base64 StoredKey>:<base64 ServerKey>
```

which is a salted PBKDF2-HMAC-SHA256 hash. The password itself is never
stored, never logged and never in a reply. The column and the whole of
`socketcord/db/schema.sql` are byte-identical to the challenge-response
version, so accounts made by it keep working and nobody has to drop their
database.

**Checking a password** (`socketcord/scram.py`): parse `salt`, `iterations`
and `StoredKey` out of the row, recompute `StoredKey` from the password that
arrived, and compare with `hmac.compare_digest` (constant time). A wrong
username runs the same PBKDF2 over a throwaway salt and answers the same
`251 INVALID_CREDENTIALS` with the same `detail`, so neither the code, the
text nor the timing says which accounts exist. 600,000 iterations take about
70 ms, which is also the cost of one login to the server.

### The three calls

* **Register** `POST users`. Checks the username rules, that the name is
  free, the per-address register limit (`REGISTER_RATE_PER_MINUTE`) and the
  one-account-per-day rules (section 5); then hashes the password and inserts
  the row, re-checking the name and the limit inside the transaction. The
  reply is `{"user_id", "username"}` with `path` `login/created`, and no
  session: the client logs in next.
* **Log in** `POST users/login`. Counts the attempt (see the throttle below),
  checks the password, then writes a fresh session token. The reply carries
  `user_id`, `session_token`, `expires_at`, `username`, `text_color`,
  `background` and the whole `user`.
* **Change password** `PATCH users/password` (and its older URL
  `PATCH users/profile/password`). Takes `new_password` alone: **no current
  password is asked for**, because the GUI's pop-up has only New and Confirm,
  so holding a valid session token is what authorizes the change. A new salt
  is drawn and the new hash is written in one transaction that also checks
  that the row still has the hash and the session this request read. The
  session token is **not** rotated: the reply is `{"user_id", "changed":
  true}`, the caller keeps its token, and no connection is logged out. The
  old password stops working everywhere at once.

### Login throttle

The attempt is counted before the password is checked, so requests sent in
parallel cannot all slip under the limit. A correct password is not counted
and clears what was counted for that username from that address.

* Real accounts: 5 failures per (username, address) within 5 minutes,
  counted per username, so somebody behind the same NAT cannot lock other
  people out.
* Usernames that do not exist: 50 failures per address within 5 minutes,
  counted per address rather than per name, so made-up names cannot fill the
  table.
* Over either limit: `252 RATE_LIMITED` with `retry_after`. IPv6 addresses
  count per /64.

Usernames are public in SocketCord anyway: the online list and every group's
member list show them, and sign-up says when a name is taken.

### Sessions

**One session per account.** The session token is a random UUID4. The server
hands it out in the login reply. The database keeps one session per account:
`SHA256(token)` in `users.session_token_hash` (never the token itself) and
`users.session_expires_at`. A session lasts `SESSION_TTL_HOURS` (168 hours,
7 days) unless something below ends it first.

* **A login replaces the session.** The new token becomes the account's only
  one, so every connection that still uses the old token is pushed
  `login/revoked` with reason `logged_in_elsewhere` and logged out; its next
  call gets `251 INVALID_SESSION`. This happens even if the login itself then
  loses a race with another login of the account (its reply is
  `251 INVALID_SESSION`). A client must treat this as logged out and must not
  log in again by itself: two clients that do would keep logging each other
  out.
* **Several connections may share the token**, up to
  `MAX_CONNECTIONS_PER_USER` (5) logged-in connections per account, for
  example a second connection for slow AI summaries. Such a connection sends
  the same User ID and Session Token in its headers; its first call logs it
  in. It must never call `users/login`, which would replace the session and
  log the first connection out. A sixth connection's calls get
  `252 TOO_MANY_CONNECTIONS`. A login on a sixth connection is not refused:
  it replaces the session like any login, so the five old connections get
  `logged_in_elsewhere`.
* **A password change keeps the token.** Nothing is revoked and nothing is
  pushed; every connection of the account carries on with the token it has.
* **Logout** (`POST users/logout`) ends the session if it is still the
  current one. The connection that called it is logged out (the socket stays
  open); every other connection that used the token gets reason
  `logged_out`.
* **Expiry**: connections on a session that reached `expires_at` get reason
  `expired` (checked every 5 seconds, and on every request).

Reconnecting with a saved token works: the first call on the new connection
marks the user online again. A connection that is logged in as one account
cannot switch to another by sending the other account's token
(`252 CONNECTION_IN_USE`); log in again on it instead.

Whenever the server logs a connection out by itself, it first pushes
`login/revoked` with `reason`: `logged_in_elsewhere`, `logged_out` (from
another connection), `expired` or, rarely, `too_many_connections` (only when
a login meets a session change at the same moment; [API.md](API.md) section
4.9). The socket stays open, so the user can log in again on it.

### What this does and does not protect

| Protected | Not protected |
| --------- | ------------- |
| A stolen database dump holds no passwords: cracking one hash costs 600,000 PBKDF2-HMAC-SHA256 rounds per guess, against a per-account salt. | **The password on the wire.** It is in the request body in clear text, so anyone who can see the traffic (the same Wi-Fi, a switch, a router, a packet capture) reads it and can log in as that user. |
| A stolen database dump holds no live session tokens: only `SHA256(token)` is stored. | **Everything else on the wire**: session tokens, messages, usernames. A token can be reused by anyone who sees it until it expires. |
| A wrong username and a wrong password are indistinguishable, in code, text and timing. | **Replay and tampering.** There is no proof of freshness and no integrity check beyond CRC-32, which is not a security measure: anyone in the middle can change a request. |
| An account's password cannot be read back out of the server or its replies. | **The client.** Anything that can read the process or the traffic of the GUI sees the password the user typed. |

Wrapping the sockets with Python's `ssl` module would close the right-hand
column, and the one-step flow above would then be reasonable; that is the TLS
layer from Chapter 2. It is not implemented.

---

## 5. One account per device/address per day

* The server sees the TCP source **IP address** of every connection.
* It **cannot see a client's MAC address**. MAC addresses are link-layer, so
  every router rewrites the frame and the server only ever sees the last
  hop. A device therefore has to identify itself. The reference client sends
  `device_id` = the first 32 hex characters of SHA-256 over
  `"socketcord-device:"` followed by the MAC (`uuid.getnode()`) as 12
  lower-case hex digits (`scram.device_fingerprint()`), so the MAC itself
  never leaves the machine.
* There are two independent rules over a 24-hour window
  (`REG_LIMIT_WINDOW_HOURS`):
  * at most `REG_LIMIT_DEVICE_MAX` accounts (default 1) per `device_id`;
  * at most `REG_LIMIT_IP_MAX` accounts (default 1) per source IP. An IPv6
    address counts as its whole /64, since one home or site gets a /64.
    Addresses in `REG_LIMIT_EXEMPT` (CIDRs) skip this rule. The server logs a
    warning at start-up when this rule is off, because then only the device
    id, which clients choose, limits sign-ups.
* Both rules apply to every sign-up. A client can lie about its device id but
  not about its IP, so the IP rule keeps a cap on what one machine can create.
  Breaking either rule gives `252 RATE_LIMITED` with `retry_after`. A value
  of 0 turns that rule off.
* The check and the insert happen atomically, with a PostgreSQL advisory lock
  per key, so parallel sign-ups cannot slip past the limit together.
* Behind NAT many clients share one IP. That covers Docker Desktop/OrbStack
  port forwarding on a Mac (every client shows up as the gateway) and a
  campus network in front of a cloud server. The compose file therefore sets
  `REG_LIMIT_IP_MAX=20` and keeps `REG_LIMIT_DEVICE_MAX=1`. For local
  development, use `--no-reg-limit`.

---

## 6. Server pushes

The server can send a reply packet nobody asked for: status 200, one of the
nine paths below, no `method` and no `rid` (`"method" not in reply` is the
test; the list is `PUSH_PATHS` in `socketcord/protocol.py`, and no push path
is ever a request URL):

`chat/new_message`, `users/status`, `users/request`, `chats/new`,
`chats/group_created`, `chat/members_changed`, `chats/group_deleted`,
`users/profile_changed`, `login/revoked`.

The first four are the ones contract v2 names; the others are the extras the
GUI needs, under the same naming style. When each is sent, to whom, its
fields and an example: [API.md](API.md) section 4.

---

## 7. Calls

The 11 calls of the frontend README and the 17 additional calls, each with
its request fields, reply fields, errors, pushes and an example:
[API.md](API.md) sections 2 and 3. Section 5 of API.md maps the replies onto
the frontend's `chat_page.py`.
