"""Data layer. `make_repository` picks the implementation from DATABASE_URL."""
from __future__ import annotations

from .base import (AlreadyRequested, DatabaseUnavailable, NotFoundError, RateLimited,  # noqa: F401
                   RegistrationLimit, RegistrationRule, Repository, RepositoryError, UsernameTaken)


def make_repository(database_url: str, pool_size: int = 10) -> Repository:
    if database_url in ("", "memory"):
        from .memory import MemoryRepository
        return MemoryRepository()
    if database_url.startswith(("postgres://", "postgresql://")):
        from .postgres import PostgresRepository
        return PostgresRepository(database_url, max_size=pool_size)
    raise ValueError("DATABASE_URL must be 'memory' or a postgresql:// URL")
