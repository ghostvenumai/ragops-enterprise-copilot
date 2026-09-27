"""Alembic environment with externally injected credentials and no implicit DDL."""

from alembic import context

from ragops.persistence.database import database_url, open_engine
from ragops.persistence.models import Base


def run_migrations() -> None:
    if context.is_offline_mode():
        context.configure(
            url=database_url(),
            target_metadata=Base.metadata,
            literal_binds=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    connection = context.config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = open_engine()
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=Base.metadata, compare_type=True
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
