# ADR-004: Per-step HITL gate from one policy file

**Status:** Accepted · **Rubric:** Contract Compliance (HITL decision gates)

## Context
The platform must act autonomously on safe fixes and stop for humans on risky ones. "Risky" has several dimensions: what the operation does, how sure we are, what it costs if wrong, and whether a policy was violated.

## Decision
- **Gate per step, plan gated at its most restrictive step.** Each step is classified by the server, never by the LLM: READ, SAFE_MUTATION, DISRUPTIVE, DESTRUCTIVE or MANUAL.
- **Base gate:** op class × **composite confidence** = `min(LLM confidence, deterministic evidence score)`. The evidence score comes from a runbook catalogue: signature matched and metrics corroborate → 0.90, signature only → 0.65, no match → 0.35.
- **Escalation triggers the rubric names**, which can only raise the gate:
  - **financial value:** revenue per minute × disruption minutes, above $10k → APPROVAL, above $50k → ESCALATION
  - **policy violations:** an invalid plan (twice) or an OPA denial → ESCALATION
  - **confidence:** below 0.60 → at least APPROVAL
  - also: critical alerts, a degraded guard, an LLM/op-class tag mismatch, a destructive step with no rollback, and a cached diagnosis (capped at 0.79)
- **Mechanics:** LangGraph `interrupt()`. The gate node has no side effects before the interrupt, because LangGraph re-runs it on resume. Decisions are bound to the plan version (`plan_hash`), and the approver's identity comes from the session. Roles: `sre` for APPROVAL, `senior_sre` for ESCALATION.
- **One source of truth:** `gate_policy.yaml` feeds both the gate and OPA's data (`scripts/gen_opa_data.py --check`).

## Alternatives considered
- **Plan-level gate on LLM confidence (v1):** let destructive operations auto-run at high self-reported confidence.
- **Timer-based auto-approve:** an unattended destructive change is not acceptable. Our blueprint's REVIEW timer is limited to disruptive steps and wasn't built in the prototype.

## Consequences
- The jury can read every decision's reasons: the gate records, per step, e.g. `matrix:DESTRUCTIVE@0.86, impact_usd>50000`.
- OPA enforces the destructive rule a second time on the tool side: no human approver, no destructive call.

**In the code:** `common/gate.py`, `common/gate_policy.yaml`, `tests/test_gate.py` (every cell and override), `orchestrator/api.py` (decisions).
