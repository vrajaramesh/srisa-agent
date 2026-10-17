"""Async SQLAlchemy engine/session for Neon serverless Postgres."""

import ssl
from uuid import uuid4

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import SCHEMA

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

# libpq-only query params asyncpg rejects.
_LIBPQ_ONLY = ("sslmode", "channel_binding")


def to_asyncpg(url: str) -> tuple[URL, dict]:
    """Turn a libpq-style URL (as Neon gives it) into an asyncpg URL + connect_args.

    asyncpg doesn't understand `sslmode=require`; it wants an `ssl` argument instead.
    """
    parsed = make_url(url)
    sslmode = parsed.query.get("sslmode")
    parsed = parsed.set(drivername="postgresql+asyncpg").difference_update_query(_LIBPQ_ONLY)
    connect_args: dict = {}
    if sslmode and sslmode not in ("disable", "allow", "prefer"):
        connect_args["ssl"] = ssl.create_default_context()
    return parsed, connect_args


def create_engine(url: str, schema: str = SCHEMA) -> AsyncEngine:
    """Engine tuned for Neon's pooler (PgBouncer, transaction mode).

    PgBouncer in transaction mode hands each transaction a possibly different server
    connection, so per-connection prepared statements break: disable asyncpg's and
    SQLAlchemy's statement caches and give statements unique names. Also no session-level
    state (SET, session advisory locks) - only transaction-scoped locks.
    """
    sa_url, connect_args = to_asyncpg(url)
    connect_args |= {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    }
    # Models are declared in schema "agent"; tests remap it to e.g. "agent_test".
    execution_options = {"schema_translate_map": {SCHEMA: schema}} if schema != SCHEMA else {}
    return create_async_engine(
        sa_url,
        connect_args=connect_args,
        pool_pre_ping=True,  # Neon computes auto-suspend; drop dead connections
        pool_size=5,
        max_overflow=5,
        pool_recycle=300,
        execution_options=execution_options,
    )


def init_db(url: str, schema: str = SCHEMA) -> None:
    """Create the process-wide engine (called from the FastAPI lifespan)."""
    global _engine, _sessionmaker
    _engine = create_engine(url, schema)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


async def dispose_db() -> None:
    """Close all pooled connections."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine, _sessionmaker = None, None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, failing loudly if the lifespan didn't start it."""
    if _sessionmaker is None:
        raise RuntimeError("Database not initialised; is the app lifespan running?")
    return _sessionmaker
