"""Alembic environment: async migrations against Neon's DIRECT (non-pooled) endpoint.

Only ever touches the schema named by DB_SCHEMA (default "agent"), including Alembic's
own version table, so other schemas in the shared database are left alone.
"""

import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import DatabaseSettings
from app.db import to_asyncpg

settings = DatabaseSettings()  # type: ignore[call-arg]
SCHEMA = settings.db_schema
# Migrations need a direct connection (DDL + Alembic's version bookkeeping don't belong
# on PgBouncer); fall back to DATABASE_URL for a plain local Postgres.
URL = (settings.database_url_direct or settings.database_url).get_secret_value()


def _run(connection: Connection) -> None:
    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
    context.configure(
        connection=connection,
        version_table_schema=SCHEMA,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    sa_url, connect_args = to_asyncpg(URL)
    engine = create_async_engine(sa_url, connect_args=connect_args, poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline mode is not supported; run against a database.")
asyncio.run(_run_async())
