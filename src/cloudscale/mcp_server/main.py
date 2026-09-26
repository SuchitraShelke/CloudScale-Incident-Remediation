"""MCP server entrypoint (mcp SDK 2.x: FastMCP is now `MCPServer`)."""

import asyncio
import logging
from pathlib import Path

import redis.asyncio as aioredis
from mcp.server.transport_security import TransportSecuritySettings
from psycopg_pool import AsyncConnectionPool

from cloudscale.common import telemetry
from cloudscale.common.audit import AuditLog
from cloudscale.common.config import get_settings
from cloudscale.common.gate import load_policy
from cloudscale.common.health import probe
from cloudscale.common.tokens import TokenVerifier, load_public_key
from cloudscale.mcp_server.enforcement import Enforcer
from cloudscale.mcp_server.server import build_server
from cloudscale.mcp_server.simulator import Simulator


async def serve() -> None:
    s = get_settings()
    if s.otel_enabled:
        telemetry.setup(s.service_name, s.otel_endpoint, s.otel_metrics_endpoint)
    # Audit pool opens lazily: if the audit DB is down, mutating calls are refused, reads still work.
    audit_pool = AsyncConnectionPool(s.audit_dsn, min_size=0, max_size=4, open=False, timeout=5)
    await audit_pool.open(wait=False)
    enforcer = Enforcer(
        verifier=TokenVerifier(load_public_key(Path(s.token_public_key_path))),   # public key only
        redis=aioredis.from_url(s.redis_url),
        opa_url=s.opa_url,
        op_classes=load_policy()["op_classes"],
        audit=AuditLog(audit_pool),
    )
    mcp = build_server(enforcer, Simulator(Path(s.scenarios_dir)), s.sim_control_key,
                       health=lambda: probe(s, ["redis", "opa"]))
    # Bound to 0.0.0.0 inside Docker, so DNS-rebinding protection needs explicit allowed hosts.
    security = TransportSecuritySettings(allowed_hosts=["mcp-server:*", "localhost:*", "127.0.0.1:*"],
                                         allowed_origins=[])
    await mcp.run_streamable_http_async(host="0.0.0.0", port=8001, stateless_http=True,
                                        transport_security=security)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
