# Prototype Scope — build spec for the 2-day solo build

`CapstoneProjectPlan_v2.md` is the **target-architecture blueprint** (what the docs describe).
This file is the **prototype** (what gets built). Anything in v2 that is not listed here is
"designed, not built" and must be labelled that way in the docs and the demo.

Rubric: `Capstone_evalutation_rubric.txt` — 4 × 25 %: Core Integration, Telemetry & Audit,
Contract Compliance, Engineering Package.

## Architecture (as built)

| Container | Image / entrypoint | Purpose |
|---|---|---|
| `orchestrator` | app image, `cloudscale.orchestrator.main:app` :8000 | REST API, LangGraph graph, HITL decisions, exec-token minting, audit verify |
| `mcp-server` | app image, `cloudscale.mcp_server.main` :8001 | FastMCP streamable-HTTP tools on a stateful simulator; token + OPA enforcement |
| `console` | app image, Streamlit :8501 | Ops console: incidents, approvals, audit verify |
| `postgres` | `pgvector/pgvector:pg16` | LangGraph checkpoints, incidents, audit chain, token ledger, semantic cache vectors |
| `redis` | `redis:7-alpine` | Circuit breaker, idempotency, token `jti`, rate counters |
| `opa` | `openpolicyagent/opa` | Default-deny tool policy |
| `ollama` (+ `ollama-init`) | `ollama/ollama` | `llama-guard3:1b` |
| `jaeger` | `jaegertracing/jaeger` | Traces via OTLP gRPC; UI :16686 (search tag `incident.id`) |
| `prometheus` | `prom/prometheus` | Metrics via native OTLP receiver :9090 (localhost only) |
| `grafana` | `grafana/grafana` | Provisioned dashboard `CloudScale Incident Platform` :3000 |

One Python package (`src/cloudscale`), one Docker image, three entrypoints.

**Accepted trade-off (document in ADR zero-trust):** the orchestrator both runs the agents and
signs exec tokens. Tokens are minted only after a recorded gate decision, and the MCP server +
OPA verify them independently. Production: a separate approval service holds the key.

## Agents (hierarchical topology)
Supervisor (deterministic conditional edges) → Triage (LLM) → Planner (LLM) → validate_plan →
HITL gate (`interrupt()`) → Executor (deterministic tool calls) → Evaluator (deterministic SLO
check + LLM summary) → close.

## Tools (7)
`get_metrics`, `fetch_k8s_logs`, `get_deployment_status` (READ) · `clear_pod_cache`
(SAFE_MUTATION) · `restart_service` (DISRUPTIVE) · `apply_hotfix`, `rollback_deployment`
(DESTRUCTIVE). All take the same `Scope` (namespace, cloud_provider).

## HITL gate
Gates: AUTO / APPROVAL / ESCALATION (REVIEW is blueprint-only).
Triggers, most restrictive wins:
- op-class × composite confidence matrix (v2 §9.2, REVIEW cells → APPROVAL)
- **financial impact**: `estimated_impact_usd` > $10k → APPROVAL, > $50k → ESCALATION
- **policy violation**: OPA deny or plan validation failure → ESCALATION
- `risk_level` CRITICAL (derived deterministically from severity + service tier) → ESCALATION
- MANUAL step → ESCALATION; DESTRUCTIVE without rollback → ESCALATION

## Scenarios (6)
| # | Scenario | Expected |
|---|---|---|
| 1 | OOMKilled `orders-service` pod ($1.5k/min) → hotfix memory + restart | APPROVAL → RESOLVED (fault-injection variant → breaker OPEN → rollback) |
| 2 | Cache bloat → `clear_pod_cache` | AUTO → RESOLVED; re-run → semantic cache hit |
| 3 | Payment pool change with impact > $50k | ESCALATION (financial threshold) |
| 4 | AZ network partition → manual runbook | ESCALATION (manual) |
| 5 | Direct prompt injection in alert payload | QUARANTINED |
| 6 | **Indirect** prompt injection inside fetched pod logs | QUARANTINED |

Scenario files hold `incident` (sent to the pipeline) separately from `simulation` and
`ground_truth` (never sent to an LLM).

## Must-fix design points carried from the gap analysis
- Idempotency key = `sha256(incident_id | plan_hash | step_id | args_hash)`.
- No retries on mutating tools (READ only, tenacity).
- Every tool result is scrubbed + guarded before it enters a prompt (indirect injection).
- IP pseudonym mapping is per incident, persisted in state.
- Simulator state keyed by `incident_id`.
- Gate node handles APPROVED / REJECTED / ESCALATE and has no side effects before `interrupt()`.
- Exec tokens are requested for every approved path, not only AUTO.
- Audit hash computed inside the insert transaction (client sets `ts`, `seq` from the locked tail).
- Startup sweeper resumes non-terminal LangGraph threads.
- Manual steps: approval = acknowledged handoff; incident ends `ESCALATED` with the runbook ref.

