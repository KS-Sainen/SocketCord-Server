# SocketCord Backend

The server side of SocketCord, the term-project chat app for 2110471
Computer Network I. It speaks the RequestCord protocol from the
[frontend repo](https://github.com/KS-Sainen/SocketCord-Frontend) over plain
TCP sockets (`socket` + `selectors`, no HTTP or WebSocket library) and
stores everything in PostgreSQL. One feature reaches outside: the AI chat
summary calls Anthropic's API over HTTPS with the official `anthropic` SDK
(server to Anthropic only, never part of the client↔server protocol).

* Wire format and challenge-response login: [docs/PROTOCOL.md](docs/PROTOCOL.md)
* Every call and push, in full: [docs/API.md](docs/API.md); a one-line list: [below](#calls)
* Database: [below](#database), tables in [socketcord/db/schema.sql](socketcord/db/schema.sql)
* AI summary (rubric Part 3): [below](#ai-chat-summary-part-3)

## Quick start

### With Docker (PostgreSQL + backend)

```bash
cp .env.example .env        # then change the two secrets
docker compose up --build -d
docker compose logs -f backend
```

The backend listens on port 8080 of the host. Clients on other computers
connect to this machine's LAN IP, port 8080. `docker compose down` stops it;
`docker compose down -v` also deletes the database.

Without a `.env`, compose falls back to a development database password that
anyone can read in `docker-compose.yml`. Set `POSTGRES_PASSWORD` before the
first `docker compose up`: PostgreSQL reads it only when its volume is
created, so changing it later needs `docker compose down -v` (deletes the
data) or `ALTER USER socketcord PASSWORD '...'` inside the db container.

### Without Docker (in-memory database, nothing saved)

Needs Python 3.9+ and nothing else:

```bash
python3 -m socketcord --db memory --no-reg-limit
```

To use your own PostgreSQL instead: `pip install -r requirements.txt`, then
`python3 -m socketcord --db postgresql://user:pass@localhost:5432/socketcord`.
The tables are created on start-up. AI summaries written by Claude also need
Python 3.10+ (for the `anthropic` package in requirements.txt) and
`ANTHROPIC_API_KEY` set; without them, summaries come from the offline engine.

### Try it without the GUI

```bash
python3 client/socketcord_client.py --host 127.0.0.1 --port 8080
```

Type `help` for the commands (register, login, online, create, join, send,
dm, accept, ...). Open two terminals, or two computers, to watch pushes
arrive.

### Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

The 262 tests use the in-memory database. About half of them start a real
server on a free port and talk to it over TCP; the rest check the
repository, the wire format and the challenge-response math directly. To run the repository
and server tests against PostgreSQL as well (474 tests in all), point
`SOCKETCORD_TEST_DATABASE_URL` at an empty throwaway database. No test calls
the real AI API: the Claude tests run the real SDK against a local fake of
the Messages API.

> If the folder's path contains `:` (for example `~/school/term:1`), a virtualenv
> created inside it breaks `source .venv/bin/activate`, because `PATH` uses `:`
> as its separator. Call `.venv/bin/python -m ...` directly, or keep the venv
> somewhere else.

## Calls

Every call, at a glance. Request and reply fields, errors, pushes and an
example of each are in [docs/API.md](docs/API.md) (the section number is in
the last column).

| Method | URL | What it does | API.md |
| ------ | --- | ------------ | ------ |
| POST | `users` | register (two-step challenge-response) | 2.1 |
| POST | `users/login` | log in (two steps) | 2.2 |
| PATCH | `users/profile` | change your username and/or name colours (`text_color`, `text_bg_color` or `background`) | 2.3 |
| GET | `users/info` | your account | 2.4 |
| GET | `chats` | the chats you are in | 2.5 |
| GET | `chat/{id}/info` | a chat's details and members | 2.6 |
| GET | `chat/{id}/messages` | message history, with filters | 2.7 |
| POST | `chat/{id}/message` | send a message | 2.8 |
| POST | `users/{id}/dm` | send a DM; the first one opens the room (a request only with `DM_REQUIRE_ACCEPT=true`) | 2.9 |
| POST | `chat/{id}/invite` | invite someone to a group | 2.10 |
| PATCH | `users/profile/password` | change your password (two steps) | 2.11 |
| POST | `users/logout` | end your session | 3.1 |
| GET | `users/online` | who is online | 3.2 |
| GET | `users/search` | find users by name | 3.3 |
| GET | `users/requests` | your pending DM requests and invites | 3.4 |
| POST | `users/requests/{id}/accept`, `/decline`, `/cancel` | answer or take back a request | 3.5–3.7 |
| POST | `chats` | create a group | 3.8 |
| GET | `chats/groups` | every group on the server | 3.9 |
| POST | `chat/{id}/read` | move your read marker | 3.10 |
| POST | `chat/{id}/join` | join a group by yourself | 3.11 |
| POST | `chat/{id}/leave` | leave a group | 3.12 |
| GET or POST | `chat/{id}/summary` | AI summary of a chat | 3.13 |
| GET | `users/{id}/groups` | the groups a user is in | 3.14 |
| GET | `chat/{id}/invitees` | who you can invite to a group (online people and your DM contacts) | 3.15 |
| POST | `chat/{id}/invite/accept` | accept your invite to that group | 3.16 |
| POST | `chat/{id}/invite/reject` | decline your invite to that group | 3.17 |

## How it works

```
 client A ─┐                     ┌──────────────── backend process ─────────────────┐
 client B ─┼── TCP :8080 ──────► │ networking thread (selectors event loop)          │
 client C ─┘   RequestCord       │   listening socket → accept()                     │
               packets           │   connection sockets → recv() → RequestParser     │
                                 │        (handles fragmented + coalesced packets)   │
                                 │   wake-up socketpair ← workers: "reply ready"     │
                                 │            │ complete request      ▲ ReplyCord   │
                                 │            ▼                       │             │
                                 │ worker pool → Router → handler(ctx) → ReplyCord   │
                                 │                  │                                │
                                 │      PresenceHub (who is online, pushes)          │
                                 └──────────────────┼────────────────────────────────┘
                                                    │ psycopg pool, TCP :5432
                                                    ▼
                                               PostgreSQL
```

* **One networking thread owns every socket.** It accepts connections,
  reads bytes, cuts them into packets, and is the only thread that writes
  replies. Packets may arrive split across reads or several in one read.
* **Handlers run in a thread pool**, because they wait on the database. Each
  one returns a `ReplyCord`, and the networking thread sends it back to the
  connection the request came from. Requests from one connection run in
  order, so replies come back in order.
* **Pushes**: when something happens (new message, someone logs in, a DM
  request), handlers ask the `PresenceHub` to send an `event/...` packet to
  the right users' connections.
* **Errors never kill the server.** A bad packet, a crashing handler or a
  database outage becomes an error reply (250/254), and everyone else keeps
  chatting.

## Database

Six tables, following the team's data model
([socketcord/db/schema.sql](socketcord/db/schema.sql)):

```
users ──1:N── messages ──N:1── chats ──┬── groups (Group extends Chat) ──1:N── group_memberships ──N:1── users
  │                                   └── dms    (DM extends Chat)    initiator, receiver ──> users
  └──────────────────────────────────────────────── group_memberships: users N ─ M groups, status pending/accepted
```

| Spec | Column | Notes |
| ---- | ------ | ----- |
| User ID | `users.user_id` | UUID |
| Username | `users.username` | unique without regard to case (R3) |
| Password Hash | `users.password_hash` | the SCRAM-SHA-256 verifier, in PostgreSQL's own format `SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`; the password never reaches the server |
| Session Token | `users.session_token_hash` | SHA-256 of the token (the token itself is never stored), one per account, plus `session_expires_at` |
| User Text Color | `users.text_color` | `#rrggbb`, default `#171717` |
| User Text Background Color | `users.text_bg_color` | `#rrggbb`, default `#f2f2f2` (the frontend's defaults) |
| Chat ID | `chats.chat_id` | `chats.kind` makes a chat exactly one of group / DM |
| Group Name | `groups.group_name` | `groups.group_id` = `chats.chat_id` |
| Group Membership | `group_memberships (user_id, group_id)` | the pair is the primary key; `status` `pending` = invited, `accepted` = member |
| Initiator, Receiver | `dms.initiator`, `dms.receiver` | one DM per pair of users, either direction |
| Message ID | `messages.message_id` | UUIDv7: sorts by time, so paging and unread counts work on it |
| Content, Sender, Timestamp, Chat ID | `content`, `sender_id`, `sent_at`, `chat_id` | `sent_at` is the time inside the UUIDv7 |

Columns beyond the spec, each for a feature that needs it: `kind` in
`chats`, `groups` and `dms` (a chat is exactly one of a group or a DM);
`users.session_expires_at` (sessions expire); `created_at`
timestamps; `users.created_ip` / `created_device` (one account per day);
`groups.description` and `created_by` (chat info, R8 owner);
`dms.status` + `accepted_at` (a DM starts as a request the receiver accepts,
like an invite); read markers `group_memberships.last_read_message_id`,
`dms.initiator_last_read` / `receiver_last_read` (unread counts, the GUI's
NEW line); `group_memberships.membership_id` (names an invite in the API) and
`invited_by`; `messages.kind` (system lines such as "bob joined the group").

Rules the database enforces itself (triggers and constraints): a group is
deleted with its last accepted member, taking its pending invites and
messages; a DM is deleted when both people's accounts are (and a pending DM
request when either is); a member never goes back to "invited"; text and
background colours differ. The server refuses to start on a database with the
schema from before 2026-11-06 and says how to reset it.

## Layout

```
socketcord/
  protocol.py     RequestCord / ReplyCord, method + status codes, stream parsers
  scram.py        challenge-response math + client flows (stdlib only; frontend can copy it)
  server.py       the multi-connection socket server (networking thread)
  app.py          dispatch: frame → route → auth → handler → reply (+ path/rid context)
  router.py       URL patterns, ApiError, request field validation
  presence.py     online users, push to connections
  security.py     challenge store, login throttling, session token hashing
  ai.py           AI summaries: transcript, Claude engine, offline engine, cache and limits
  handlers/       auth.py (register/login/password/logout), users.py, chats.py, summary.py, common.py
  views.py        records → JSON
  config.py       environment variables
  db/             base.py (interface), postgres.py, memory.py, schema.sql
client/socketcord_client.py   reference client + terminal UI
tests/                        protocol, challenge-response, end-to-end
Dockerfile, docker-compose.yml, .env.example
```

## Configuration

Environment variables; the CLI flags `--host --port --db --no-reg-limit
--log-level` override them. With Docker, put them in `.env`: the compose
file forwards all of them to the backend except `DATABASE_URL`,
`SOCKETCORD_HOST` and `SOCKETCORD_PORT`, which are fixed inside the
container (`SOCKETCORD_PORT` in `.env` only sets the host port that is
published).

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `SOCKETCORD_HOST` / `SOCKETCORD_PORT` | `0.0.0.0` / `8080` | where the server listens |
| `DATABASE_URL` | `memory` | `memory` or `postgresql://...` (the password may also come from `PGPASSWORD`) |
| `SOCKETCORD_SECRET` | random | keeps fake login challenges stable across restarts |
| `SOCKETCORD_WORKERS` | `16` | handler threads |
| `SOCKETCORD_MAX_CONNECTIONS` | `1024` | lowered automatically to what the file-descriptor limit allows |
| `MAX_CONNECTIONS_PER_IP` | `64` (`512` in compose) | connections one address may hold |
| `MAX_CONNECTIONS_PER_USER` | `5` | logged-in connections that may share an account's one session token; another connection's calls get `252 TOO_MANY_CONNECTIONS` |
| `SCRAM_ITERATIONS` | `600000` | PBKDF2 rounds for new passwords; clients refuse fewer than 600,000 by default |
| `SESSION_TTL_HOURS` | `168` | session lifetime, counted from the login (or from the last password change, which issues a new token) |
| `CHALLENGE_TTL_SECONDS` | `60` | time to answer a challenge |
| `LOGIN_MAX_FAILURES` / `LOGIN_FAILURE_WINDOW_SECONDS` | `5` / `300` | failed proofs per (username, address) per window |
| `LOGIN_MAX_FAILURES_PER_CLIENT` | `50` | failed proofs per address for usernames that do not exist |
| `CHALLENGE_RATE_PER_MINUTE` / `CHALLENGE_BURST` | `30` / `15` (`120` / `40` in compose) | register / password-change challenges per address (each costs a DH step) |
| `GROUP_CREATE_PER_HOUR` | `20` | new groups per account (burst 5) |
| `REQUEST_RATE_PER_HOUR` | `120` | DM requests + invites per account (burst 20) |
| `MAX_PENDING_REQUESTS` | `100` | pending requests one user may have sent, or received (0 = no cap) |
| `MEMBERSHIP_RATE_PER_MINUTE` | `30` | joins + leaves per account (burst 10) |
| `MESSAGE_RATE_PER_MINUTE` | `60` | messages and DMs per account (burst 30) |
| `PROFILE_RATE_PER_MINUTE` | `10` | colour changes (`PATCH users/profile`) per account (burst 5) |
| `REG_LIMIT_ENABLED` | `true` | one new account per device/address per day |
| `REG_LIMIT_DEVICE_MAX` | `1` | accounts per device id per window (0 = rule off) |
| `REG_LIMIT_IP_MAX` | `1` (`20` in compose, because of NAT) | accounts per source IP per window (0 = rule off) |
| `REG_LIMIT_WINDOW_HOURS` | `24` | the window both rules count over |
| `REG_LIMIT_EXEMPT` | empty | CIDRs the IP rule skips, e.g. `127.0.0.0/8` |
| `DM_REQUIRE_ACCEPT` | `false` | `false`: the first DM opens the DM room at once (what the GUI expects); `true`: the first DM is a request the receiver must accept before the room opens |
| `LOG_LEVEL` | `INFO` | every request is logged with its source address and status |
| `ANTHROPIC_API_KEY` | empty | key for AI summaries (read by the Anthropic SDK; never logged) |
| `AI_PROVIDER` | `auto` | `auto` (Claude if a key is set and the `anthropic` library can be set up, otherwise offline), `claude`, `offline`, `off` |
| `AI_MODEL` | `claude-opus-5-5` | `claude-sonnet-5-5` or `claude-haiku-5-5` cost less |
| `AI_EFFORT` | `low` | how long the model thinks: `low`, `medium`, `high`, `xhigh`, `max`, or `default` (send none); low keeps the wait short |
| `AI_MAX_TOKENS` | `8192` | output cap, thinking included |
| `AI_TIMEOUT_SECONDS` / `AI_MAX_RETRIES` | `30` / `1` | per attempt; the SDK retries 429, 5xx and dropped connections |
| `AI_FALLBACKS` | `true` | if the model declines, the API retries on its recommended fallback model |
| `AI_MAX_CONCURRENT` | `4` | summaries being written at once (at most half the worker threads) |
| `AI_SUMMARY_PER_MINUTE` | `4` | summary requests per account (burst 3) |
| `AI_DAILY_LIMIT` | `100` | paid Claude calls per UTC day, whole server (0 = no cap; the count starts over when the server restarts) |
| `AI_CACHE_SECONDS` | `600` | asking again about the same messages within this time costs nothing |
| `AI_MAX_INPUT_CHARS` | `40000` | transcript size cap; the oldest messages are left out first |

## Course requirements (score sheet) → backend

| Req | Backend side | Call | Test |
| --- | ------------ | ---- | ---- |
| R1 | Listens on all interfaces; clients on other machines connect by IP. Docker for deployment. | – | – |
| R2 | Raw `socket` + `selectors` only | `server.py` | `test_fragmented_and_coalesced_over_tcp` |
| R3 | Usernames unique, case-insensitive (DB unique index) | `POST users` → `USERNAME_TAKEN` | `test_unique_names_case_insensitive` |
| R4 | Online list includes yourself, plus live `event/presence` | `GET users/online` | `test_online_list_includes_self_and_updates` |
| R5 | Every group and every DM pair is its own room (`chats` row; one DM per pair) | `GET chats` | `test_dm_request_accept_and_privacy` |
| R6 | History plus live `event/message` for the chat window | `GET chat/{id}/messages`, `POST chat/{id}/message` | `test_group_lifecycle` |
| R7 | DM to anyone in the list; only the two can read it | `POST users/{id}/dm` | `test_dm_request_accept_and_privacy` |
| R8 | A new group has only its creator | `POST chats` | `test_group_lifecycle` |
| R9 | All groups with member lists | `GET chats/groups` | `test_group_lifecycle` |
| R10 | Join by yourself; an invite adds you only after you accept | `POST chat/{id}/join`, `.../invite` | `test_group_lifecycle`, `test_invite_needs_accept` |
| R11 | Only members can read or send | membership check | `test_group_lifecycle` |

R7: by default (`DM_REQUIRE_ACCEPT=false`) the first DM opens the room at
once, as the GUI expects. With `DM_REQUIRE_ACCEPT=true` the first DM goes
out as a request that carries the message, and the other side must accept
it first.

Part 3, the AI feature: the **AI chat summary** (`GET chat/{id}/summary`,
next section; tests in `tests/test_ai.py`). Beyond R1–R11 the backend also
has the challenge-response login, the per-day registration limit, saved
history in PostgreSQL, unread counts, message and user search, name colours,
and Docker deployment. Every call and push, including the 17 calls that are
not in the frontend README, is documented in [docs/API.md](docs/API.md).

## AI chat summary (Part 3)

The "AI Summary" button in the frontend's chat header. A member asks for a
summary of a chat's latest messages (`mode: "recent"`) or of what they have
not read yet (`mode: "unread"`). The server:

1. checks membership exactly like `GET chat/{id}/messages` (a DM summary is
   only for its two people, a group summary only for members: R7/R11);
2. takes the newest `count` messages (default 50, at most 200) and writes
   them as a transcript, one JSON object per line, inside `<transcript>`;
3. sends one request to Claude (`claude-opus-5-5`, effort `low`) and answers
   with the summary in a normal ReplyCord.

To turn it on, put a key from [console.anthropic.com](https://console.anthropic.com)
into `.env` as `ANTHROPIC_API_KEY=...` and run `docker compose up -d --build`.
The log then says `AI summaries: Claude (claude-opus-5-5, effort low)`.
Without a key the call still works with the **offline engine**: who wrote how
much, and the last lines. It is not AI, and its replies say `engine:
"offline"`, so the GUI can be built and tested before anyone has a key.

What the server does about the usual problems of an AI feature on a chat server:

* **Prompt injection.** Chat text is written by users, so the system prompt
  says the transcript is material to summarize, never instructions. Each
  message is a JSON string with `<` and `>` escaped, so a message cannot close
  the `<transcript>` tag. The summary goes only to the member who asked, and
  the model has no tools: the worst a hostile message can do is spoil that
  member's summary.
* **Privacy.** Only when a member asks, the server sends Anthropic's API the
  messages in the requested window (each with its id, its time to the
  minute, and the sender's username), the group's name (for a DM, the two
  usernames), the asking member's username, and the requested language;
  nothing else leaves the server. The server keeps nothing beyond a
  10-minute cache of summaries in memory.
* **Cost.** Every paid call is counted: 4 requests a minute per account,
  `AI_DAILY_LIMIT` (100) calls per UTC day for the whole server, and a cache, so
  asking again about the same messages is free. The transcript is capped at
  `AI_MAX_INPUT_CHARS`; when it is longer, the oldest messages are left out
  and the reply says how many (`omitted`).
* **Latency.** Writing a summary takes a few seconds and holds one worker
  thread, so at most `AI_MAX_CONCURRENT` (4, never more than half the workers)
  run at once; others get `252 AI_BUSY` straight away instead of queueing. The
  rest of the chat keeps running meanwhile.
* **Refusals.** If the model declines (`stop_reason: "refusal"`), the
  request's `fallbacks: "default"` setting has the API retry on the model
  Anthropic recommends for that kind of refusal, where there is one. A
  request that is still declined answers `252 AI_DECLINED`.

Call details and error codes: [docs/API.md](docs/API.md), section 3.13.

### Protocols used (for the R1 explanation)

* **TCP**: reliable, ordered byte stream. Chat must not lose or reorder
  messages, so TCP rather than UDP. One connection per client stays open, so
  the server can push to it at any time.
* **RequestCord** (application layer): our own framing on top of TCP.
  Size fields delimit packets in the stream, CRC-32 checks each field, and a
  method code + URL picks the call. JSON message bodies.
* **SCRAM-SHA-256-style challenge-response** for passwords, since there is no
  TLS underneath. An ephemeral **Diffie-Hellman** exchange (RFC 3526,
  2048-bit) encrypts the password verifier at sign-up and at password change.
* **TCP options and flow control**: `TCP_NODELAY` is set because chat
  packets are small (Nagle's algorithm would delay them), and `SO_KEEPALIVE`
  notices dead peers. A client that floods requests is slowed down by TCP's
  own flow control: the server stops reading its socket until it catches up.
* **IP**: the source address identifies a client's connection and feeds the
  one-account-per-day rule. MAC addresses never reach the server (link layer,
  rewritten at each hop), so the client sends a hash of its own MAC instead.
* **PostgreSQL wire protocol over TCP** between backend and database, inside
  the Docker network. The database port is not published.
* **HTTPS (HTTP over TLS over TCP)** from the backend to Anthropic's API, only
  for AI summaries. The SDK makes this call; clients never talk to Anthropic,
  and the key stays on the server.

## Connecting the frontend

What the frontend (checked at commit `b43be55`) still needs in order to talk
to this server. [docs/API.md](docs/API.md) section 5 maps every reply onto
the data-in methods of `chat_page.py` and lists the client's duties.

1. **Log in and sign up with the challenge-response flows.** `main.py` sends
   `{"username", "password"}` in the clear, which the server refuses with
   `250 PLAINTEXT_PASSWORD`. Copy `socketcord/scram.py` next to `models.py`
   and do:

   ```python
   from scram import LoginFlow
   self.flow = LoginFlow(username, password)
   self.network.send(RequestCord.from_dict(POST_MTDC, "users/login", self.flow.start()))
   # in handle_reply, when path == "users/login" and status == 200:
   if data["step"] == "challenge":
       self.network.send(RequestCord.from_dict(POST_MTDC, "users/login", self.flow.respond(data)))
   elif data["step"] == "done" and self.flow.verify(data):
       RequestCord.set_auth(data["user_id"], data["session_token"])
   ```

   Sign-up is `RegisterFlow` with URL `users` (`do_signup` sends to
   `users/login` for now). `ChangePasswordFlow` (URL
   `users/profile/password`, method PATCH) works the same way, and its final
   reply carries a new `session_token`: call `RequestCord.set_auth` with it
   at once. Each flow takes about 70–170 ms of PBKDF2 on the client, so a
   worker thread is nicer than the UI thread. A flow object is single-use:
   after any failure (wrong password, expired challenge) make a new one.
   Password-change failures answer 252, not 251, so do not log the user out
   on those.
2. **Route replies on `path` and `method`.** Replies carry `path` = the
   request URL (`users/login`, `users`, `chats`, ...) and `method`; pushes
   use `event/...`. `main.py` calls `.to_lower()`, which Python strings do
   not have (use `.lower()`), and checks for a first segment `login`, which
   never comes: the login replies say `users/login`.
3. **Show the chat page.** `main.py` still has `ChatPage` commented out. Feed
   it as described in docs/API.md section 5. Convert the API's timestamps
   first: `chat_page.py` parses them with `datetime.fromisoformat`, which
   does not accept the trailing `Z` before Python 3.11.
4. **Handle being logged out.** A `251` reply to a call sent with
   credentials (every call except `POST users` and `POST users/login`), or an
   `event/session_revoked` push, means the connection is no longer logged
   in: clear the credentials and show the login page. On sign-up and login,
   `251` only means that attempt failed (wrong password, expired challenge):
   show the error. One session per account: a login on another device ends
   this one (`logged_in_elsewhere`), so never log in again automatically.
5. **DMs and invites.** By default (`DM_REQUIRE_ACCEPT=false`) a first DM
   opens the room at once. An invite waits to be accepted: answer it with
   `POST chat/{id}/invite/accept` or `.../invite/reject`, and keep the list
   current with `GET users/requests` and `event/request`.
6. **Send read markers.** `POST chat/{id}/read` after showing a chat;
   otherwise unread counts and the NEW line never clear.
7. **Wait longer for the AI summary.** `GET chat/{id}/summary` takes a few
   seconds, at worst about a minute: use a 90 s reply timeout (docs/API.md
   section 3.13).
8. `login_page.py` shows "Signing up..." when LOG IN is clicked and
   "Logging in..." for SIGN UP.

## Known limits

* No TLS: chat text and session tokens are readable on the wire, and someone
  who can modify traffic during sign-up could swap the DH value (see the
  table in docs/PROTOCOL.md §4). Passwords and verifiers never are.
* Challenges, login throttling and presence live in memory, which is right
  for one server process. Running several backend processes would need them
  moved into a shared store.
* Requests from one connection run in order, so while a summary is being
  written, that connection's next requests wait (pushes still arrive). Give
  `GET chat/{id}/summary` a long reply timeout (the reference client waits
  90 s). A GUI that must stay responsive can open a second TCP connection
  and send the summary there with the same User ID and Session Token in the
  header. Never call `users/login` on that second connection: an account
  has one session, so a login there would end the first connection's
  session (`event/session_revoked`, reason `logged_in_elsewhere`).
* One session per account: logging in on a second device logs the first one
  out. Several connections can still share the session (up to
  `MAX_CONNECTIONS_PER_USER`), as described above.
* The chat itself uses only sockets. The AI summary's outbound call to
  Anthropic goes through the `anthropic` SDK over HTTPS; it is not
  client↔server traffic. `AI_PROVIDER=offline` removes that call entirely.
