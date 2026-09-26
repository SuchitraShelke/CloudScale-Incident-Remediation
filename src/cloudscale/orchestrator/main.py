"""Orchestrator entrypoint: wires config, Postgres, Redis, MCP and the graph into the API."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from cloudscale.common import telemetry
from cloudscale.common.audit import AuditLog
from cloudscale.common.config import get_settings
from cloudscale.common.db import migrate
from cloudscale.common.health import probe
from cloudscale.common.safety.llama_guard import Guard, RedisGuardCache
from cloudscale.common.tokens import TokenIssuer, load_private_key
from cloudscale.orchestrator.api import router
from cloudscale.orchestrator.auth import Auth
from cloudscale.orchestrator.cache import FastEmbedder, SemanticCache
from cloudscale.orchestrator.graph import Deps, build_graph
from cloudscale.orchestrator.ledger import TokenLedger
from cloudscale.orchestrator.llm.claude import ClaudeLLM
from cloudscale.orchestrator.llm.scripted import ScriptedLLM
from cloudscale.orchestrator.service import IncidentService
from cloudscale.orchestrator.store import PgIncidentStore
from cloudscale.orchestrator.tools import ToolClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cloudscale.orchestrator")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    await migrate(s.postgres_dsn, "audit_writer", s.audit_db_password)
    redis = aioredis.from_url(s.redis_url)
    sim_base = s.mcp_url.removesuffix("/mcp")

    async def register_sim(incident_id: str, scenario_id: str) -> None:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{sim_base}/sim/register", headers={"x-sim-key": s.sim_control_key},
                             json={"incident_id": incident_id, "scenario_id": scenario_id})
            r.raise_for_status()

    pool = AsyncConnectionPool(s.postgres_dsn, min_size=1, max_size=5, open=False)
    audit_pool = AsyncConnectionPool(s.audit_dsn, min_size=1, max_size=5, open=False)
    await pool.open()
    await audit_pool.open()
    if s.llm_mode == "live" and s.anthropic_api_key:
        llm = ClaudeLLM(anthropic.AsyncAnthropic(api_key=s.anthropic_api_key, max_retries=1), s.model_deep, s.model_fast)
        log.info("LLM mode: live (%s / %s, scripted fallback)", s.model_deep, s.model_fast)
    else:
        llm = ScriptedLLM()
        log.info("LLM mode: scripted%s", " (LLM_MODE=live but no ANTHROPIC_API_KEY)" if s.llm_mode == "live" else "")
    deps = Deps(llm=llm, tools=ToolClient(s.mcp_url, redis, s.breaker_threshold, s.breaker_cooldown_s),
                issuer=TokenIssuer(load_private_key(Path(s.token_private_key_path))),
                guard=Guard(s.ollama_url, s.guard_model, s.guard_timeout_s, RedisGuardCache(redis)),
                cache=SemanticCache(pool, FastEmbedder(s.fastembed_cache_dir)))
    async with AsyncPostgresSaver.from_conn_string(s.postgres_dsn) as saver:
        await saver.setup()
        app.state.auth = Auth(s.session_secret, s.demo_users)
        app.state.audit = AuditLog(audit_pool)
        app.state.ledger = TokenLedger(pool)
        app.state.service = IncidentService(build_graph(deps, saver), register_sim, Path(s.scenarios_dir),
                                            PgIncidentStore(pool), app.state.audit, app.state.ledger)
        # Load the embedding model now (first load ~20 s on the dev VM) instead of on the first incident.
        warm = asyncio.create_task(deps.cache.embedder.embed("warm up"))
        warm.add_done_callback(lambda t: log.info("embedding model warm: %s", "ok" if not t.exception() else t.exception()))
        resumed = await app.state.service.resume_in_flight()
        if resumed:
            log.info("resumed in-flight incidents after restart: %s", resumed)
        yield
    await pool.close()
    await audit_pool.close()
    await redis.aclose()


app = FastAPI(title="CloudScale Orchestrator", lifespan=lifespan)
app.include_router(router)


def instrument(app: FastAPI) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    s = get_settings()
    telemetry.setup(s.service_name, s.otel_endpoint, s.otel_metrics_endpoint)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="livez,health")


if get_settings().otel_enabled:
    instrument(app)


@app.get("/livez")
async def livez() -> dict:
    """Docker healthcheck: process is serving. Must stay cheap — deep probes stall on this host."""
    return {"ok": True}


@app.get("/health")
async def health() -> dict:
    s = get_settings()
    deps = await probe(s, ["postgres", "redis", "ollama", "opa", "mcp-server"])
    live = s.llm_mode == "live" and bool(s.anthropic_api_key)
    return {"service": "orchestrator", "llm_mode": "live" if live else "scripted", "deps": deps}
