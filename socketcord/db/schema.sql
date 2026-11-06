-- SocketCord schema for PostgreSQL 13+.
-- The backend runs this file on every start-up; every statement is idempotent.
--
-- The team's data model:
--   User 1 ─ N Message (as sender)          Chat 1 ─ N Message
--   Group  extends Chat (+ Group Name)      DM extends Chat (+ Initiator, Receiver)
--   User N ─ M Group  through Group Membership (Status 'pending' = invited, 'accepted' = member)
--   "DM deleted IFF every member is deleted"
--
-- Spec field                  -> column
--   User ID                     users.user_id
--   Username                    users.username
--   Password Hash               users.password_hash   (the SCRAM-SHA-256 verifier; see below)
--   Session Token               users.session_token_hash (sha256 of the token; the token is never stored)
--   User Text Color             users.text_color      ('#rrggbb')
--   User Text Background Color  users.text_bg_color   ('#rrggbb')
--   Chat ID                     chats.chat_id
--   Group Name                  groups.group_name     (groups.group_id = chats.chat_id)
--   Initiator, Receiver         dms.initiator, dms.receiver (dms.dm_id = chats.chat_id)
--   Group Membership            group_memberships (user_id, group_id) unique, status
--   Message ID, Content,        messages.message_id (UUIDv7), content,
--   Sender, Timestamp, Chat ID  sender_id, sent_at, chat_id
-- Columns not in the spec are marked "extra" with the feature that needs them.

-- ---------------------------------------------------------------- refuse the old schema
-- The schema before 2026-11-06 (sessions, chat_members, chat_requests, users.scram_salt)
-- cannot be upgraded in place. Stop before creating anything next to it.
DO $$
BEGIN
    IF to_regclass('public.sessions') IS NOT NULL
       OR to_regclass('public.chat_members') IS NOT NULL
       OR to_regclass('public.chat_requests') IS NOT NULL
       OR EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'scram_salt') THEN
        RAISE EXCEPTION 'this database still has the old SocketCord schema'
            USING HINT = 'Reset it (this deletes all its data): docker compose down -v, then docker compose up -d --build';
    END IF;
END
$$;

-- ---------------------------------------------------------------- User
CREATE TABLE IF NOT EXISTS users (
    user_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username           varchar(32) NOT NULL CHECK (username ~ '^[A-Za-z0-9_.-]{3,32}$'),
    -- The challenge-response verifier in PostgreSQL's own SCRAM format:
    -- SCRAM-SHA-256$<iterations>:<16-byte salt>$<StoredKey>:<ServerKey> (base64).
    -- The password itself never reaches the server.
    password_hash      text NOT NULL CHECK (
        password_hash ~ '^SCRAM-SHA-256\$[1-9][0-9]{3,7}:[A-Za-z0-9+/]{22}==\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=$'
        AND split_part(split_part(password_hash, '$', 2), ':', 1)::bigint BETWEEN 4096 AND 10000000),
    -- One session per account: a new login replaces it.
    session_token_hash bytea UNIQUE CHECK (octet_length(session_token_hash) = 32),
    session_expires_at timestamptz,                                          -- extra: sessions expire
    text_color         varchar(7) NOT NULL DEFAULT '#171717' CHECK (text_color ~ '^#[0-9a-f]{6}$'),
    text_bg_color      varchar(7) NOT NULL DEFAULT '#f2f2f2' CHECK (text_bg_color ~ '^#[0-9a-f]{6}$'),
    created_at         timestamptz NOT NULL DEFAULT now(),                   -- extra
    created_ip         inet,                                                 -- extra: one account per day
    created_device     varchar(128),                                         -- extra: one account per day
    CHECK ((session_token_hash IS NULL) = (session_expires_at IS NULL)),
    CHECK (text_color <> text_bg_color)
);
-- R3: every client has a unique name (case-insensitive).
CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_key ON users (lower(username));
CREATE INDEX IF NOT EXISTS users_created_ip_idx     ON users (created_ip, created_at);
CREATE INDEX IF NOT EXISTS users_created_device_idx ON users (created_device, created_at);

-- ---------------------------------------------------------------- Chat
-- `kind` (extra) makes "extends" disjoint: a chat is exactly one of a group or a DM.
CREATE TABLE IF NOT EXISTS chats (
    chat_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       varchar(5) NOT NULL CHECK (kind IN ('group', 'dm')),
    created_at timestamptz NOT NULL DEFAULT now(),                           -- extra
    UNIQUE (chat_id, kind)
);

