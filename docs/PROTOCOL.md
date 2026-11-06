# SocketCord protocol, as implemented by the backend

This document describes what the backend expects and what it sends back.
The packet layout is the one in the frontend's README and `models.py`, and
it is unchanged. The additions are a few fields in the JSON message, the
challenge-response flows, server pushes, and some new calls. Section 2
lists every change. Every call and push, field by field, is in
[API.md](API.md).

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
| `path`   | always                   | The URL of the request this reply answers, without leading/trailing `/` (e.g. `users/login`). Pushes use `event/<name>`. This is the "context" that lets the client route a reply. |
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
length is checked, except the `content` of `POST chat/{chat_id}/message`,
which is stored exactly as sent.

---

## 2. What changed compared with the frontend README

The binary packet layout is unchanged. Everything else that differs:

1. **Reply context.** Every reply has `path` (the request URL) and `method`
   (`"POST"`, `"GET"`, `"PATCH"`), and echoes `rid` when the request carried
   one.
2. **Server pushes.** The server can send a reply packet nobody asked for,
   with status 200 and `path = event/<name>` ([API.md](API.md) section 4).
   This delivers new messages, presence changes, requests, new DM rooms,
   group changes and logouts as they happen.
3. **Challenge-response.** `POST users`, `POST users/login` and
   `PATCH users/profile/password` each take two requests (section 4). A
   message containing `password`, `old_password` or `new_password` is refused
   with `250 PLAINTEXT_PASSWORD`.
4. **Credentials.** A protected call with no User ID/Session Token (all
   zeros) gets `253 Not Found`, as the README says. A call with credentials
   that are wrong, expired or revoked gets `251 Unauthorized`, so the client
   knows to show the login page again.
