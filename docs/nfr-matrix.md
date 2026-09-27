# NFR matrix

**Rubric:** Contract Compliance (NFRs), Engineering Package (NFR matrix)

"Measured" values come from the prototype on the dev machine: a VMware VM running Docker inside WSL2 (nested virtualization, 4 vCPU). Expect laptops to be several times faster. "Verified by" names the test or command that shows it.

| NFR | Target | Measured in the prototype | Verified by |
|---|---|---|---|
| **Latency: AUTO incident end to end** | < 3 min | Live OpenAI: 15–16 s for s02 (cache bloat); 11–13 s on a semantic-cache hit | `scripts/rehearse.py` |
| **Latency: per agent task (LPT p95)** | Triage < 30 s (P1) | Live OpenAI: triage ≈ 5 s on `gpt-5.4`, planning ≈ 6–15 s; Llama Guard adds up to the 20 s guard budget on this host | Grafana LPT panel, `/metrics/live` |
| **Human latency** | Measured, not bounded | HITL wait p50 ≈ 3 s in test runs | `hitl.wait.seconds` histogram |
| **Safety: no destructive change without a human** | 0 exceptions | Enforced twice: the gate never AUTOs DESTRUCTIVE, and OPA denies destructive calls without a human approver | `tests/test_gate.py`, `opa test` (17), `scripts/mcp_smoke.py` |
| **Security: prompt injection** | Direct and indirect caught | s05 (alert) and s06 (fetched logs) both QUARANTINED; benign infra logs pass | `tests/test_guard.py`, `tests/test_graph.py` |
| **Security: zero trust** | Every tool call authenticated, scoped, authorized | 11/11 live cases: forged, cross-namespace, cross-cloud, read-token mutation, AUTO destructive, class escalation, changed args, replay | `scripts/mcp_smoke.py` |
| **Integrity: audit** | 100 % verifiable, tamper-evident | Chain verifies; UPDATE/DELETE refused for the writer role and the owner; tampering pinpointed to the row | `tests/test_audit.py`, `GET /audit/verify` |
| **Resilience: tool failure** | Contained and reversible | Injected fault → breaker OPEN → completed hotfix rolled back → ESCALATED; plans with an open breaker don't start | `tests/test_resilience_safety.py`, `scripts/fault.py` |
| **Resilience: crash** | Resume without re-executing | Startup sweeper resumes from the checkpoint; replays return the stored result (no second execution) | `tests/test_graph.py::test_restart_sweeper…`, `tests/test_mcp_server.py` |
| **Resilience: LLM outage** | Degrade, don't stop | Deep → fast → scripted, audited as `PROVIDER_FAILOVER` (exercised live by a rejected API key); 0 failovers across 41 live OpenAI incidents | `tests/test_claude_llm.py`, `tests/test_openai_llm.py` |
| **Cost** | ≤ $0.10 per incident average; hard cap $1.00 | **Measured on OpenAI: $0.0227 per P1, $0.0055 per P2, $0.0008 per cache hit; $0.0101 blended** (41 live incidents); cap enforced in code | Token ledger (`/ledger/summary`), `tests/test_claude_llm.py::test_budget_cap…` |
| **Observability** | Every tool call traced + audited | One trace per run across orchestrator and MCP server; POLICY_DECISION row per call | Jaeger `incident.id` search, audit log |
| **Data protection** | No secrets or raw IPs reach the LLM | Scrubber on alert and fetched logs; IPs pseudonymized; the LLM context contains no simulation data or answers | `tests/test_resilience_safety.py`, `tests/test_graph.py::test_scenario_answers_never_reach_the_llm` |
| **Scalability** | 5 concurrent incidents locally; HPA in cloud | Semaphore of 5; a slot is released while an incident waits on a human | `orchestrator/service.py` |
| **Portability** | AWS + Azure | Scenarios run on both clouds; scope rules are cloud-aware | Scenarios s02/s06 (Azure), others (AWS) |

**Test totals:** 175 pytest tests (including 14 against real Postgres/pgvector) + 17 OPA policy tests + 11 live zero-trust cases + the live rehearsal (`scripts/rehearse.py --crash`: 12/12 on OpenAI, twice in a row).