-- ---------------------------------------------------------------- Group extends Chat
CREATE TABLE IF NOT EXISTS groups (
    group_id    uuid PRIMARY KEY,
    kind        varchar(5) NOT NULL DEFAULT 'group' CHECK (kind = 'group'),
    group_name  varchar(64) NOT NULL CHECK (btrim(group_name) <> ''),
    description varchar(500) NOT NULL DEFAULT '',                           -- extra: chat info shows it
    created_by  uuid REFERENCES users (user_id) ON DELETE SET NULL,          -- extra: the owner (R8)
    FOREIGN KEY (group_id, kind) REFERENCES chats (chat_id, kind) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS groups_created_by_idx ON groups (created_by);

-- ---------------------------------------------------------------- DM extends Chat
-- One DM per pair of users. It starts as a request ('pending') that the receiver
-- accepts, like a group invite ("other side must accept").
CREATE TABLE IF NOT EXISTS dms (
    dm_id               uuid PRIMARY KEY,
    kind                varchar(5) NOT NULL DEFAULT 'dm' CHECK (kind = 'dm'),
    initiator           uuid REFERENCES users (user_id) ON DELETE SET NULL,
    receiver            uuid REFERENCES users (user_id) ON DELETE SET NULL,
    status              varchar(8) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted')),           -- extra: DM requests
    accepted_at         timestamptz,                                         -- extra
    -- extra: read markers for unread counts. The nil UUID means "nothing read";
    -- it sorts before every message id.
    initiator_last_read uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    receiver_last_read  uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    CHECK (initiator <> receiver),
    CHECK ((status = 'accepted') = (accepted_at IS NOT NULL)),
    FOREIGN KEY (dm_id, kind) REFERENCES chats (chat_id, kind) ON DELETE CASCADE
);
-- LEAST/GREATEST skip NULLs, so a DM that lost a deleted user stays out of the pair rule.
CREATE UNIQUE INDEX IF NOT EXISTS dms_one_per_pair ON dms (LEAST(initiator, receiver), GREATEST(initiator, receiver))
    WHERE initiator IS NOT NULL AND receiver IS NOT NULL;
CREATE INDEX IF NOT EXISTS dms_initiator_idx ON dms (initiator);
CREATE INDEX IF NOT EXISTS dms_receiver_idx  ON dms (receiver);

-- ---------------------------------------------------------------- Group Membership
CREATE TABLE IF NOT EXISTS group_memberships (
    user_id              uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    group_id             uuid NOT NULL REFERENCES groups (group_id) ON DELETE CASCADE,
    status               varchar(8) NOT NULL CHECK (status IN ('pending', 'accepted')),
    membership_id        uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),     -- extra: names an invite in the API
    invited_by           uuid REFERENCES users (user_id) ON DELETE SET NULL, -- extra: who invited
    created_at           timestamptz NOT NULL DEFAULT now(),                 -- extra: invited or joined
    joined_at            timestamptz,                                        -- extra: became 'accepted'
    last_read_message_id uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',  -- extra: unread counts
    PRIMARY KEY (user_id, group_id),                                         -- each pair is unique
    CHECK ((status = 'accepted') = (joined_at IS NOT NULL)),
    CHECK (invited_by IS DISTINCT FROM user_id)
);
CREATE INDEX IF NOT EXISTS group_memberships_group_idx      ON group_memberships (group_id, status);
CREATE INDEX IF NOT EXISTS group_memberships_invited_by_idx ON group_memberships (invited_by);

-- ---------------------------------------------------------------- Message
CREATE TABLE IF NOT EXISTS messages (
    -- UUIDv7, made by the server under the chat's lock: sorts by time, never repeats.
    message_id uuid PRIMARY KEY CHECK (substr(message_id::text, 15, 1) = '7'),
    chat_id    uuid NOT NULL REFERENCES chats (chat_id) ON DELETE CASCADE,
    sender_id  uuid REFERENCES users (user_id) ON DELETE SET NULL,          -- Sender; NULL for system notices
    kind       varchar(8) NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'system')),  -- extra
    content    text NOT NULL CHECK (char_length(content) BETWEEN 1 AND 4000),
    -- Timestamp: the id's own 48-bit millisecond clock, so time order and id order always agree.
    sent_at    timestamptz GENERATED ALWAYS AS (
        to_timestamp((('x' || substr(replace(message_id::text, '-', ''), 1, 12))::bit(48)::bigint) / 1000.0)) STORED,
    CHECK (kind = 'text' OR sender_id IS NULL)
);
CREATE INDEX IF NOT EXISTS messages_chat_idx   ON messages (chat_id, message_id DESC);
CREATE INDEX IF NOT EXISTS messages_sender_idx ON messages (sender_id);