5. **Data model.** The database is the team's six tables (`users`, `chats`,
   `groups`, `dms`, `group_memberships`, `messages`; see
   `socketcord/db/schema.sql`), and the API follows it:
   * Users have a username and two colours, `text_color` and
     `text_bg_color` (`"#rrggbb"`, default `"#171717"` on `"#f2f2f2"`, the
     frontend's own defaults). There is no display name and no bio.
     `PATCH users/profile` sets the colours, and every user object in a
     reply carries them, so the GUI can paint names without extra calls.
   * Every id is a UUID string. Message ids are UUIDv7: they grow with time,
     so `before_id`, `after_id` and `last_read_message_id` page and count by
     them, and two ids compare correctly as lower-case strings.
   * A DM starts as a **DM request**: a pending `dms` row whose
     `request_id` is the DM's chat id. A group invite is a pending
     `group_memberships` row whose `request_id` is its membership id. The
     receiver accepts or declines, the sender may cancel. Declined and
     cancelled requests are deleted, so answering one again gives
     `253 REQUEST_NOT_FOUND`; answering an accepted one gives
     `252 REQUEST_CLOSED`.
6. **One session per account.** A login replaces the account's session and
   logs out every connection that used the old token
   (`event/session_revoked`, reason `logged_in_elsewhere`). A password change
   replaces the token too, and its final reply carries the new one (section
   4, Sessions).
7. **Reply shapes.**
   * Every reply that adds a chat to your list (`POST chats`,
     `POST chat/{id}/join`, accepting a request, `POST users/{id}/dm`, and
     `event/chat`) carries it in the same shape as an item of `GET chats`,
     with `is_dm`, `unread` and `last_activity`.
   * `POST users/{id}/dm` always answers with `status` (`"sent"` or
     `"pending"`), `chat_id`, `chat`, `messages` and `request`.
   * `GET chat/{id}/messages` also returns `last_read_message_id` and
     `first_unread_message_id` (where the GUI draws its NEW line).
   * `GET chat/{id}/info` shows a group's pending invites (`invited`) to its
     members. The `info` in the replies of `POST chat/{id}/join` and of
     accepting an invite is that same object.
   * The `GET chat/{id}/messages` filters `count`, `content` and `time` work
     as the README says, plus `before_id` and `after_id`.
8. **New calls**, beyond the README's 11: `POST users/logout`,
   `GET users/online`, `GET users/search`, `GET users/requests`,
   `POST users/requests/{id}/accept|decline|cancel`, `POST chats`,
   `GET chats/groups`, `POST chat/{id}/read`, `POST chat/{id}/join`,
   `POST chat/{id}/leave`, `GET chat/{id}/summary`. All are documented in
   full in [API.md](API.md) section 3.

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
| `CONNECTION_CLOSED` | 250 | Register, login or password change, step 1: the connection closed while the challenge was being made, so none was kept. Nobody reads this reply. |
| `PLAINTEXT_PASSWORD` | 250 | Register, login or password change: the message contains `password`, `old_password` or `new_password`. Has `field`. |
| `INVALID_USERNAME` | 250 | Register or login: the username breaks the rules. Has `field`. |
| `INVALID_TARGET` | 250 | A DM to yourself, or inviting yourself. |
| `INVALID_PROOF` | 251 / 250 | The encrypted verifier failed its integrity check: 251 at registration, 250 at a password change. |
| `INVALID_SESSION` | 251 | The session token is unknown, ended (logout, a newer login, a password change), expired, or not this User ID's; also a login, or a password change, whose new session another login of the same account replaced at the same moment. |
| `INVALID_CREDENTIALS` | 251 / 252 | Login (251): wrong password or unknown username. Password change (252): the current password is wrong. |
| `CHALLENGE_INVALID` | 251 / 252 | The challenge is unknown, used, expired, or from another connection (or, for a password change, another session); or the password changed between the two steps. 251 for login and register, 252 for a password change. |
| `USERNAME_TAKEN` | 252 | Register: the name is taken (any letter case). Has `field`. |
| `RATE_LIMITED` | 252 | A per-account limit ([API.md](API.md) section 1.7), or the one-account-per-day rule at registration. Has `retry_after`. |
| `TOO_MANY_ATTEMPTS` | 252 | Too many failed passwords for a username from one address, or too many register/password-change challenges from one address. Has `retry_after`. |
| `LIMIT_REACHED` | 252 | A DM request or invite would give the sender more than `MAX_PENDING_REQUESTS` pending requests sent, or the receiver more than that received. |
| `CONNECTION_IN_USE` | 252 | This connection is logged in as another account. |
| `TOO_MANY_CONNECTIONS` | 252 | The account already has `MAX_CONNECTIONS_PER_USER` logged-in connections, and this connection tried to use its session. (A login is never refused this way: it replaces the session.) |
| `NOT_A_MEMBER` | 252 | Reading, writing, marking as read, inviting, summarizing or leaving a group you are not in. |
| `NOT_A_GROUP` | 252 | Join, leave or invite on a DM you are in. |
| `ALREADY_MEMBER` | 252 | Joining a group you are in, or inviting one of its members. |
| `ALREADY_INVITED` | 252 | Inviting someone who already has a pending invite to that group. |
| `ALREADY_REQUESTED` | 252 | A DM to someone who has not answered your DM request yet. |
| `REQUEST_CLOSED` | 252 | Accepting, declining or cancelling a request that is already accepted. |
| `TRY_AGAIN` | 252 | `POST users/{id}/dm`: the pair's DM changed three times while the call ran. Send it again. |
| `AI_BUSY` | 252 | Too many summaries are being written right now. `retry_after` 5. |
| `AI_QUOTA_USED` | 252 | The server's paid summaries for this UTC day are used up. `retry_after` counts to midnight UTC. |
| `AI_DECLINED` | 252 | The model declined to summarize these messages. |
| `NOT_FOUND` | 253 | No call has this method and URL (also a URL id that is not a UUID); a protected call without credentials; or the chat or user was deleted while the call ran. |
| `CHAT_NOT_FOUND` | 253 | No such chat. Also every DM you are not in, and DM requests that are not accepted yet, so nobody can tell other people's DMs from missing chats. |
| `USER_NOT_FOUND` | 253 | No such user. |
| `REQUEST_NOT_FOUND` | 253 | No such request, it is not yours to answer, or it was declined, cancelled or deleted with its group (those rows are deleted). |
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
On those two calls, 251 only reports that the attempt failed (wrong password,
expired challenge, a login that lost a race with another login of the
account). Password-change mistakes (wrong current password, expired
challenge) answer 252 instead, because the session is still fine.

---

## 4. Challenge-response authentication

The rule from the README is "never send a plaintext password". The flows
below use the key math of SCRAM-SHA-256 (RFC 5802 / RFC 7677, the same
method PostgreSQL uses for its own logins). Registration and password change
add an ephemeral Diffie-Hellman exchange, so the new verifier travels
encrypted. The client side is `socketcord/scram.py`. It uses only the
standard library, so the frontend can copy the file and call `LoginFlow`,
`RegisterFlow` and `ChangePasswordFlow`. `client/socketcord_client.py` shows
them in use.

### Keys

```
SaltedPassword  = PBKDF2-HMAC-SHA256(NFKC(password), salt, iterations)   # 32 bytes
ClientKey       = HMAC(SaltedPassword, "Client Key")
StoredKey       = SHA256(ClientKey)
ServerKey       = HMAC(SaltedPassword, "Server Key")
ClientSignature = HMAC(StoredKey, AuthMessage)
ClientProof     = ClientKey XOR ClientSignature
ServerSignature = HMAC(ServerKey, AuthMessage)
```

The server stores only `salt, iterations, StoredKey, ServerKey`. It checks a
proof by recovering `ClientKey = ClientProof XOR HMAC(StoredKey, AuthMessage)`
and testing `SHA256(ClientKey) == StoredKey`. This only works for someone who
knows StoredKey, so StoredKey must never be visible on the wire.

### Verifier transfer (register and change password)

```
group      RFC 3526 group 14: 2048-bit safe prime p, generator 2 ("dh_group": "modp2048")
server     b = random 256-bit exponent, B = 2^b mod p      (sent in the challenge as "dh_public")
client     a = random 256-bit exponent, A = 2^a mod p      (sent in the answer as "dh_public")
both       Z = A^b = B^a mod p, as 256 big-endian bytes; public values must be in [2, p-2]
           OKM = HKDF-SHA256(IKM=Z, salt="socketcord-verifier-wrap", info=AuthMessage, 96 bytes)
           pad = OKM[0:64], mac_key = OKM[64:96]
client     wrapped_verifier = (StoredKey || ServerKey) XOR pad
           Full = AuthMessage + "," + base64(wrapped_verifier)
           wrap_tag = HMAC(mac_key, Full)
```

The client's key pair is fresh for every exchange, so every shared secret,
and therefore every pad, is used exactly once. The server reuses its own key
pair for at most 60 s or 200 challenges, because generating one costs about
3 ms of CPU and a fresh one per request would let anyone stall the server.
`Full` is what the tag, the password-change proof and the ServerSignature
cover.

### Shared rules

* A challenge is valid for 60 s, can be used once, and must be answered **on
  the same TCP connection** that asked for it.
* The client creates `client_nonce` (18 random bytes, base64). The server
  creates `server_nonce`. Because both are fresh, a sniffed proof cannot be
  replayed.
* The client must refuse any `dh_group` other than `modp2048`, and any
  `iterations` above 10,000,000 or below its own floor. The flows'
  `min_iterations` defaults to 600,000, the server default, so a fake server
  cannot ask for cheap PBKDF2 and get an easily cracked proof. Do not set
  the server's `SCRAM_ITERATIONS` below what clients accept. 600,000
  iterations take about 70 ms on a laptop.
* Each flow object is single-use: `respond()` wipes the password from it and
  refuses to run twice. After any failure, create a new flow.
* Register and password-change challenges cost the server a DH step, so they
  are rate-limited per client address (`CHALLENGE_RATE_PER_MINUTE`, burst
  `CHALLENGE_BURST`), checked before the request's fields: only the
  plaintext-password check (and, for a password change, the session check)
  comes first. Going over the limit answers `252 TOO_MANY_ATTEMPTS`.
* The client should check `server_signature` in the final reply. If it is
  wrong, the server does not hold this account's verifier, so treat it as a
  fake server.
* Message 2 is told apart from message 1 by the presence of `challenge_id`.
* Fields that hold base64 are used in AuthMessage exactly as sent.
* The username in the login and register AuthMessage is the normalized form,
  trimmed and lower-cased (`scram.canonical_username`), whatever case the
  user typed: an account registered as `Alice` logs in with
  `socketcord-login-v1,alice,...`. `<user_id>` in the password AuthMessage is
  the account's UUID in lower case with hyphens, as replies write it. The
  flows in `scram.py` do both.

### Login: `POST users/login` twice

```
client                                                     server
  | {"username": "alice", "client_nonce": cN}                |
  |--------------------------------------------------------->|  look up alice
  |   {"step":"challenge","challenge_id":id,"salt":s,        |  (unknown name: fake but
  |    "iterations":i,"server_nonce":sN,"expires_in":60}     |   consistent salt)
  |<---------------------------------------------------------|
  | derive keys from password+s+i                            |
  | AuthMessage = "socketcord-login-v1,alice,cN,sN,s,i,id"   |
  | {"challenge_id":id,"client_proof":p}                     |
  |--------------------------------------------------------->|  verify p against StoredKey
  |   {"step":"done","user_id":u,"session_token":t,          |  replace the account's session
  |    "expires_at":..., "user":{...},"server_signature":g}  |  (only if the password did not
  |<---------------------------------------------------------|   change meanwhile)
  | check g == HMAC(ServerKey, AuthMessage)                  |
  | from now on: put u and t in every request header         |
```

A wrong password and an unknown username both end in `251 INVALID_CREDENTIALS`.
Every answer counts as an attempt before its proof is checked, so answers
sent in parallel cannot slip past the limit. A correct answer is not
counted, and it clears the failures already counted for that username from
that address (a successful password change does the same). The limit is 5
failed attempts per (username, address) within 5 minutes, counted for that
username only, so someone behind the same NAT cannot lock other people out.
Failed password changes of the account count toward the same limit.
Usernames that do not exist are counted per address instead: 50 failures
within 5 minutes. Over the limit, new challenges and answers to old ones get
`252 TOO_MANY_ATTEMPTS` with `retry_after`. IPv6 addresses count per /64.

Usernames are public in SocketCord: the online list and every group's member
list show them, and sign-up says when a name is taken. The fake challenge
only keeps login replies uniform; hiding which names exist is not a goal.

### Register: `POST users` twice

```
1. client -> {"username": "alice", "client_nonce": cN, "device_id": "<optional, see section 5>"}
   server -> {"step":"challenge","challenge_id":id,"salt":s,"iterations":i,"server_nonce":sN,
              "dh_group":"modp2048","dh_public":B,"expires_in":60}
             (the server picks the salt; the name is checked for availability and the
              one-account-per-day rule is checked here already)
2. client derives StoredKey/ServerKey from password+s+i and its DH pair (a, A), then
   AuthMessage = "socketcord-register-v2,alice,cN,sN,s,i,id,modp2048,B,A"
   client -> {"challenge_id":id,"dh_public":A,"wrapped_verifier":w,"wrap_tag":t}
   server unwraps the verifier (tag must match), re-checks the name and the limit
          atomically, stores the verifier
   server -> {"step":"done","user_id":u,"user":{...},"server_signature":HMAC(ServerKey, Full)}
3. client checks the signature, then logs in normally.
```

### Change password: `PATCH users/profile/password` twice, while logged in

```
1. client -> {"client_nonce": cN}
   server -> {"step":"challenge","challenge_id":id,"salt":s,"iterations":i,"server_nonce":sN,
              "new_salt":s2,"new_iterations":i2,"dh_group":"modp2048","dh_public":B,"expires_in":60}
2. AuthMessage = "socketcord-password-v2,<user_id>,cN,sN,s,i,id,s2,i2,modp2048,B,A"
   client -> {"challenge_id":id,"dh_public":A,"wrapped_verifier":w (new keys),"wrap_tag":t,
              "client_proof": proof with the OLD keys over Full}
   server -> {"step":"done","user_id":u,"session_token":t2,"expires_at":...,
              "logged_out_connections":n,"server_signature":HMAC(old ServerKey, Full)}
3. client checks the signature, then puts t2 in the header of every request from now on.
```

The old-password proof covers both DH values and the ciphertext. Someone in
the middle who swaps either one breaks the proof, so a password change is
safe even against an active attacker who does not know the old password.
The new verifier and a new session token are written in one database
transaction, and only if the account still has the password and the session
the challenge was issued for, so either both change or neither does. The
reply carries the new token `t2` and its `expires_at` (a full
`SESSION_TTL_HOURS` from now); `logged_out_connections` counts the other
connections that used the old token. Those get `event/session_revoked`
(`password_changed`), and their next call gets `251`. The connection that
made the change keeps working (see Sessions), unless a login with the new
password replaced the new session at that same moment: the new password
then stands, but the reply is `251 INVALID_SESSION`.

### Sessions

**One session per account.** The session token is a random UUID4. The
server hands it out in the final reply of a login, and a new one in the
final reply of a password change. The database keeps one session per
account: `SHA256(token)` in `users.session_token_hash` (never the token
itself) and `users.session_expires_at`. A session lasts `SESSION_TTL_HOURS`
(168 hours, 7 days) unless something below ends it first.

* **A login replaces the session.** The new token becomes the account's only
  one, so every connection that still uses the old token is pushed
  `event/session_revoked` with reason `logged_in_elsewhere` and logged out;
  its next call gets `251 INVALID_SESSION`. This happens even if the login
  itself then loses a race with another login of the account (its reply is
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
* **A password change rotates the token** (see "Change password" above).
  The connection that made the change moves to the new token at once. Its
  old token keeps working **on that connection only, for 30 seconds**, so
  requests it sent before reading the reply still go through; everywhere
  else the old token stops at once, with reason `password_changed`.
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
`event/session_revoked` with `reason`: `logged_in_elsewhere`, `logged_out`
(from another connection), `password_changed`, `expired` or, rarely,
`too_many_connections` (only when a login meets a session change at the same
moment; [API.md](API.md) section 4.9). The socket stays open, so the user can
log in again on it.

### What this does and does not protect

| Protected | Not protected (would need TLS) |
| --------- | ------------------------------ |
| The password never crosses the socket, in any form. | Chat contents and session tokens travel unencrypted, so someone on the path can read them or reuse a token until it expires. |
| The verifier never crosses the socket in the clear, so a recording of a registration and later logins does not give anyone ClientKey. | A recorded login lets someone test password guesses offline. Each guess costs 600,000 PBKDF2 rounds. |
| A captured login proof cannot be replayed. | An attacker who can modify traffic during **registration** can swap in their own DH value, because there is no shared secret yet. |
| A stolen database holds no passwords and no live session tokens. | |
| The client can tell a fake server from the real one, and a password change cannot be tampered with in transit. | |

Wrapping the sockets with Python's `ssl` module would close the right-hand
column. That is the TLS layer from Chapter 2.

---

## 5. One account per device/address per day

* The server sees the TCP source **IP address** of every connection.
* It **cannot see a client's MAC address**. MAC addresses are link-layer, so
  every router rewrites the frame and the server only ever sees the last
  hop. A device therefore has to identify itself. `RegisterFlow` sends
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

The server can send a reply packet nobody asked for: status 200,
`path = event/<name>`, no `method` and no `rid`. There are nine:
`event/message`, `event/presence`, `event/request`, `event/chat`,
`event/group_created`, `event/group_updated`, `event/group_deleted`,
`event/profile` and `event/session_revoked`. When each is sent, to whom,
its fields and an example: [API.md](API.md) section 4.

---

## 7. Calls

The 11 calls of the frontend README and the 13 additional calls, each with
its request fields, reply fields, errors, pushes and an example:
[API.md](API.md) sections 2 and 3. Section 5 of API.md maps the replies onto
the frontend's `chat_page.py`.
