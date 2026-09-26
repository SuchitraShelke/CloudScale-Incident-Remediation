"""Scripted LLM: canned triage + plans per alert type, filled from what the agents observed.
Deterministic, free, offline. Used for development, tests and as the demo fallback."""

from collections.abc import Callable
from typing import Any

from cloudscale.common.schemas import RemediationPlan
from cloudscale.orchestrator.llm.base import LLMUsage, TriageOut

USAGE = LLMUsage(model="scripted")


def _scope(ctx: dict) -> dict:
    inc = ctx["incident"]
    return {"namespace": inc["namespace"], "cloud_provider": inc["cloud_provider"]}


def _svc(ctx: dict) -> str:
    return ctx["incident"]["affected_service"]


def _rollback(ctx: dict) -> dict:
    return {"tool_name": "rollback_deployment",
            "tool_args": {"scope": _scope(ctx), "deployment": _svc(ctx),
                          "revision": ctx["deployment"]["revision"]}}


def _oom(ctx: dict) -> dict:
    return {
        "summary": "Raise the memory limit from 1Gi to 2Gi, then roll the pods.",
        "steps": [
            {"step_id": "raise-memory-limit", "kind": "tool", "llm_claims_destructive": True,
             "estimated_impact": "Rolling update of all pods; ~5 min at reduced capacity.",
             "call": {"tool_name": "apply_hotfix",
                      "tool_args": {"scope": _scope(ctx), "deployment": _svc(ctx),
                                    "patch": {"resources": {"limits": {"memory": "2Gi"}}}}},
             "rollback": _rollback(ctx)},
            {"step_id": "restart-pods", "kind": "tool", "llm_claims_destructive": False,
             "estimated_impact": "Brief unavailability per pod during rolling restart.",
             "call": {"tool_name": "restart_service", "tool_args": {"scope": _scope(ctx), "service": _svc(ctx)}}},
        ],
        "artifacts": [{"artifact_type": "k8s_patch", "filename": f"{_svc(ctx)}-memory.yaml",
                       "target_cloud": ctx["incident"]["cloud_provider"],
                       "content": "spec:\n  template:\n    spec:\n      containers:\n        - name: app\n"
                                  "          resources:\n            limits:\n              memory: 2Gi\n"}],
    }


def _cache(ctx: dict) -> dict:
    return {
        "summary": "Clear the unbounded in-process cache; follow up with an eviction policy.",
        "steps": [{"step_id": "clear-cache", "kind": "tool", "llm_claims_destructive": False,
                   "estimated_impact": "Cold cache for ~2 min; no downtime.",
                   "call": {"tool_name": "clear_pod_cache",
                            "tool_args": {"scope": _scope(ctx), "service": _svc(ctx)}}}],
    }


def _pool(ctx: dict) -> dict:
    return {
        "summary": "Raise the DB connection pool from 20 to 50 to absorb the traffic increase.",
        "steps": [{"step_id": "raise-pool-size", "kind": "tool", "llm_claims_destructive": True,
                   "estimated_impact": "Rolling update of payment pods; ~5 min at reduced capacity.",
                   "call": {"tool_name": "apply_hotfix",
                            "tool_args": {"scope": _scope(ctx), "deployment": _svc(ctx),
                                          "patch": {"connection_pool_max_size": 50}}},
                   "rollback": _rollback(ctx)}],
    }


def _az(ctx: dict) -> dict:
    return {
        "summary": "AZ-level partition: fail traffic over away from the isolated AZ (manual runbook).",
        "steps": [{"step_id": "az-failover", "kind": "manual_runbook", "runbook_ref": "RB-AZ-FAILOVER",
                   "estimated_impact": "Regional capacity reduced by one AZ until repaired."}],
    }


# alert_name -> (triage answer, plan builder)
CANNED: dict[str, tuple[TriageOut, Callable[[dict], dict]]] = {
    "KubePodOOMKilled": (TriageOut(root_cause="JVM heap exceeds the 1Gi container memory limit under load.",
                                   root_cause_category="memory_limit_too_low", llm_confidence=0.9), _oom),
    "ContainerMemoryHigh": (TriageOut(root_cause="Thumbnail cache grows without an eviction policy.",
                                      root_cause_category="unbounded_cache", llm_confidence=0.88), _cache),
    "DBConnectionPoolExhausted": (TriageOut(root_cause="Connection pool of 20 is too small for 2.3x traffic.",
                                            root_cause_category="connection_pool_too_small",
                                            llm_confidence=0.86), _pool),
    "AZNetworkPartition": (TriageOut(root_cause="Subnet 10.0.2.0/24 (one AZ) is unreachable from the others.",
                                     root_cause_category="az_network_partition", llm_confidence=0.82), _az),
}


class ScriptedLLM:
    async def triage(self, ctx: dict[str, Any]) -> tuple[TriageOut, LLMUsage]:
        canned = CANNED.get(ctx["incident"]["alert_name"])
        if canned is None:
            return TriageOut(root_cause="No known pattern matched.", root_cause_category="unknown",
                             llm_confidence=0.3), USAGE
        out = canned[0].model_copy(update={"affected_services": [_svc(ctx)]})
        return out, USAGE

    async def plan(self, ctx: dict[str, Any]) -> tuple[RemediationPlan, LLMUsage]:
        canned = CANNED.get(ctx["incident"]["alert_name"])
        if canned is None:
            return RemediationPlan.model_validate(
                {"summary": "No automated remediation known; hand off to on-call.",
                 "steps": [{"step_id": "handoff", "kind": "manual_runbook", "runbook_ref": "RB-ONCALL"}]}), USAGE
        return RemediationPlan.model_validate(canned[1](ctx)), USAGE

    async def summarize(self, ctx: dict[str, Any]) -> tuple[str, LLMUsage]:
        v = ctx["verification"]
        return (f"{v['outcome']}: after {len(ctx['results'])} action(s), metrics are {v['final_metrics']} "
                f"against SLOs {v['thresholds']}."), USAGE
