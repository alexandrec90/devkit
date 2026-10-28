"""Alembic's entry point: how `alembic upgrade` reaches the database.

The URL comes from `DATABASE_URL`, never from `alembic.ini`: each worktree publishes the
database on its own host port, so a committed URL would be wrong for all but one of them.
"""

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# The models' `MetaData`, once there are models: `alembic revision --autogenerate`
# compares it with the database to write a revision.
target_metadata = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set: alembic reads the database's URL from it")
    return url


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: the SQL a migration would run, with no connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
