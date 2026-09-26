"""Shared fixtures. Postgres-backed tests use the compose Postgres (127.0.0.1:5432) with a separate
`cloudscale_test` database, and are skipped when it isn't running."""

import asyncio
import sys

import httpx
import psycopg
import pytest
import pytest_asyncio
import respx
from psycopg_pool import AsyncConnectionPool

from cloudscale.common.db import migrate

if sys.platform == "win32":   # async psycopg can't use Windows' default Proactor loop (containers are Linux)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

OPA = "http://opa.test:8181"
OLLAMA = "http://ollama.test:11434"

ADMIN_DSN = "postgresql://cloudscale:cloudscale@127.0.0.1:5432/cloudscale"
TEST_DSN = "postgresql://cloudscale:cloudscale@127.0.0.1:5432/cloudscale_test"
AUDIT_DSN = "postgresql://audit_writer:audit-test@127.0.0.1:5432/cloudscale_test"


def _pg_available() -> bool:
    try:
        with psycopg.connect(ADMIN_DSN, connect_timeout=2, autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = 'cloudscale_test'").fetchone():
                conn.execute("CREATE DATABASE cloudscale_test")
        return True
    except psycopg.OperationalError:
        return False


@pytest.fixture(scope="session")
def pg():
    if not _pg_available():
        pytest.skip("Postgres not running (docker compose up -d postgres)")
    return TEST_DSN


@pytest_asyncio.fixture
async def clean_db(pg):
    """Fresh schema per test: drop tables as the owner (the triggers block TRUNCATE by design)."""
    async with await psycopg.AsyncConnection.connect(pg, autocommit=True) as conn:
        await conn.execute("DROP TABLE IF EXISTS audit_log, incidents, semantic_cache, token_usage")
    await migrate(pg, "audit_writer", "audit-test")
    return pg


@pytest_asyncio.fixture
async def audit_pool(clean_db):
    pool = AsyncConnectionPool(AUDIT_DSN, min_size=1, max_size=4, open=False)
    await pool.open()
    yield pool
    await pool.close()


@pytest.fixture
def mocked_backends():
    """OPA allow-all (rules are covered by `opa test` + scripts/mcp_smoke.py) and Ollama unreachable,
    which is this dev host's reality."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{OPA}/v1/data/cloudscale/mcp/decision").mock(
            return_value=httpx.Response(200, json={"result": {"allow": True, "reasons": []}}))
        mock.post(f"{OLLAMA}/api/generate").mock(side_effect=httpx.ConnectError("ollama unreachable"))
        yield