-- ---------------------------------------------------------------- triggers
-- A group is deleted when its last accepted member leaves; pending invites go with it.
-- The group row is locked first, so two last members leaving at once, or a leave
-- racing a join, cannot both miss each other.
CREATE OR REPLACE FUNCTION socketcord_delete_empty_group() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'accepted' THEN
        RETURN NULL;
    END IF;
    PERFORM 1 FROM groups WHERE group_id = OLD.group_id FOR UPDATE;
    IF NOT EXISTS (SELECT 1 FROM group_memberships m
                    WHERE m.group_id = OLD.group_id AND m.status = 'accepted') THEN
        DELETE FROM chats WHERE chat_id = OLD.group_id;
    END IF;
    RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS group_memberships_delete_empty_group ON group_memberships;
CREATE TRIGGER group_memberships_delete_empty_group
    AFTER DELETE ON group_memberships
    FOR EACH ROW EXECUTE FUNCTION socketcord_delete_empty_group();

-- A member never goes back to "invited" (that could leave a group with nobody in it).
CREATE OR REPLACE FUNCTION socketcord_no_demotion() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'group membership cannot go from accepted back to pending';
END
$$;
DROP TRIGGER IF EXISTS group_memberships_no_demotion ON group_memberships;
CREATE TRIGGER group_memberships_no_demotion
    BEFORE UPDATE OF status ON group_memberships
    FOR EACH ROW WHEN (OLD.status = 'accepted' AND NEW.status = 'pending')
    EXECUTE FUNCTION socketcord_no_demotion();

-- A DM is made between two existing users; NULL sides only ever come from ON DELETE SET NULL.
CREATE OR REPLACE FUNCTION socketcord_dm_insert_check() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.initiator IS NULL OR NEW.receiver IS NULL THEN
        RAISE EXCEPTION 'a DM needs both an initiator and a receiver';
    END IF;
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS dms_insert_check ON dms;
CREATE TRIGGER dms_insert_check BEFORE INSERT ON dms
    FOR EACH ROW EXECUTE FUNCTION socketcord_dm_insert_check();

-- "DM deleted IFF every member is deleted". A pending DM (a request) that lost
-- either side is dead too. (Declining or cancelling a request also deletes it:
-- until it is accepted, a DM has no members.)
CREATE OR REPLACE FUNCTION socketcord_delete_orphan_dm() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM chats WHERE chat_id = NEW.dm_id;
    RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS dms_delete_orphan ON dms;
CREATE TRIGGER dms_delete_orphan
    AFTER UPDATE OF initiator, receiver ON dms
    FOR EACH ROW WHEN ((NEW.initiator IS NULL AND NEW.receiver IS NULL)
                       OR (NEW.status = 'pending' AND (NEW.initiator IS NULL OR NEW.receiver IS NULL)))
    EXECUTE FUNCTION socketcord_delete_orphan_dm();

-- Deleting a group or DM row deletes its chat (and with it the messages), and a
-- chat must have one of them by the end of its transaction.
CREATE OR REPLACE FUNCTION socketcord_delete_parent_of_group() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN DELETE FROM chats WHERE chat_id = OLD.group_id; RETURN NULL; END $$;
CREATE OR REPLACE FUNCTION socketcord_delete_parent_of_dm() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN DELETE FROM chats WHERE chat_id = OLD.dm_id; RETURN NULL; END $$;
DROP TRIGGER IF EXISTS groups_delete_parent ON groups;
CREATE TRIGGER groups_delete_parent AFTER DELETE ON groups
    FOR EACH ROW EXECUTE FUNCTION socketcord_delete_parent_of_group();
DROP TRIGGER IF EXISTS dms_delete_parent ON dms;
CREATE TRIGGER dms_delete_parent AFTER DELETE ON dms
    FOR EACH ROW EXECUTE FUNCTION socketcord_delete_parent_of_dm();

CREATE OR REPLACE FUNCTION socketcord_chat_has_subtype() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM chats c WHERE c.chat_id = NEW.chat_id)
       AND NOT EXISTS (SELECT 1 FROM groups g WHERE g.group_id = NEW.chat_id)
       AND NOT EXISTS (SELECT 1 FROM dms d WHERE d.dm_id = NEW.chat_id) THEN
        RAISE EXCEPTION 'chat % has neither a groups row nor a dms row', NEW.chat_id;
    END IF;
    RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS chats_need_subtype ON chats;
CREATE CONSTRAINT TRIGGER chats_need_subtype AFTER INSERT ON chats
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION socketcord_chat_has_subtype();
