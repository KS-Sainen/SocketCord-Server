"""Entry point: python -m socketcord [--host H] [--port P] [--db URL|memory] [--no-reg-limit]"""
from __future__ import annotations

import argparse
import logging
import signal
import sys

from . import __version__
from .app import Application
from .config import Config
from .db import make_repository
from .db.base import DatabaseUnavailable
from .server import SocketCordServer


def build(config: Config) -> SocketCordServer:
    repo = make_repository(config.database_url, pool_size=config.workers + 2)
    app = Application(config, repo)
    return SocketCordServer(app, config.host, config.port, workers=config.workers,
                            max_connections=config.max_connections,
                            max_connections_per_ip=config.max_connections_per_ip)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m socketcord", description="SocketCord backend server")
    parser.add_argument("--host", help="address to listen on (default SOCKETCORD_HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, help="TCP port (default SOCKETCORD_PORT or 8080)")
    parser.add_argument("--db", help="'memory' or a postgresql:// URL (default DATABASE_URL or memory)")
    parser.add_argument("--no-reg-limit", action="store_true",
                        help="turn off the one-account-per-day rule (for local testing)")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING (default LOG_LEVEL or INFO)")
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.host:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    if args.db:
        config.database_url = args.db
    if args.no_reg_limit:
        config.reg_limit_enabled = False
    if args.log_level:
        config.log_level = args.log_level.upper()

    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("socketcord")
    log.info("SocketCord backend %s", __version__)
    if config.database_url == "memory":
        log.warning("using the in-memory database: nothing is saved when the server stops")
    if not config.reg_limit_enabled:
        log.warning("the one-account-per-day registration limit is OFF")
    elif config.reg_limit_ip_max <= 0:
        log.warning("REG_LIMIT_IP_MAX=0: only the device id (which clients choose) limits sign-ups")

    try:
        server = build(config)
    except DatabaseUnavailable as exc:
        log.error("cannot start: %s", exc)
        return 1
    log.info("AI summaries: %s", server.app.ai.describe())

    received = []

    def stop(signum, _frame):
        # Nothing that takes a lock in here (logging does): just set the flag.
        received.append(signum)
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        if received:
            log.info("stopped by %s", signal.Signals(received[0]).name)
        server.app.repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
