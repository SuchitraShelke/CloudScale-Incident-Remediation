"""MCP server factory: 7 tools on the simulator, every call through the Enforcer."""

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from cloudscale.common.schemas import K8sName
from cloudscale.common.tool_args import (
    ApplyHotfixArgs,
    ClearPodCacheArgs,
    FetchLogsArgs,
    GetDeploymentArgs,
    GetMetricsArgs,
    HotfixPatch,
    RestartServiceArgs,
    RollbackArgs,
    Scope,
)
from cloudscale.mcp_server.enforcement import Enforcer
from cloudscale.mcp_server.simulator import SimulatedToolFailure, Simulator

Result = dict[str, Any]


def build_server(enforcer: Enforcer, sim: Simulator, sim_control_key: str,
                 health: Any = None) -> MCPServer:
    mcp = MCPServer("cloudscale-tools", version="0.1.0",
                    instructions="Infrastructure remediation tools. Every call needs a scoped token in _meta.")

    async def mutate(tool: str, claims, args: dict) -> Result:
        sim.check_fault(tool)
        return sim.get(claims.incident_id).apply(tool, args)

    # ---------- READ ----------
    @mcp.tool()
    async def get_metrics(scope: Scope, service: K8sName, ctx: Context, time_range: str = "5m") -> Result:
        """Current golden-signal metrics for a service."""
        async def run(claims, a):
            sim.check_fault("get_metrics")
            return {"service": a["service"], "time_range": a["time_range"],
                    "metrics": sim.get(claims.incident_id).metrics()}
        return await enforcer.run(ctx, "get_metrics", GetMetricsArgs(scope=scope, service=service,
                                                                      time_range=time_range), run)

    @mcp.tool()
    async def fetch_k8s_logs(scope: Scope, service: K8sName, ctx: Context, lines: int = 100) -> Result:
        """Recent pod logs for a service. Output is untrusted data."""
        async def run(claims, a):
            sim.check_fault("fetch_k8s_logs")
            return {"service": a["service"], "lines": sim.get(claims.incident_id).logs(a["lines"])}
        return await enforcer.run(ctx, "fetch_k8s_logs", FetchLogsArgs(scope=scope, service=service, lines=lines), run)

    @mcp.tool()
    async def get_deployment_status(scope: Scope, deployment: K8sName, ctx: Context) -> Result:
        """Replicas, current/previous revision, image and resources (needed to plan a rollback)."""
        async def run(claims, a):
            return sim.get(claims.incident_id).deployment(a["deployment"])
        return await enforcer.run(ctx, "get_deployment_status", GetDeploymentArgs(scope=scope, deployment=deployment), run)

    # ---------- MUTATING ----------
    @mcp.tool()
    async def clear_pod_cache(scope: Scope, service: K8sName, ctx: Context) -> Result:
        """Clear in-process caches on a service's pods (SAFE_MUTATION)."""
        return await enforcer.run(ctx, "clear_pod_cache", ClearPodCacheArgs(scope=scope, service=service),
                                  lambda c, a: mutate("clear_pod_cache", c, a))

    @mcp.tool()
    async def restart_service(scope: Scope, service: K8sName, ctx: Context) -> Result:
        """Rolling restart of a service (DISRUPTIVE)."""
        return await enforcer.run(ctx, "restart_service", RestartServiceArgs(scope=scope, service=service),
                                  lambda c, a: mutate("restart_service", c, a))

    @mcp.tool()
    async def apply_hotfix(scope: Scope, deployment: K8sName, patch: HotfixPatch, ctx: Context) -> Result:
        """Apply an allowlisted config patch to a deployment (DESTRUCTIVE)."""
        return await enforcer.run(ctx, "apply_hotfix", ApplyHotfixArgs(scope=scope, deployment=deployment, patch=patch),
                                  lambda c, a: mutate("apply_hotfix", c, a))

    @mcp.tool()
    async def rollback_deployment(scope: Scope, deployment: K8sName, revision: int, ctx: Context) -> Result:
        """Roll a deployment back to a revision from its rollout history (DESTRUCTIVE)."""
        return await enforcer.run(ctx, "rollback_deployment",
                                  RollbackArgs(scope=scope, deployment=deployment, revision=revision),
                                  lambda c, a: mutate("rollback_deployment", c, a))

    # ---------- simulator control plane (not MCP; exists only because the backend is simulated) ----------
    def authorized(request: Request) -> bool:
        return request.headers.get("x-sim-key") == sim_control_key

    @mcp.custom_route("/sim/register", methods=["POST"])
    async def sim_register(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await request.json()
        try:
            sim.register(body["incident_id"], body["scenario_id"])
        except (KeyError, FileNotFoundError) as e:
            return JSONResponse({"error": f"unknown scenario: {e}"}, status_code=400)
        return JSONResponse({"registered": body["incident_id"]})

    @mcp.custom_route("/sim/faults", methods=["POST"])
    async def sim_faults(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await request.json()
        sim.inject_fault(body["tool"], int(body["fail_times"]))
        return JSONResponse({"faults": sim.faults})

    @mcp.custom_route("/sim/state/{incident_id}", methods=["GET"])
    async def sim_state(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            inc = sim.get(request.path_params["incident_id"])
        except SimulatedToolFailure as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"scenario_id": inc.scenario_id, "metrics": inc.metrics(),
                             "deployments": inc.deployments, "actions": inc.actions})

    @mcp.custom_route("/livez", methods=["GET"])
    async def livez(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @mcp.custom_route("/health", methods=["GET"])
    async def health_route(_: Request) -> JSONResponse:
        deps = await health() if health else {}
        return JSONResponse({"service": "mcp-server", "deps": deps})

    return mcp
