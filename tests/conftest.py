"""Shared test setup: fake settings, real Postgres (throwaway schema), mocked Graph API.

Database: TEST_DATABASE_URL if set (e.g. a local docker-compose Postgres), otherwise
DATABASE_URL_DIRECT / DATABASE_URL from .env. Everything happens in the schema
"agent_test", which is created by the Alembic migration, truncated before every test and
dropped at the end. Other schemas are never touched.
"""

import asyncio
import hashlib
import hmac
import itertools
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

TEST_SCHEMA = "agent_test"
_dotenv = dotenv_values(Path(__file__).parent.parent / ".env")
TEST_DB_URL = (
    os.environ.get("TEST_DATABASE_URL")
    or _dotenv.get("DATABASE_URL_DIRECT")
    or _dotenv.get("DATABASE_URL")
    or ""
)

# Must be set before the app (and its cached Settings) is imported.
os.environ.update(
    {
        "WA_ACCESS_TOKEN": "test-access-token",
        "WA_APP_SECRET": "test-app-secret",
        "WA_VERIFY_TOKEN": "test-verify-token",
        "WA_PHONE_NUMBER_ID": "106540352242922",
        "GRAPH_API_VERSION": "v25.0",
        "LOG_LEVEL": "DEBUG",
        "ALLOWED_SENDERS": "",
        "DATABASE_URL": TEST_DB_URL,
        "DATABASE_URL_DIRECT": TEST_DB_URL,
        "DB_SCHEMA": TEST_SCHEMA,
        "USE_CLOUD_TASKS": "false",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "LLM_MODEL": "claude-sonnet-5-5",
        "MYBILL_BASE_URL": "https://mybill.test",
        "MYBILL_AUTH": "none",
        "ADMIN_API_KEY": "test-admin-key",
        "ADMIN_ALLOWED_EMAILS": "",
        "TASKS_TARGET_URL": "",
    }
)

import pytest  # noqa: E402
import respx  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from httpx import Request, Response  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db  # noqa: E402
from app.agent import runner  # noqa: E402
from tests.fake_llm import FakeLLM  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent
APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
MESSAGES_URL = "https://graph.facebook.com/v25.0/106540352242922/messages"
TABLES = ("llm_turns", "messages", "conversations", "product_links", "ref_codes")
MYBILL = "https://mybill.test/api/agent"

assert TEST_SCHEMA.endswith("_test")  # guard: we TRUNCATE and DROP this schema


def load_fixture(name: str) -> dict[str, Any]:
    """Load a sample webhook payload from tests/fixtures."""
    return json.loads((FIXTURES / name).read_text())


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    """Build an X-Hub-Signature-256 header value the way Meta does."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _execute(sql: str) -> list[Any]:
    """Run SQL on a fresh engine (own event loop friendly) and return rows, if any."""
    engine = db.create_engine(TEST_DB_URL, TEST_SCHEMA)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql))
            return list(result.mappings()) if result.returns_rows else []
    finally:
        await engine.dispose()


def run_sql(sql: str) -> list[Any]:
    """Synchronous helper for sync tests (TestClient tests have no running loop)."""
    return asyncio.run(_execute(sql))


@pytest.fixture(scope="session", autouse=True)
def test_schema() -> Iterator[None]:
    """Migrate the throwaway schema once per session; drop it afterwards."""
    if not TEST_DB_URL:
        pytest.exit("No test database: set TEST_DATABASE_URL or DATABASE_URL(_DIRECT) in .env", returncode=2)
    run_sql(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE')
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")
    yield
    run_sql(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE')


@pytest.fixture(autouse=True)
def fresh_settings() -> None:
    """Re-read settings for every test (some tests monkeypatch env vars)."""
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch) -> FakeLLM:
    """Every test talks to a scripted fake Claude (echoes by default); never the real API."""
    fake = FakeLLM()
    monkeypatch.setattr(runner, "get_client", lambda settings: fake)
    return fake


@pytest.fixture
def clean_tables() -> None:
    """Empty all agent_test tables (only for tests that touch the database)."""
    run_sql("TRUNCATE " + ", ".join(f'"{TEST_SCHEMA}".{t}' for t in TABLES) + " CASCADE")


@pytest.fixture
def client(clean_tables: None) -> Iterator[TestClient]:
    """TestClient with lifespan running (DB pool + shared httpx client)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def db_ready(clean_tables: None) -> AsyncIterator[None]:
    """Initialise app.db in the test's event loop (for calling the worker directly)."""
    settings = get_settings()
    db.init_db(settings.database_url.get_secret_value(), settings.db_schema)
    from app import mybill_client, whatsapp_client

    await whatsapp_client.start_client()
    await mybill_client.start_client(settings)
    yield
    await mybill_client.close_client()
    await whatsapp_client.close_client()
    await db.dispose_db()


def _graph_ok(request: Request) -> Response:
    """Fake Graph API: unique wamid per send, success for read receipts."""
    body = json.loads(request.content)
    if body.get("status") == "read":
        return Response(200, json={"success": True})
    return Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": f"wamid.out{next(_ids)}"}]})


_ids = itertools.count(1)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    """respx router for all outbound HTTP (Graph API, mybill). Unmocked requests fail."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def graph_api(router: respx.MockRouter) -> respx.Route:
    """Mock the Graph API messages endpoint; no real calls to Meta."""
    return router.post(MESSAGES_URL).mock(side_effect=_graph_ok)


def sent_texts(route: respx.Route) -> list[str]:
    """Bodies of the text messages sent through the mocked Graph API, in order."""
    bodies = [json.loads(call.request.content) for call in route.calls]
    return [b["text"]["body"] for b in bodies if b.get("type") == "text"]
