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


def open_engine() -> Engine:
    url = database_url()
    arguments = {"connect_timeout": 5} if url.drivername == "postgresql+psycopg" else {}
    return create_engine(url, pool_pre_ping=True, hide_parameters=True, connect_args=arguments)


@contextmanager
def transaction(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session, session.begin():
        yield session
