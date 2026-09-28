"""Explicit database sessions; Alembic exclusively owns schema lifecycle."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session


def database_url() -> URL:
    direct = os.getenv("RAGOPS_DATABASE_URL")
    secret_file = os.getenv("RAGOPS_DATABASE_URL_FILE")
    if direct and secret_file:
        raise ValueError("Configure exactly one database URL source")
    if secret_file:
        direct = Path(secret_file).read_text(encoding="utf-8").strip()
    if not direct:
        raise ValueError("RAGOPS_DATABASE_URL or RAGOPS_DATABASE_URL_FILE is required")
    try:
        url = make_url(direct)
    except Exception:
        raise ValueError("Invalid database URL") from None
    if url.drivername == "sqlite" and os.getenv("RAGOPS_ENV") == "test":
        return url
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        raise ValueError("A PostgreSQL psycopg URL with host and database is required")
    return url


def open_engine(*, timeout_seconds: float | None = None) -> Engine:
    """Engine with bounded connects; a request-serving engine also bounds each statement.

    Without timeout_seconds (migrations, tooling) only the 5 s connect timeout applies.
    With it, connects, unacknowledged network writes and statements are all bounded, so
    an unreachable or stalled database fails a request instead of hanging it.
    """
    url = database_url()
    arguments: dict[str, object] = {}
    if url.drivername == "postgresql+psycopg":
        arguments["connect_timeout"] = 5
        if timeout_seconds is not None:
            milliseconds = int(timeout_seconds * 1000)
            arguments.update(
                connect_timeout=max(2, round(timeout_seconds)),  # libpq minimum is 2 s
                tcp_user_timeout=milliseconds,
                options=f"-c statement_timeout={milliseconds}",
            )
    return create_engine(url, pool_pre_ping=True, hide_parameters=True, connect_args=arguments)


@contextmanager
def transaction(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session, session.begin():
        yield session
