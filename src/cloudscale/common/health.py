"""Dependency probes shared by the /health endpoints and scripts/health_check.py."""

import asyncio

import httpx
import psycopg
import redis.asyncio as aioredis

from cloudscale.common.config import Settings


async def _postgres(s: Settings) -> str:
    async with await psycopg.AsyncConnection.connect(s.postgres_dsn, connect_timeout=3) as conn:
        cur = await conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = await cur.fetchone()
        return f"ok (pgvector {row[0]})" if row else "ok (pgvector MISSING)"


async def _redis(s: Settings) -> str:
    r = aioredis.from_url(s.redis_url, socket_timeout=3)
    try:
        await r.ping()
        return "ok"
    finally:
        await r.aclose()


async def _http(url: str) -> str:
    async with httpx.AsyncClient(timeout=3) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return "ok"


async def _ollama(s: Settings) -> str:
    async with httpx.AsyncClient(timeout=3) as c:
        resp = await c.get(f"{s.ollama_url}/api/tags")
        resp.raise_for_status()
        names = {m["name"] for m in resp.json().get("models", [])}
        return "ok" if s.guard_model in names else f"ok (model {s.guard_model} not pulled yet)"


async def probe(s: Settings, targets: list[str]) -> dict[str, str]:
    checks = {
        "postgres": lambda: _postgres(s),
        "redis": lambda: _redis(s),
        "opa": lambda: _http(f"{s.opa_url}/health"),
        "ollama": lambda: _ollama(s),
        "mcp-server": lambda: _http(s.mcp_url.removesuffix("/mcp") + "/health"),
    }

    async def run(name: str) -> tuple[str, str]:
        try:
            return name, await checks[name]()
        except Exception as e:  # noqa: BLE001 — health output, not control flow
            return name, f"FAIL: {type(e).__name__}: {e}"

    return dict(await asyncio.gather(*(run(t) for t in targets)))