## Guard strategy (decided 2026-09-26 — dev host is slow at inference)
The dev machine is a VMware VM running Docker in WSL2 (nested virtualization): LlamaGuard 1B
takes 15-45 s per warm call, ~160 s cold. So (`common/safety/`):
1. STRONG heuristic signature → QUARANTINE, no model call.
2. No cues → pass, no model call (most log text).
3. Cues → LlamaGuard on ≤ 3 windows of ≤ 600 chars around the cues (custom S1-S4 categories, raw prompt).
4. Timeout (`GUARD_TIMEOUT_S`, 20 s in compose) or error → `degraded=True` → gate floor APPROVAL.
   Exception: a high-risk cue (exfiltration, pipe-to-shell, secret request) with no model verdict
   **fails closed → QUARANTINE** (this is what catches scenario 6 on the dev host).
5. Verdicts cached in Redis by segment hash; `scripts/guard_probe.py` warms the cache before a demo.
Ollama runs with `OLLAMA_KEEP_ALIVE=-1` so the model stays loaded.

## LLM
`claude-sonnet-5` (P1 triage/planning) ↔ `claude-haiku-4-5` (P2, summaries). Failover
Sonnet → Haiku → scripted. `LLM_MODE=scripted|live`: scripted returns canned per-scenario
responses (dev mode and offline demo fallback).

## Observability
OTel traces across API → graph nodes → LLM calls → MCP calls (traceparent propagated).
Metrics: `incident.task.latency` (LPT), `llm.tokens` (TCR), `cache.lookups` (CHR),
`mcp.tool.calls` (TFR), `llm.cost.usd`. Grafana dashboard provisioned from the repo.

## Docs (Engineering Package)
- 4 diagrams: C4 context/container, component (orchestrator), agent sequence, HITL state machine
  (Mermaid → Excalidraw import → `.excalidraw` + PNG)
- ~7 concise ADRs: topology, protocol (MCP vs UTCP/A2A/A2UI), state & semantic caching,
  HITL gate, guardrails & zero trust, model routing, cloud deployment target
- DDD context map, NFR matrix, OWASP LLM Top 10 (2025) table
- TCO/ROI `.md` + `.xlsx` with token-optimization comparison (all-Sonnet vs routed vs routed+cache)

## Not built (blueprint only)
React frontend, api-gateway as separate service, REVIEW timer/Hold, approval promoter,
approve-with-modifications, Helm/CD, mTLS, A2A/UTCP/A2UI, multi-provider, eval harness,
`scale_pods`, Datadog normalizer, record/replay fixtures.

## Schedule & cut order
See the 2-day table in the gap analysis. Cut order if behind: Streamlit (use Swagger `/docs`)
→ live LLM (scripted only) → semantic cache (exact match) → OPA (in-code policy).

## Milestones
- [x] M0 Scaffold: compose stack healthy, package installs, health endpoints
- [x] M1 Shared schemas + gate policy + table tests; MCP server + simulator + token/OPA
      (78 pytest + 17 `opa test` + `scripts/mcp_smoke.py` 11/11 live)
- [x] M2 Graph in scripted mode — scenario 2 AUTO end to end (vertical slice)
      (11 graph tests; all 6 scenarios verified live via `scripts/run_scenario.py`)
- [x] M3 HITL API + console + exec tokens + audit chain — scenario 1 approved
      (105 tests incl. 8 on real Postgres; live: approve via API -> RESOLVED, 24 audit rows, chain verifies,
      UPDATE/DELETE refused; startup sweeper resumes in-flight incidents)
- [~] M4 Live LLM router + fallback + semantic cache + ledger
      (built; 123 tests; live stack: s02 re-run served from pgvector cache. PENDING: one live Claude run —
      the key in .env was rejected with 401, so the orchestrator runs scripted)
- [x] M5 Guard layers + scrubber + validators + breaker/fault injection — scenarios 5/6
      (152 tests; live: injected restart fault -> breaker OPEN -> hotfix rolled back -> ESCALATED;
      pre-flight blocks plans whose tools have an OPEN breaker; `scripts/fault.py inject|status|reset`)
- [x] M6 OTel traces + metrics + Grafana
      (155 tests; live: one trace per run spanning orchestrator -> mcp-server via _meta traceparent;
      7 metrics in Prometheus; LPT/TCR/CHR/TFR via /metrics/live + console Metrics page + Grafana.
      otel-lgtm replaced by jaeger+prometheus+grafana: it idled at 100-220% CPU on the dev VM)
- [x] M7 Docs package
      (4 Mermaid diagrams, 7 ADRs, DDD map, NFR matrix, OWASP 2025 table, TCO/ROI md + xlsx verified
      against Python, README. Manual step left: import diagrams into Excalidraw and export .excalidraw/PNG)
- [ ] M8 Scenario run, demo script, rehearsal
