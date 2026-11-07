"""Test fixtures: a real server on an ephemeral port, real TCP clients.

Runs against the in-memory repository. Set SOCKETCORD_TEST_DATABASE_URL to a
throwaway PostgreSQL database to run every test against Postgres as well.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "client"))

from socketcord.config import Config  # noqa: E402

# The server, the app and the client are imported only by the fixtures that start
# a server, so the repository tests need nothing but socketcord.db.

PG_URL = os.environ.get("SOCKETCORD_TEST_DATABASE_URL")
BACKENDS = ["memory"] + (["postgres"] if PG_URL else [])
TABLES = "users, chats, groups, dms, group_memberships, messages"


def make_config(**overrides) -> Config:
    # Fast PBKDF2 and no rate limits, unless a test switches them on.
    # AI summaries use the offline engine, so no test ever calls the real (paid) API,
    # even when ANTHROPIC_API_KEY happens to be set.
    cfg = Config(host="127.0.0.1", port=0, scram_iterations=4096, reg_limit_enabled=False, workers=4,
                 register_rate_per_minute=0, group_create_per_hour=0, request_rate_per_hour=0,
                 membership_rate_per_minute=0, message_rate_per_minute=0, profile_rate_per_minute=0,
                 ai_provider="offline", ai_rate_per_minute=0, ai_daily_limit=0)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class Harness:
    def __init__(self, repo, config: Config, server_kw: dict) -> None:
        from socketcord.app import Application
        from socketcord.server import SocketCordServer
        self.config = config
        self.app = Application(config, repo)
        kw = {"workers": config.workers, "housekeeping_interval": 0.2, "max_connections_per_ip": 10_000}
        kw.update(server_kw)
        self.server = SocketCordServer(self.app, config.host, config.port, **kw)
        self.address = self.server.bind()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients = []

    def client(self, **kw):
        from socketcord_client import SocketCordClient
        c = SocketCordClient(*self.address, timeout=5.0, **kw)
        self.clients.append(c)
        return c

    def user(self, name: str, password: str = None, **register_kw):
        password = password or f"pw-{name}-secret"
        c = self.client()
        c.register(name, password, **register_kw)
        c.login(name, password)
        return c

    def stop(self) -> None:
        for c in self.clients:
            c.close()
        self.server.shutdown()
        self.thread.join(5)


@pytest.fixture(scope="session")
def pg_repo():
    if not PG_URL:
        yield None
        return
    from socketcord.db.postgres import PostgresRepository
    repo = PostgresRepository(PG_URL, max_size=8)
    yield repo
    repo.close()


@pytest.fixture(params=BACKENDS)
def repo(request, pg_repo):
    if request.param == "memory":
        from socketcord.db.memory import MemoryRepository
        yield MemoryRepository()
        return
    with pg_repo.pool.connection() as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    yield pg_repo


@pytest.fixture
def make_server(repo):
    started = []

    def factory(server_kw: dict = None, **overrides) -> Harness:
        h = Harness(repo, make_config(**overrides), server_kw or {})
        started.append(h)
        return h

    yield factory
    for h in started:
        h.stop()


@pytest.fixture
def server(make_server) -> Harness:
    return make_server()
