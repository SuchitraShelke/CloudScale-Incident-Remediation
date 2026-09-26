"""MCP client used by the agents (the anti-corruption layer between plan steps and MCP calls).
`target` is the MCP URL in production, or an MCPServer instance for in-process tests.

Resilience per call: circuit breaker (per tool + cloud) -> MCP call; READ tools retry transient
failures (3 attempts, exponential backoff); mutating tools never retry — a lost response is
recovered through the server's idempotency store instead.
"""

from typing import Any

from mcp import Client
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from cloudscale.common.telemetry import CIRCUIT_VALUE, inject_context, instruments, span
from cloudscale.orchestrator.breaker import CircuitBreaker, CircuitOpenError

TOKEN_META_KEY = "io.cloudscale/token"
READ_TOOLS = {"get_metrics", "fetch_k8s_logs", "get_deployment_status"}


class ToolCallError(Exception):
    def __init__(self, tool: str, message: str, breaker_change: str | None = None):
        self.tool, self.message, self.breaker_change = tool, message, breaker_change
        self.denied = "denied:" in message
        super().__init__(f"{tool}: {message}")


class ToolClient:
    def __init__(self, target: Any, redis=None, breaker_threshold: int = 3, breaker_cooldown_s: float = 30.0,
                 retry_wait_s: float = 0.5):
        self.target, self.redis = target, redis
        self.threshold, self.cooldown, self.retry_wait = breaker_threshold, breaker_cooldown_s, retry_wait_s

    def breaker(self, tool: str, cloud: str) -> CircuitBreaker | None:
        if self.redis is None:
            return None
        return CircuitBreaker(self.redis, tool, cloud, self.threshold, self.cooldown)

    async def _once(self, tool: str, args: dict[str, Any], token: str) -> dict[str, Any]:
        # One session per call: stateless server, and each exec token belongs to exactly one call.
        async with Client(self.target) as c:
            r = await c.call_tool(tool, args, meta={TOKEN_META_KEY: token, **inject_context()})
        if r.is_error:
            raise ToolCallError(tool, r.content[0].text if r.content else "unknown error")
        return r.structured_content or {}

    async def call(self, tool: str, args: dict[str, Any], token: str) -> dict[str, Any]:
        cloud = args["scope"]["cloud_provider"]
        with span(f"mcp.call {tool}", tool=tool, cloud=cloud) as s:
            try:
                result = await self._call(tool, args, token, cloud)
                s.set_attribute("outcome", "ok")
                return result
            except CircuitOpenError:
                s.set_attribute("outcome", "circuit_open")
                raise
            except ToolCallError as e:
                s.set_attribute("outcome", "denied" if e.denied else "error")
                if e.breaker_change:
                    instruments().circuit_state.set(CIRCUIT_VALUE[e.breaker_change], {"tool": tool, "cloud": cloud})
                raise

    async def _call(self, tool: str, args: dict[str, Any], token: str, cloud: str) -> dict[str, Any]:
        breaker = self.breaker(tool, cloud)
        if breaker:
            await breaker.before_call()                       # raises CircuitOpenError
        try:
            if tool in READ_TOOLS:
                async for attempt in AsyncRetrying(
                        stop=stop_after_attempt(3), reraise=True,
                        wait=wait_exponential(multiplier=self.retry_wait, max=4 * self.retry_wait),
                        retry=retry_if_exception(lambda e: isinstance(e, ToolCallError) and not e.denied)):
                    with attempt:
                        result = await self._once(tool, args, token)
            else:
                result = await self._once(tool, args, token)
        except ToolCallError as e:
            if breaker and not e.denied:                       # one failure per logical call, after retries
                e.breaker_change = await breaker.on_failure()
            raise
        if breaker and (changed := await breaker.on_success()):
            instruments().circuit_state.set(CIRCUIT_VALUE[changed], {"tool": tool, "cloud": cloud})
        return result
