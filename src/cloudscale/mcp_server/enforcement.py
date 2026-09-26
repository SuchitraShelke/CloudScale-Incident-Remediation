"""Zero-trust enforcement for every tool call. Order matters:

1. token from request `_meta` -> verify Ed25519 signature, exp, aud, iss
2. normalize args, compute args_hash + op class
3. OPA default-deny decision (fail closed on error / timeout / missing result)
4. mutating only: idempotency key must equal hash(incident, plan, step, args) from the token;
   a stored result is returned as-is -> retries and crash-resume never re-execute
5. exec only: single-use `jti` (SET NX) -> a replayed token can't trigger a second execution
6. execute on the simulator, store the result under the idempotency key
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from cloudscale.common.canonical import args_hash, idempotency_key
from cloudscale.common.telemetry import extract_context, instruments, tracer
from cloudscale.common.tokens import TokenClaims, TokenError, TokenVerifier
from cloudscale.common.tool_args import normalize
from cloudscale.mcp_server.simulator import SimulatedToolFailure

log = logging.getLogger("cloudscale.mcp.enforcement")

TOKEN_META_KEY = "io.cloudscale/token"
IDEM_TTL_S = 24 * 3600


class Denied(ToolError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__(f"denied: {', '.join(reasons)}")


class Enforcer:
    def __init__(self, verifier: TokenVerifier, redis, opa_url: str, op_classes: dict[str, str],
                 opa_timeout_s: float = 2.0, audit=None):
        self.verifier, self.r, self.op_classes, self.audit = verifier, redis, op_classes, audit
        self.opa_decision_url = f"{opa_url.rstrip('/')}/v1/data/cloudscale/mcp/decision"
        self.opa_timeout_s = opa_timeout_s
        self.decisions: list[dict] = []   # recent decisions, for tests/debug

    def _token(self, ctx: Context) -> str:
        meta = ctx.request_context.meta or {}
        token = meta.get(TOKEN_META_KEY)
        if not token:
            raise Denied(["missing_token"])
        return token

    async def _counters(self, claims: TokenClaims, tool: str, cloud: str) -> dict[str, Any]:
        minute_key = f"rate:{claims.incident_id}:{int(time.time() // 60)}"
        calls = await self.r.incr(minute_key)
        await self.r.expire(minute_key, 120)
        state = await self.r.hget(f"breaker:{tool}:{cloud}", "state")
        state = state.decode() if isinstance(state, bytes) else state
        return {"calls_last_minute": calls - 1, "breaker_state": state or "CLOSED"}

    async def _opa(self, opa_input: dict[str, Any]) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=self.opa_timeout_s) as c:
                resp = await c.post(self.opa_decision_url, json={"input": opa_input})
                resp.raise_for_status()
                result = resp.json().get("result")
        except (httpx.HTTPError, ValueError):
            return ["opa_unavailable"]
        if not isinstance(result, dict) or "allow" not in result:
            return ["opa_no_result"]
        return [] if result["allow"] is True else sorted(result.get("reasons") or ["opa_denied"])

    async def run(self, ctx: Context, tool: str, args: BaseModel,
                  execute: Callable[[TokenClaims, dict[str, Any]], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        parent = extract_context(ctx.request_context.meta)     # trace context sent by the orchestrator
        with tracer.start_as_current_span(f"mcp.server {tool}", context=parent, record_exception=False,
                                          set_status_on_exception=False) as s:
            s.set_attribute("tool", tool)
            try:
                result = await self._run(ctx, tool, args, execute)
            except Denied as e:
                s.set_attribute("outcome", "denied")
                s.set_attribute("policy.reasons", ",".join(e.reasons))
                instruments().tool_calls.add(1, {"tool": tool, "outcome": "denied"})
                raise
            except ToolError:
                s.set_attribute("outcome", "error")
                instruments().tool_calls.add(1, {"tool": tool, "outcome": "error"})
                raise
            s.set_attribute("outcome", "ok")
            s.set_attribute("idempotent_replay", bool(result.get("idempotent_replay")))
            instruments().tool_calls.add(1, {"tool": tool, "outcome": "ok"})
            return result

    async def _run(self, ctx: Context, tool: str, args: BaseModel,
                   execute: Callable[[TokenClaims, dict[str, Any]], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            claims = self.verifier.verify(self._token(ctx))
        except TokenError as e:
            raise await self._deny(tool, None, [str(e).split(":")[0]]) from e

        norm = normalize(tool, args)
        ah = args_hash(tool, norm)
        op = self.op_classes.get(tool, "UNKNOWN")
        opa_input = {
            "tool": tool, "op_class": op, "args": norm, "args_hash": ah,
            "token": claims.model_dump(exclude_none=True),
            "counters": await self._counters(claims, tool, norm["scope"]["cloud_provider"]),
        }
        if reasons := await self._opa(opa_input):
            raise await self._deny(tool, claims, reasons, ah)

        idem = None
        if op != "READ":
            if claims.typ != "exec":       # defense in depth; OPA already denies this
                raise await self._deny(tool, claims, ["read_token_mutation"], ah)
            expected = idempotency_key(claims.incident_id, claims.plan_hash, claims.step.step_id, ah)
            if norm["scope"]["idempotency_key"] != expected:
                raise await self._deny(tool, claims, ["bad_idempotency_key"], ah)
            idem = f"idem:{expected}"
            if cached := await self.r.get(idem):
                await self._record(tool, claims, "allow", ["idempotent_replay"], ah)
                return json.loads(cached) | {"idempotent_replay": True}

        if claims.typ == "exec" and not await self.r.set(f"jti:{claims.jti}", 1, nx=True, ex=900):
            raise await self._deny(tool, claims, ["token_replayed"], ah)

        # Can't audit -> can't act: a mutating call only runs once its allow decision is on the chain.
        if not await self._record(tool, claims, "allow", [], ah, args=norm) and op != "READ":
            await self.r.delete(f"jti:{claims.jti}")
            raise Denied(["audit_unavailable"])
        try:
            result = await execute(claims, norm)
        except SimulatedToolFailure as e:
            await self._audit("TOOL_CALL_FAILED", claims, {"tool": tool, "args_hash": ah, "error": str(e)})
            raise ToolError(f"tool_failed: {e}") from e
        if idem:
            await self.r.set(idem, json.dumps(result), ex=IDEM_TTL_S)
        if op != "READ":
            await self._audit("TOOL_CALL_FINISHED", claims, {"tool": tool, "args_hash": ah, "ok": True})
        return result

    async def _audit(self, event_type: str, claims: TokenClaims | None, payload: dict) -> bool:
        if self.audit is None:
            return True
        try:
            await self.audit.append(event_type, "policy", "mcp-server", payload,
                                    claims.incident_id if claims else None)
            return True
        except Exception:  # audit outage: reported to the caller, which decides whether to proceed
            log.exception("audit append failed")
            return False

    async def _record(self, tool: str, claims: TokenClaims | None, outcome: str, reasons: list[str],
                      args_hash_: str | None = None, args: dict | None = None) -> bool:
        entry = {"tool": tool, "outcome": outcome, "reasons": reasons,
                 "incident_id": claims.incident_id if claims else None}
        self.decisions = (self.decisions + [entry])[-100:]
        log.info("policy_decision %s", entry)
        payload = {"tool": tool, "outcome": outcome, "reasons": reasons, "args_hash": args_hash_,
                   "token_type": claims.typ if claims else None,
                   "step_id": claims.step.step_id if claims and claims.step else None,
                   "approver": claims.approval.approver if claims and claims.approval else None}
        if args is not None:
            payload["args"] = args            # tool invocation arguments, for post-mortems
        return await self._audit("POLICY_DECISION", claims, payload)

    async def _deny(self, tool: str, claims: TokenClaims | None, reasons: list[str],
                    args_hash_: str | None = None) -> Denied:
        await self._record(tool, claims, "deny", reasons, args_hash_)
        return Denied(reasons)
