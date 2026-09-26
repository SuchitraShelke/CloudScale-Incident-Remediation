"""HITL gate: pure, deterministic, safe to re-run when LangGraph resumes a node.

Per step: op-class x confidence matrix, then overrides that can only RAISE the gate:
financial impact, critical risk, low-confidence floor, degraded guard, tag mismatch, policy
violation, destructive-without-rollback, manual step. Plan gate = most restrictive step gate.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from cloudscale.common.canonical import plan_hash
from cloudscale.common.schemas import (
    Gate,
    GateContext,
    GateEvaluation,
    OpClass,
    RemediationPlan,
    RemediationStep,
    RiskLevel,
    StepGate,
)

POLICY_PATH = Path(__file__).with_name("gate_policy.yaml")
ORDER: list[Gate] = ["AUTO", "APPROVAL", "ESCALATION"]


@lru_cache
def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _max(a: Gate, b: Gate) -> Gate:
    return a if ORDER.index(a) >= ORDER.index(b) else b


def classify(step: RemediationStep, policy: dict | None = None) -> OpClass:
    policy = policy or load_policy()
    if step.kind == "manual_runbook":
        return "MANUAL"
    return policy["op_classes"][step.call.tool_name]


def tier(confidence: float, policy: dict) -> str:
    t = policy["confidence_tiers"]
    if confidence >= t["high"]:
        return "high"
    if confidence >= t["medium"]:
        return "medium"
    if confidence >= t["low"]:
        return "low"
    return "very_low"


def risk_level(ctx: GateContext, policy: dict | None = None) -> RiskLevel:
    policy = policy or load_policy()
    if ctx.alert_name in policy["risk"]["critical_alerts"]:
        return "CRITICAL"
    tier1 = ctx.service in policy["risk"]["tier1_services"]
    if ctx.severity == "P1":
        return "HIGH" if tier1 else "MEDIUM"
    return "MEDIUM" if tier1 else "LOW"


def impact_usd(op: OpClass, service: str, policy: dict) -> float:
    f = policy["financial"]
    rate = f["revenue_per_minute_usd"].get(service, f["revenue_per_minute_usd"]["default"])
    return float(rate * f["disruption_minutes"][op])


def step_gate(step: RemediationStep, ctx: GateContext, policy: dict | None = None) -> StepGate:
    policy = policy or load_policy()
    op = classify(step, policy)
    impact = impact_usd(op, ctx.service, policy)

    def result(gate: Gate, reasons: list[str]) -> StepGate:
        return StepGate(step_id=step.step_id, op_class=op, gate=gate, impact_usd=impact, reasons=reasons)

    if op == "READ":
        return result("AUTO", ["read_only"])
    if op == "MANUAL":
        return result("ESCALATION", ["manual_runbook"])

    conf = min(ctx.confidence, policy["cache_confidence_cap"]) if ctx.from_cache else ctx.confidence
    gate: Gate = policy["matrix"][op][tier(conf, policy)]
    reasons = [f"matrix:{op}@{conf:.2f}"]

    def raise_to(floor: Gate, reason: str) -> None:
        nonlocal gate
        gate = _max(gate, floor)
        reasons.append(reason)

    fin = policy["financial"]
    if impact > fin["escalation_above_usd"]:
        raise_to("ESCALATION", f"impact_usd>{fin['escalation_above_usd']}")
    elif impact > fin["approval_above_usd"]:
        raise_to("APPROVAL", f"impact_usd>{fin['approval_above_usd']}")
    if risk_level(ctx, policy) == "CRITICAL":
        raise_to("ESCALATION", "critical_risk")
    if conf < policy["confidence_tiers"]["medium"]:
        raise_to("APPROVAL", "low_confidence_floor")
    if ctx.guard_degraded:
        raise_to("APPROVAL", "guard_degraded")
    if ctx.tag_mismatch:
        raise_to("APPROVAL", "tag_mismatch")
    if ctx.policy_violation:
        raise_to("ESCALATION", "policy_violation")
    if op == "DESTRUCTIVE" and step.rollback is None:
        raise_to("ESCALATION", "no_rollback")
    return result(gate, reasons)


def evaluate_plan(plan: RemediationPlan, ctx: GateContext, policy: dict | None = None) -> GateEvaluation:
    policy = policy or load_policy()
    steps = [step_gate(s, ctx, policy) for s in plan.steps]
    gate = max((s.gate for s in steps), key=ORDER.index)
    return GateEvaluation(
        plan_hash=plan_hash(plan),
        gate=gate,
        risk_level=risk_level(ctx, policy),
        required_role={"APPROVAL": "sre", "ESCALATION": "senior_sre"}.get(gate),
        steps=steps,
    )
