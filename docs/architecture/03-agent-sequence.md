# Agent sequence: an approved destructive fix (scenario s01)

The OOMKilled `orders-service` incident: a memory hotfix plus restart. The hotfix is destructive, so the gate stops for an SRE.

```mermaid
sequenceDiagram
    autonumber
    actor SRE
    participant O as Orchestrator
    participant T as Triage agent
    participant P as Planner agent
    participant G as HITL gate
    participant X as Executor
    participant M as MCP server
    participant OPA
    participant A as Audit log

    O->>O: Intake: scrub secrets, pseudonymize IPs, guard check
    O->>T: incident (untrusted data in incident_data tags)
    T->>M: get_metrics / fetch_k8s_logs / get_deployment_status (READ token)
    M->>OPA: allow? (scope, token type, op class)
    OPA-->>M: allow
    M-->>T: metrics, logs, deployment revision 14
    T->>T: guard fetched logs (indirect injection)<br/>evidence score from runbook catalogue
    T->>P: root cause, confidence = min(LLM 0.90, evidence 0.90)
    P-->>G: plan: apply_hotfix (memory 2Gi, rollback to rev 14) + restart
    G->>G: DESTRUCTIVE at 0.90 → APPROVAL, impact $7.5k
    G-->>A: HITL_REQUEST
    G--)SRE: interrupt(): waiting for role sre
    SRE->>O: Approve (plan_hash, session = sre1)
    O-->>A: HITL_DECISION by human:sre1
    O->>X: resume from checkpoint
    X->>M: apply_hotfix + single-use exec token (step, args hash, approver)
    M->>OPA: allow? (args hash matches, human approval present)
    OPA-->>M: allow
    M-->>A: POLICY_DECISION, TOOL_CALL_FINISHED
    X->>M: restart_service + its own exec token
    X->>M: get_metrics every 3 s (Evaluator)
    M-->>X: memory 55 %, error rate 0.4 %, p99 320 ms
    X-->>A: VERIFICATION → RESOLVED
```

If the restart fails instead (`scripts/fault.py inject restart_service 1`), the circuit breaker opens. The Executor then rolls back the completed hotfix with its pre-approved `rollback_deployment` token, and the incident ends `ESCALATED`.
