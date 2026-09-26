"""Strict argument models for the 7 MCP tools — shared by the MCP server (tool signatures) and the
orchestrator (validate_plan, args hashing). Both sides hash `normalize()` output, so defaults the
planner omitted can't cause an args-hash mismatch.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from cloudscale.common.schemas import CloudProvider, K8sName, Strict

Quantity = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,4}(Mi|Gi)$")]
Cpu = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,4}m?$")]
ImageTag = Annotated[str, StringConstraints(pattern=r"^v?[0-9]+\.[0-9]+\.[0-9]+$")]


class Scope(Strict):
    namespace: K8sName
    cloud_provider: CloudProvider
    idempotency_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ResourceSpec(Strict):
    memory: Quantity | None = None
    cpu: Cpu | None = None


class Resources(Strict):
    limits: ResourceSpec | None = None
    requests: ResourceSpec | None = None


class HotfixPatch(Strict):
    """Allowlisted keys only (replaces v1's free-form dict). Bounds are checked by validators."""
    resources: Resources | None = None
    connection_pool_max_size: int | None = Field(default=None, ge=1, le=200)
    tls_secret_ref: K8sName | None = None
    image_tag: ImageTag | None = None


class GetMetricsArgs(Strict):
    scope: Scope
    service: K8sName
    time_range: Literal["1m", "5m", "15m"] = "5m"


class FetchLogsArgs(Strict):
    scope: Scope
    service: K8sName
    lines: int = Field(default=100, ge=1, le=500)


class GetDeploymentArgs(Strict):
    scope: Scope
    deployment: K8sName


class ClearPodCacheArgs(Strict):
    scope: Scope
    service: K8sName


class RestartServiceArgs(Strict):
    scope: Scope
    service: K8sName


class ApplyHotfixArgs(Strict):
    scope: Scope
    deployment: K8sName
    patch: HotfixPatch


class RollbackArgs(Strict):
    scope: Scope
    deployment: K8sName
    revision: int = Field(ge=1)


ARG_MODELS: dict[str, type[BaseModel]] = {
    "get_metrics": GetMetricsArgs,
    "fetch_k8s_logs": FetchLogsArgs,
    "get_deployment_status": GetDeploymentArgs,
    "clear_pod_cache": ClearPodCacheArgs,
    "restart_service": RestartServiceArgs,
    "apply_hotfix": ApplyHotfixArgs,
    "rollback_deployment": RollbackArgs,
}


def normalize(tool: str, args: dict[str, Any] | BaseModel) -> dict[str, Any]:
    """Validate + fill defaults. Raises KeyError (unknown tool) or pydantic.ValidationError."""
    model = args if isinstance(args, BaseModel) else ARG_MODELS[tool].model_validate(args)
    return model.model_dump(mode="json")
