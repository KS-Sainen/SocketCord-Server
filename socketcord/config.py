"""Settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from typing import List, Optional

from . import scram


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _env_choice(name: str, default: str, choices: tuple) -> str:
    value = _env(name, default).lower()
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)} (got {value!r})")
    return value


AI_PROVIDERS = ("auto", "claude", "offline", "off")
AI_EFFORTS = ("low", "medium", "high", "xhigh", "max", "default")


def _env_networks(name: str, default: str) -> List[ipaddress._BaseNetwork]:
    raw = _env(name, default)
    return [ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()]


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8080
    database_url: str = "memory"          # "memory" or postgresql://user:pass@host:5432/db
    workers: int = 16                     # request-handler threads
    max_connections: int = 1024
    max_connections_per_ip: int = 64      # one address cannot take every slot
    max_connections_per_user: int = 5     # logged-in connections per account
    log_level: str = "INFO"

    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    scram_iterations: int = scram.DEFAULT_ITERATIONS
    challenge_ttl: int = 60               # seconds a challenge stays valid
    session_ttl_seconds: int = 7 * 24 * 3600

    login_max_failures: int = 5           # per (username, ip) ...
    login_failure_window: int = 300       # ... within this many seconds
    login_max_failures_per_client: int = 50   # per ip, over all usernames, in the same window

    # Requests that make the server do expensive work (a 2048-bit DH step) are
    # limited per client address: register and password-change challenges.
    challenge_rate_per_minute: float = 30.0
    challenge_burst: int = 15
    # Things that push a packet to other users are limited per account.
    group_create_per_hour: float = 20.0   # POST chats
    request_rate_per_hour: float = 120.0  # DM requests and group invites
    membership_rate_per_minute: float = 30.0   # joins + leaves
    message_rate_per_minute: float = 60.0      # messages sent
    profile_rate_per_minute: float = 10.0      # profile updates
    max_pending_requests: int = 100       # pending requests one user may send / receive

    # "One new account per device/address per day". Each rule is checked on its own;
    # 0 turns a rule off. The IP rule is the server's own observation; the device
    # rule relies on the id the client sends (a hash of its MAC address).
    reg_limit_enabled: bool = True
    reg_limit_window_hours: int = 24
    reg_limit_device_max: int = 1
    reg_limit_ip_max: int = 1
    reg_limit_exempt: List[ipaddress._BaseNetwork] = field(default_factory=list)

    dm_require_accept: bool = False       # true: the first DM is a request the receiver must accept

    # AI chat summaries, GET chat/{id}/summary (socketcord/ai.py). The API key is
    # read by the Anthropic SDK from ANTHROPIC_API_KEY; it is never stored here.
    ai_provider: str = "auto"             # auto (Claude if a key is set, else offline) | claude | offline | off
    ai_model: str = "claude-opus-5-5"
    ai_effort: str = "low"                # how long the model thinks: low | medium | high | xhigh | max
    ai_max_tokens: int = 8192             # thinking + summary
    ai_timeout: float = 30.0              # seconds per attempt
    ai_max_retries: int = 1               # SDK retries on 429/5xx/connection errors
    ai_fallbacks: bool = True             # on a refusal, the API retries on its recommended fallback model
    ai_max_concurrent: int = 4            # summaries being written at once (capped at workers/2)
    ai_rate_per_minute: float = 4.0       # summary requests per account (burst 3)
    ai_daily_limit: int = 100             # paid Claude calls per day for the whole server; 0 = no cap
    ai_cache_seconds: int = 600           # the same messages asked again within this time cost nothing
    ai_max_input_chars: int = 40_000      # transcript size cap; the oldest messages go first

    @classmethod
    def from_env(cls) -> "Config":
        secret_text = os.environ.get("SOCKETCORD_SECRET", "").strip()
        return cls(
            host=_env("SOCKETCORD_HOST", "0.0.0.0"),
            port=_env_int("SOCKETCORD_PORT", 8080),
            database_url=_env("DATABASE_URL", "memory"),
            workers=_env_int("SOCKETCORD_WORKERS", 16),
            max_connections=_env_int("SOCKETCORD_MAX_CONNECTIONS", 1024),
            max_connections_per_ip=_env_int("MAX_CONNECTIONS_PER_IP", 64),
            max_connections_per_user=_env_int("MAX_CONNECTIONS_PER_USER", 5),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            secret=secret_text.encode() if secret_text else secrets.token_bytes(32),
            scram_iterations=scram.check_iterations(_env_int("SCRAM_ITERATIONS", scram.DEFAULT_ITERATIONS)),
            challenge_ttl=_env_int("CHALLENGE_TTL_SECONDS", 60),
            session_ttl_seconds=_env_int("SESSION_TTL_HOURS", 24 * 7) * 3600,
            login_max_failures=_env_int("LOGIN_MAX_FAILURES", 5),
            login_failure_window=_env_int("LOGIN_FAILURE_WINDOW_SECONDS", 300),
            login_max_failures_per_client=_env_int("LOGIN_MAX_FAILURES_PER_CLIENT", 50),
            challenge_rate_per_minute=float(_env("CHALLENGE_RATE_PER_MINUTE", "30")),
            challenge_burst=_env_int("CHALLENGE_BURST", 15),
            group_create_per_hour=float(_env("GROUP_CREATE_PER_HOUR", "20")),
            request_rate_per_hour=float(_env("REQUEST_RATE_PER_HOUR", "120")),
            membership_rate_per_minute=float(_env("MEMBERSHIP_RATE_PER_MINUTE", "30")),
            message_rate_per_minute=float(_env("MESSAGE_RATE_PER_MINUTE", "60")),
            profile_rate_per_minute=float(_env("PROFILE_RATE_PER_MINUTE", "10")),
            max_pending_requests=_env_int("MAX_PENDING_REQUESTS", 100),
            reg_limit_enabled=_env_bool("REG_LIMIT_ENABLED", True),
            reg_limit_window_hours=_env_int("REG_LIMIT_WINDOW_HOURS", 24),
            reg_limit_device_max=_env_int("REG_LIMIT_DEVICE_MAX", 1),
            reg_limit_ip_max=_env_int("REG_LIMIT_IP_MAX", 1),
            reg_limit_exempt=_env_networks("REG_LIMIT_EXEMPT", ""),
            dm_require_accept=_env_bool("DM_REQUIRE_ACCEPT", False),
            ai_provider=_env_choice("AI_PROVIDER", "auto", AI_PROVIDERS),
            ai_model=_env("AI_MODEL", "claude-opus-5-5"),
            ai_effort=_env_choice("AI_EFFORT", "low", AI_EFFORTS),
            ai_max_tokens=_env_int("AI_MAX_TOKENS", 8192),
            ai_timeout=float(_env("AI_TIMEOUT_SECONDS", "30")),
            ai_max_retries=_env_int("AI_MAX_RETRIES", 1),
            ai_fallbacks=_env_bool("AI_FALLBACKS", True),
            ai_max_concurrent=_env_int("AI_MAX_CONCURRENT", 4),
            ai_rate_per_minute=float(_env("AI_SUMMARY_PER_MINUTE", "4")),
            ai_daily_limit=_env_int("AI_DAILY_LIMIT", 100),
            ai_cache_seconds=_env_int("AI_CACHE_SECONDS", 600),
            ai_max_input_chars=_env_int("AI_MAX_INPUT_CHARS", 40_000),
        )

    def ip_exempt(self, ip: Optional[str]) -> bool:
        """True if the IP rule should not apply to this address."""
        if not ip:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.reg_limit_exempt)
