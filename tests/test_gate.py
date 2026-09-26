"""Table-driven: every matrix cell and every override in gate_policy.yaml has a case."""

import pytest

from cloudscale.common.gate import evaluate_plan, load_policy, risk_level, step_gate
from cloudscale.common.schemas import GateContext, RemediationPlan, RemediationStep, ToolCall

SCOPE = {"namespace": "production", "cloud_provider": "aws"}
ROLLBACK = ToolCall(tool_name="rollback_deployment", tool_args={"scope": SCOPE, "deployment": "x", "revision": 1})

TOOL_FOR = {
    "READ": "get_metrics",
    "SAFE_MUTATION": "clear_pod_cache",
    "DISRUPTIVE": "restart_service",
    "DESTRUCTIVE": "apply_hotfix",
}


def step(op: str, rollback: bool = True, step_id: str = "s1") -> RemediationStep:
    if op == "MANUAL":
        return RemediationStep(step_id=step_id, kind="manual_runbook", runbook_ref="RB-AZ-FAILOVER")
    return RemediationStep(
        step_id=step_id, kind="tool",
        call=ToolCall(tool_name=TOOL_FOR[op], tool_args={"scope": SCOPE}),
        rollback=ROLLBACK if rollback else None,
    )


def ctx(confidence: float, service: str = "image-resizer", **kw) -> GateContext:
    base = {"incident_id": "INC-1", "severity": "P2", "service": service, "alert_name": "Generic",
            "confidence": confidence}
    return GateContext(**(base | kw))


# (op class, confidence) -> expected gate. Low-revenue service so financial rules don't fire.
MATRIX_CASES = [
    ("READ", 0.95, "AUTO"), ("READ", 0.10, "AUTO"),
    ("SAFE_MUTATION", 0.95, "AUTO"), ("SAFE_MUTATION", 0.70, "AUTO"),
    ("SAFE_MUTATION", 0.50, "APPROVAL"), ("SAFE_MUTATION", 0.30, "ESCALATION"),
    ("DISRUPTIVE", 0.95, "AUTO"), ("DISRUPTIVE", 0.70, "APPROVAL"),
    ("DISRUPTIVE", 0.50, "APPROVAL"), ("DISRUPTIVE", 0.30, "ESCALATION"),
    ("DESTRUCTIVE", 0.95, "APPROVAL"), ("DESTRUCTIVE", 0.70, "APPROVAL"),
    ("DESTRUCTIVE", 0.50, "ESCALATION"), ("DESTRUCTIVE", 0.30, "ESCALATION"),
    ("MANUAL", 0.95, "ESCALATION"),
    # tier boundaries are half-open
    ("DISRUPTIVE", 0.80, "AUTO"), ("DISRUPTIVE", 0.7999, "APPROVAL"),
    ("SAFE_MUTATION", 0.60, "AUTO"), ("SAFE_MUTATION", 0.5999, "APPROVAL"),
]


@pytest.mark.parametrize(("op", "conf", "expected"), MATRIX_CASES)
def test_matrix(op, conf, expected):
    assert step_gate(step(op), ctx(conf)).gate == expected


@pytest.mark.parametrize(("op", "conf", "kw", "expected", "reason"), [
    # a cached RCA is capped at 0.79 -> DISRUPTIVE can't AUTO
    ("DISRUPTIVE", 0.95, {"from_cache": True}, "APPROVAL", None),
    ("SAFE_MUTATION", 0.95, {"from_cache": True}, "AUTO", None),
    ("SAFE_MUTATION", 0.95, {"guard_degraded": True}, "APPROVAL", "guard_degraded"),
    ("SAFE_MUTATION", 0.95, {"tag_mismatch": True}, "APPROVAL", "tag_mismatch"),
    ("SAFE_MUTATION", 0.95, {"policy_violation": True}, "ESCALATION", "policy_violation"),
    ("SAFE_MUTATION", 0.95, {"alert_name": "AZNetworkPartition"}, "ESCALATION", "critical_risk"),
    # financial: payment-service $12k/min. DISRUPTIVE 1 min = $12k -> APPROVAL; DESTRUCTIVE 5 min = $60k -> ESCALATION
    ("DISRUPTIVE", 0.95, {"service": "payment-service"}, "APPROVAL", "impact_usd>10000"),
    ("DESTRUCTIVE", 0.95, {"service": "payment-service"}, "ESCALATION", "impact_usd>50000"),
    # orders-service $1.5k/min: DESTRUCTIVE = $7.5k -> stays at matrix APPROVAL
    ("DESTRUCTIVE", 0.95, {"service": "orders-service"}, "APPROVAL", None),
    # overrides never lower a gate; READ ignores all of them
    ("READ", 0.95, {"policy_violation": True, "guard_degraded": True}, "AUTO", None),
])
def test_overrides(op, conf, kw, expected, reason):
    g = step_gate(step(op), ctx(conf, **kw))
    assert g.gate == expected
    if reason:
        assert reason in g.reasons


def test_destructive_without_rollback_escalates():
    g = step_gate(step("DESTRUCTIVE", rollback=False), ctx(0.95))
    assert g.gate == "ESCALATION" and "no_rollback" in g.reasons


def test_plan_gate_is_most_restrictive_step_and_sets_role():
    plan = RemediationPlan(summary="x", steps=[step("READ", step_id="a"), step("SAFE_MUTATION", step_id="b"),
                                               step("DESTRUCTIVE", step_id="c")])
    ev = evaluate_plan(plan, ctx(0.95))
    assert [s.gate for s in ev.steps] == ["AUTO", "AUTO", "APPROVAL"]
    assert ev.gate == "APPROVAL" and ev.required_role == "sre"


def test_plan_hash_changes_with_args():
    a = RemediationPlan(summary="x", steps=[step("SAFE_MUTATION")])
    b = a.model_copy(deep=True)
    b.steps[0].call.tool_args["pod_name"] = "other"
    assert evaluate_plan(a, ctx(0.9)).plan_hash != evaluate_plan(b, ctx(0.9)).plan_hash


@pytest.mark.parametrize(("kw", "expected"), [
    ({"alert_name": "AZNetworkPartition"}, "CRITICAL"),
    ({"severity": "P1", "service": "payment-service"}, "HIGH"),
    ({"severity": "P1", "service": "orders-service"}, "MEDIUM"),
    ({"severity": "P2", "service": "payment-service"}, "MEDIUM"),
    ({"severity": "P2", "service": "image-resizer"}, "LOW"),
])
def test_risk_level_is_deterministic(kw, expected):
    assert risk_level(ctx(0.9, **kw)) == expected


def test_policy_covers_every_tool():
    from typing import get_args

    from cloudscale.common.schemas import ToolName
    assert set(load_policy()["op_classes"]) == set(get_args(ToolName))
