# IT Infrastructure Incident Remediation & Auto-Healing Platform
### CloudScale Global Networks — Capstone Implementation Plan (v2, corrected)
**Team:** 10 Engineers | **Duration:** 12 Hours | **Deployment:** Local (Docker Compose), cloud as validated reference

> This rewrites the SASVA-generated v1 plan. It fixes every gap found in the v1 review: runtime-breaking defects, internal contradictions, missing design areas, and financial errors. **Appendix A** maps each v1 issue to where it is fixed. Items are tagged **[MVP]** (must ship by the H8 feature freeze) or **[STRETCH]** (only if MVP is green).

---

## 📋 Executive Summary

The platform takes in P1/P2 infrastructure incidents and runs them through a LangGraph pipeline: a **deterministic supervisor** plus **3 specialist LLM agents** (Triage → Planner → Executor/Verifier). Every mutating operation passes through a **per-step, operation-class × confidence HITL gate** built on LangGraph `interrupt()`. Tools are exposed through a real **MCP server (streamable HTTP)**. Tools only run with **gateway-signed, per-incident, per-step tokens**, and **OPA** evaluates every call with default-deny. The system is observable end to end (OTLP → Jaeger + Prometheus) and every decision is recorded in a **hash-chained, append-only Postgres audit log**.

What changed from v1, in brief:
- **Safety core fixed.** Destructive operations can never auto-run on confidence alone. REVIEW is a real interruptible pause, not `sleep(60)`. The agent can't authorize itself, because only the gateway mints execution tokens.
- **Stack made buildable.** Current Claude models (`claude-sonnet-5`, `claude-haiku-4-5`). Current LangGraph with the Postgres checkpointer. Real MCP transport. A hand-written circuit breaker, since `tenacity` has none. OTLP exporter. Native WebSocket on the frontend.
- **Scope made deliverable in 12 hours.** Explicit MVP cut line, 7 workstreams with owners, interface contracts frozen at H1, an offline replay mode for the demo, and a test and eval suite.
- **Numbers made consistent.** One count for every artifact. The token economics and TCO are recomputed so both use the same inputs (see §17).

**Jury Score Mapping** (full traceability matrix in §18):
| Rubric Parameter | Key Deliverables |
|---|---|
| Core Integration Path (25%) | LangGraph supervisor graph with Postgres checkpointer, MCP streamable-HTTP server (7 tools), circuit breaker, crash-safe resume |
| Telemetry & Audit (25%) | OTLP traces (Jaeger), 7 OTEL metrics incl. the 4 jury metrics (Prometheus), hash-chained audit log with `/audit/verify` |
| Contract Compliance (25%) | Per-step HITL gate (single policy file shared by gate + OPA), LlamaGuard + deterministic validators, signed scoped tokens, DDD contexts mapped to code |
| Engineering Package (25%) | Runnable repo + tests + eval harness, 7 Excalidraw diagrams, 12 ADRs, OWASP LLM Top 10 (2025) threat model, 3-year TCO/ROI |

---

## 📊 Project Metrics (single source of truth — every other section uses these numbers)

| Metric | Value |
|---|---|
| Team | 10 engineers in 7 workstreams (§3) |
| Implementation window | 12 hours; feature freeze H8; demo-ready H11 |
| Application services | 4 — `frontend`, `api-gateway`, `agent-core`, `mcp-server` |
| Infrastructure containers | 7 — `postgres` (pgvector), `redis`, `opa`, `ollama`, `otel-collector`, `jaeger`, `prometheus` (+1 one-shot `ollama-init`) |
| Agents | 1 deterministic supervisor (code, no LLM) + 3 specialist LLM agents (Triage, Planner, Executor/Verifier) |
| MCP tools | 7 — `fetch_k8s_logs`, `get_metrics`, `clear_pod_cache`, `restart_service`, `scale_pods`, `apply_hotfix`, `rollback_deployment` |
| Manual (non-tool) actions | `manual_runbook` step type (cluster failover, node repair) → always ESCALATION |
| Scenarios | 9 — 8 operational (AWS + Azure, Prometheus + Datadog) + 1 adversarial (prompt injection) |
| HITL gate types | 4 — AUTO, REVIEW, APPROVAL, ESCALATION |
| Content-safety layers | 2 — LlamaGuard 3 (semantic) + deterministic validators (schema/regex/allowlist) |
| Zero-trust layers | 3 — Identity (signed scoped tokens), Policy (OPA default-deny), Network (segmented Docker networks; mTLS = [STRETCH]) |
| OTEL metrics | 7 (4 jury: LPT, TCR, CHR, TFR + cost, HITL wait, circuit state) |
| ADRs | 12 (ADR-001 … ADR-012, list in §23) |
| Excalidraw diagrams | 7 (list in §23) |
| Threat model | OWASP Top 10 for LLM Applications **2025** |
| Deployment targets | Docker Compose (built & demoed) + AWS EKS (primary reference) + Azure AKS (values overlay) — Helm lint/kubeconform-validated, **not deployed** in the 12 h |
| Target runtime | AUTO scenario end-to-end < 3 min; HITL scenarios < 3 min excluding human think time |
| Blended LLM cost / incident | ≈ $0.065 (§12) |

---

## 1. Scope & MoSCoW Cut Line

| Priority | Items |
|---|---|
| **MVP (must, by H8)** | Compose stack; 7 MCP tools on stateful simulator; 9 scenarios with ground truth; supervisor graph + 3 agents; Postgres checkpointer; per-step HITL gate incl. REVIEW timer & escalation promotion; gateway-minted read/exec tokens; OPA policy + `opa test`; LlamaGuard (1B) + validators + scrubber; circuit breaker + idempotency; hash-chained audit + verify; 7 metrics + traces; token ledger; exact-match cache; model routing Sonnet 5 ↔ Haiku 4.5; replay mode; 5 dashboard pages; unit + integration tests; 12 ADRs, 7 diagrams, TCO |
| **Should (H8–H10 if green)** | Tier-2 pgvector similarity cache; Alertmanager webhook adapter; A2A AgentCards + `message/send` for Triage; Playwright smoke test; Helm lint in CI |
| **Stretch** | mTLS agent-core↔mcp-server; multi-provider failover (ADR-012 only otherwise); Opus 5 second opinion on CRITICAL; Datadog webhook; Grafana dashboards; full A2A task lifecycle |
| **Won't (documented only)** | UTCP implementation (ADR-008); A2UI implementation (ADR-009); real cluster integration |

**Rule:** At H8 the integration lead freezes features. Anything not MVP-green at H8 is either dropped or shown as "designed, not built" in ADRs. Nothing half-built goes into the demo.

---

## 2. Delivery Plan — Timeline

| Hour | Activity | Exit criterion |
|---|---|---|
| **H0–H1** | All hands: repo scaffold, lockfiles, compose skeleton, **freeze contracts** (`IncidentState`, incident JSON schema, REST OpenAPI, WS event schema, MCP tool schemas, gate policy YAML, audit event schema) in `libs/cloudscale-common` | Contracts merged; `docker compose up` shows all containers healthy (stubs) |
| **H1–H5** | Parallel build per workstream against contracts, with stubs/fakes | Each WS: unit tests green |
| **H5** | **Integration checkpoint 1** — scenario 7 (AUTO) end-to-end in replay mode | Incident reaches RESOLVED; trace visible in Jaeger; audit chain verifies |
| **H5–H8** | Build remaining MVP: HITL paths, tokens/OPA, guard, breaker, frontend flows | **Checkpoint 2 (H8, feature freeze)** — all 9 scenarios pass integration suite in replay mode |
| **H8–H10** | Should-items, bug fixing, live-mode eval run, record replay fixtures | Eval report produced; replay fixtures committed |
| **H10–H11** | Demo rehearsal ×2 (live + replay fallback), docs finalization | Rehearsal < 12 min, zero manual fixes |
| **H11–H12** | Buffer: fixes only, final README, tag release | `v1.0` tag |

---

## 3. Team & Workstreams (10 engineers)

| WS | Owner(s) | Scope | Key deliverables |
|---|---|---|---|
| WS1 Platform & Infra | 2 | Compose, networks, Postgres/Redis/OPA/Ollama/OTel/Jaeger/Prometheus, CI, Helm chart + lint | `docker-compose.yml`, `ci.yml`, `helm/` |
| WS2 Agent Core | 2 | Graph, supervisor, 3 agents, prompts, structured outputs, model router, token ledger, cache | `agent-core/` |
| WS3 MCP & Simulation | 1 | 7 tools, stateful simulator, scenario files + ground truth, fault injection, idempotency | `mcp-server/`, `mock-data/` |
| WS4 Safety & Zero-Trust | 1 | Scrubber, normalizer, LlamaGuard client, validators, token minting/verification, OPA Rego + tests, red-team suite | `libs/.../safety`, `opa/`, `tests/redteam/` |
| WS5 Gateway, Audit & Telemetry | 1 | REST/WS, auth/RBAC, HITL endpoints + deadline worker, audit writer/verify, metrics API | `api-gateway/` |
| WS6 Frontend | 2 | 5 pages, HITL modal, ledger/cache/router panels, WS client | `frontend/` |
| WS7 Docs, Finance & QA lead | 1 | 12 ADRs, 7 diagrams, TCO, threat model, eval harness, demo script, **integration lead / freeze authority** | `docs/`, `scripts/eval_scenarios.py` |

Every workstream codes against the H1 contracts. Changing a contract after H1 needs approval from both the WS7 lead and the consuming workstream.

---

## 4. System Architecture

### 4.1 Container view (C4 Level 2 — drawn in `c4-container.excalidraw`)

```
                           ┌─────────────────────┐
  SRE (browser) ──HTTPS──▶ │ frontend :3000      │  (Vite build served by nginx; TLS at gateway in cloud)
                           └─────────┬───────────┘
                                     │ REST + native WebSocket (session cookie)
                           ┌─────────▼───────────┐       net: edge, core, data
                           │ api-gateway :8000   │  auth/RBAC · incidents · HITL decisions
                           │                     │  token minting (Ed25519) · REVIEW deadline worker
                           │                     │  audit writer/verify · metrics API (PromQL)
                           └──┬──────────────┬───┘
            internal REST     │              │ LISTEN/NOTIFY + Redis pub/sub (events)
                   ┌──────────▼─────────┐    │
                   │ agent-core :8003   │────┼──── ollama :11434 (llama-guard3:1b)   net: core
                   │ LangGraph graph    │    │
                   │ + Postgres ckpt    │    │
                   └──────────┬─────────┘    │
            MCP streamable    │ HTTP + Bearer token          net: tools
                   ┌──────────▼─────────┐        ┌───────────┐
                   │ mcp-server :8001   │──────▶ │ opa :8181 │   net: tools
                   │ 7 tools + simulator│        └───────────┘
                   └──────────┬─────────┘
                              │                                     net: data
          ┌───────────────────┴──────────────┬──────────────────┐
   ┌──────▼───────┐                  ┌───────▼──────┐   ┌───────▼────────┐
   │ postgres     │ checkpoints,     │ redis :6379  │   │ otel-collector │──▶ jaeger :16686 (traces)
   │ (pgvector)   │ audit, incidents,│ exact cache, │   │ :4317          │──▶ prometheus :9090 (metrics)
   │ :5432        │ ledger, vectors  │ rate/idem/jti│   └────────────────┘
   └──────────────┘                  │ breaker, zset│
                                     └──────────────┘
```

**Network segmentation [MVP]** (these are Docker networks; the Helm chart has matching Kubernetes NetworkPolicies):
| Network | Members | Purpose |
|---|---|---|
| `edge` | frontend, api-gateway | Only network with published ports for UI/API |
| `core` | api-gateway, agent-core, ollama, otel-collector | Orchestration traffic |
| `tools` | agent-core, mcp-server, opa | Tool execution. The gateway and frontend **cannot reach** the MCP server |
| `data` | api-gateway, agent-core, mcp-server, postgres, redis, otel-collector | State. Each service has its own DB role (§14) |

Only `frontend:3000`, `api-gateway:8000`, `jaeger:16686` and `prometheus:9090` are published to the host.

**Host requirements:** 16 GB RAM and 4+ vCPU. `llama-guard3:1b` uses about 2 GB RAM and returns in about 0.3–1.5 s on CPU (measured at H1; see risk R3 in §24).

### 4.2 Agent pipeline — LangGraph graph (supervisor = conditional edges in code)

```
START
  │
  ▼
intake ──(schema invalid)──────────────────────────────▶ reject (400, audit)
  │  scrub → normalize → guard check (heuristic + LlamaGuard)
  ├──(injection detected)──▶ quarantine ──▶ escalate
  ▼
cache_lookup (tier1 exact → tier2 pgvector [SHOULD])
  │
  ▼
triage (Sonnet 5 | Haiku 4.5; calls fetch_k8s_logs, get_metrics with READ token)
  │
  ▼
planner (structured RemediationPlan + recovery artifacts)
  │
  ▼
validate_plan (Pydantic, tool arg schemas, validators, server-side op-class, tag-mismatch check)
  ├──(invalid ×2)──▶ escalate
  ▼
hitl_gate (pure evaluation → interrupt() unless AUTO)
  ├── AUTO / REVIEW(auto-proceeded) / APPROVED ──▶ execute
  ├── REJECTED ──▶ close_rejected
  └── ESCALATED-unresolved stays interrupted; senior decision resumes it
  ▼
execute (per step: exec token → breaker → MCP call → audit; on failure → pre-approved rollback)
  │
  ▼
verify (deterministic SLO check over get_metrics polling; LLM writes summary only)
  │
  ▼
close (RESOLVED | PARTIALLY_RESOLVED | ESCALATED) → cache feedback → ledger finalize
```

**Incident status machine** (the dashboard chips use exactly these values):
`RECEIVED → TRIAGING → PLANNING → AWAITING_REVIEW | AWAITING_APPROVAL | AWAITING_ESCALATION → EXECUTING → VERIFYING → RESOLVED | PARTIALLY_RESOLVED | ESCALATED`, plus the terminal states `REJECTED`, `QUARANTINED`, `FAILED`, `DUPLICATE`.

**Crash safety:** Every node boundary is checkpointed in Postgres (`AsyncPostgresSaver`, `thread_id = incident_id`). If agent-core restarts, in-flight graphs resume from their last checkpoint. A re-run `execute` step is safe because every tool call carries `idempotency_key = step_id` (§8.3).

**Concurrency:** At most 5 incidents run at once (semaphore). Ingestion is idempotent: `incident_id` has a UNIQUE constraint. An incident whose fingerprint matches an open incident from the last 10 minutes is linked to it as `DUPLICATE`.

---

## 5. Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| LLM | Anthropic Claude — `claude-sonnet-5` (deep RCA/planning), `claude-haiku-4-5` (P2 triage/planning, verification summaries) | Structured outputs (`output_config.format`) for JSON. Sonnet 5 uses adaptive thinking and rejects `temperature`, so determinism for demos comes from **replay mode**, not sampling settings |
| Agent framework | LangGraph (current 1.x) + `langgraph-checkpoint-postgres` | `interrupt()` / `Command(resume=…)` |
| API | FastAPI + uvicorn | Native WebSocket |
| MCP | `mcp` Python SDK (FastMCP, **streamable HTTP** transport) | Real JSON-RPC MCP, not ad-hoc REST |
| DB | PostgreSQL 16 + pgvector | Checkpoints, audit, incidents, ledger, semantic vectors |
| Cache / coordination | Redis 7 | Exact cache, rate limits, idempotency, JWT `jti`, breaker state, REVIEW deadlines, pub/sub |
| Policy | OPA (`openpolicyagent/opa`, Rego v1) | `opa test` in CI |
| Content safety | Ollama + `llama-guard3:1b` (upgrade to `llama-guard3:8b` if hardware allows) | Pulled by `ollama-init` |
| Embeddings | `fastembed` (`BAAI/bge-small-en-v1.5`, 384-dim, local) | No second provider key needed |
| Observability | OpenTelemetry SDK + **OTLP exporter** → otel-collector → Jaeger (traces) + Prometheus (metrics) | The Jaeger-native exporter is deprecated and not used |
| Resilience | Custom Redis-backed circuit breaker + `tenacity` for retries | `tenacity` provides retries only |
| Tokens | PyJWT with **EdDSA (Ed25519)** | Gateway signs; MCP server verifies with the public key only |
| Frontend | React 18 + TypeScript + Vite + Tailwind + TanStack Query + Zustand + react-router | Native `WebSocket` (no socket.io) |

### 5.1 Dependency management (fixes the v1 version-pin errors)

The v1 plan hand-picked exact versions, and several combinations were incompatible (for example LangGraph 0.2.28 has no `interrupt()`; the Jaeger exporter doesn't match OTel SDK 1.28). v2 declares **ranges** in `pyproject.toml` and generates a **hash-pinned lockfile at H0** (`uv lock` → `uv export --format requirements-txt --hashes`). The lockfile is what gets installed; `pip-audit` scans it in CI.

```toml
# agent-core/pyproject.toml (excerpt; gateway/mcp-server analogous)
dependencies = [
  "anthropic",                       # latest release at H0
  "langgraph>=1.0,<2",
  "langgraph-checkpoint-postgres",
  "psycopg[binary,pool]>=3.2",
  "fastapi", "uvicorn[standard]",
  "mcp>=1.9",                        # streamable HTTP client/server
  "redis>=5",
  "pydantic>=2.9", "pydantic-settings",
  "httpx", "tenacity>=8",
  "pyjwt[crypto]>=2.9",
  "fastembed", "pgvector",
  "opentelemetry-sdk", "opentelemetry-exporter-otlp",
  "opentelemetry-instrumentation-fastapi", "opentelemetry-instrumentation-httpx",
  "cloudscale-common @ file:../libs/cloudscale-common",
]
[dependency-groups]
dev = ["pytest", "pytest-asyncio", "respx", "ruff", "mypy", "pip-audit"]
```
`guardrails-ai` is **optional**: the Layer-2 validators are plain Python with a Guardrails-compatible interface, so a failed install at H0 can't block the build. The local package is named `safety`, not `guardrails`, so it can't shadow the `guardrails` import name.

```json
// frontend/package.json (excerpt — exact versions come from package-lock.json at H0)
{
  "dependencies": {
    "react": "^18.3.1", "react-dom": "^18.3.1", "react-router-dom": "^6.28.0",
    "@tanstack/react-query": "^5.59.0", "zustand": "^5.0.1",
    "recharts": "^2.13.0", "lucide-react": "^0.453.0",
    "@radix-ui/react-dialog": "^1.1.2", "react-hot-toast": "^2.4.1"
  },
  "devDependencies": {
    "typescript": "^5.6.3", "vite": "^5.4.0", "@vitejs/plugin-react": "^4.3.0",
    "@types/react": "^18.3.0", "@types/react-dom": "^18.3.0",
    "tailwindcss": "^3.4.14", "postcss": "^8.4.0", "autoprefixer": "^10.4.0",
    "@playwright/test": "^1.48.0", "eslint": "^9.0.0"
  }
}
```

---

## 6. Repository Structure (consolidated — every file referenced anywhere in this plan)

```
cloudscale-incident-platform/
├── docker-compose.yml               # 4 app + 7 infra containers + ollama-init
├── .env.example                     # ANTHROPIC_API_KEY, LLM_MODE, DEMO_PROFILE, DB/Redis passwords
├── README.md
├── libs/cloudscale-common/          # SHARED KERNEL — frozen at H1
│   ├── schemas/                     # incident, IncidentState, RemediationPlan, WS events, audit events (Pydantic)
│   ├── gate_policy.yaml             # op classes + HITL matrix (single source of truth)
│   ├── gate.py                      # pure gate evaluation (used by agent-core AND gateway)
│   ├── op_classifier.py             # server-side operation classification
│   ├── canonical.py                 # canonical JSON + args_hash / plan_hash
│   ├── safety/                      # scrubber.py, normalizer.py, llama_guard.py, validators.py, heuristics.py
│   ├── audit_client.py              # hash-chained append (Postgres)
│   └── telemetry.py                 # OTel setup + the 7 metric instruments
├── agent-core/
│   ├── main.py                      # FastAPI: /internal/incidents, /internal/resume, A2A endpoints
│   ├── graph/  state.py, builder.py, supervisor.py (edge conditions),
│   │           intake.py, triage_agent.py, planner_agent.py, validate_plan.py,
│   │           hitl_gate.py, executor_agent.py, verifier.py, close.py
│   ├── llm/    router.py, safe_call.py, prompts/ (triage.md, planner.md, verifier.md), replay.py
│   ├── tools/  mcp_client.py, circuit_breaker.py, retry.py
│   ├── cache/  semantic_cache.py, cache_warmer.py
│   ├── economics/ token_ledger.py
│   ├── a2a/    agent_cards.py, endpoints.py
│   └── tests/
├── mcp-server/
│   ├── main.py                      # FastMCP streamable-HTTP app
│   ├── tools/  k8s_tools.py, infra_tools.py, scale_tools.py
│   ├── security/ token_verifier.py, opa_client.py, guarded_tool.py
│   ├── simulator/ engine.py (per-scenario state machine), faults.py
│   └── tests/
├── api-gateway/
│   ├── main.py
│   ├── routers/ auth.py, incidents.py, hitl.py, metrics.py, audit.py, token_ledger.py, webhooks.py, ws.py
│   ├── security/ session.py (RBAC), token_issuer.py (Ed25519), rate_limit.py
│   ├── workers/ review_deadlines.py, escalation_promoter.py, event_fanout.py
│   ├── db/ migrations/ (alembic), incident_repo.py
│   └── tests/
├── frontend/src/
│   ├── pages/  Dashboard.tsx, IncidentDetail.tsx, HITLQueue.tsx, AuditLog.tsx, Metrics.tsx, Login.tsx
│   ├── components/ IncidentCard, AgentTimeline, HITLModal, ReviewBanner, GuardrailBadge,
│   │               TokenLedgerPanel, CacheHitBadge, RouterStatusPanel, ArtifactViewer, MetricsPanel
│   ├── hooks/  useEventStream.ts (native WS + seq resume), useIncidents.ts
│   ├── store/  incidentStore.ts
│   ├── api/    client.ts
│   └── types/  incident.ts, events.ts (generated from libs schemas)
├── opa/
│   ├── policies/ mcp.rego
│   ├── data/ op_classes.json (generated from gate_policy.yaml), limits.json
│   └── tests/ mcp_test.rego
├── mock-data/
│   ├── schema.json
│   ├── manifest.sha256              # integrity check at startup (LLM04)
│   └── scenarios/ 01_pod_oom_kill.json … 08_network_partition.json, 09_prompt_injection.json
├── tests/
│   ├── integration/ test_scenarios.py (compose, replay mode)
│   ├── redteam/ injection_payloads.jsonl (20), benign_logs.jsonl (40), test_guard.py
│   └── e2e/ approval_flow.spec.ts (Playwright)
├── fixtures/replay/                 # recorded LLM responses keyed by prompt hash
├── scripts/ seed_incidents.py, demo_runner.py, eval_scenarios.py, health_check.sh, gen_opa_data.py
├── helm/cloudscale-platform/ Chart.yaml, values.yaml, values-eks.yaml, values-aks.yaml, templates/…
├── .github/workflows/ ci.yml, deploy.yml (manual dispatch, input: eks|aks)
└── docs/
    ├── adrs/ ADR-001 … ADR-012
    ├── architecture/ 7 .excalidraw files (§23)
    ├── threat-model/ owasp-llm-2025-mapping.md, prompt-injection-attack-tree.md, tool-abuse-attack-tree.md
    ├── nfr-matrix.md
    ├── demo-script.md
    └── financial/ tco-roi-model.md, tco-roi-model.xlsx, token-economics-model.md
```

---

## 7. Agent Design

### 7.1 `IncidentState` (frozen at H1 in `libs/cloudscale-common/schemas/state.py`)

```python
class IncidentState(TypedDict, total=False):
    incident: Incident                  # validated input (Pydantic model, see §15.1)
    telemetry: UnifiedTelemetry         # normalized Prometheus/Datadog snapshot
    log_excerpt_clean: str | None       # scrubbed + truncated; None if quarantined
    guard: GuardResult                  # {verdict, categories, heuristics, degraded: bool}
    cache: CacheResult                  # {tier: "tier1"|"tier2"|"miss", entry_id, original_cost_usd}
    triage: TriageResult                # rca, llm_confidence, evidence_score, confidence, risk_level, affected
    plan: RemediationPlan | None
    plan_hash: str | None               # sha256(canonical(plan))
    plan_attempts: int
    gate: GateEvaluation | None         # per-step gates + plan gate + reasons
    decision: HITLDecision | None       # outcome, approver, role, ts, exec_tokens (opaque)
    results: list[StepResult]
    verification: VerificationResult | None
    status: IncidentStatus
    spent_usd: float
    errors: list[str]
```

### 7.2 Intake node (no LLM)
1. Validate against `mock-data/schema.json`, which has `additionalProperties: false`. Invalid input → HTTP 400 plus an audit event.
2. **Scrub** secrets and **pseudonymize** IPs (§10.3).
3. **Normalize** telemetry (Prometheus or Datadog → `UnifiedTelemetry`). Truncate the log excerpt to 8,000 chars (≈2K tokens) by keeping the head and tail and collapsing repeated lines (§10.4).
4. **Guard check** on untrusted fields (`log_excerpt`, `title`, free-text tags): heuristics + LlamaGuard (§10.5). If injection is detected, set `QUARANTINED`, send no LLM call, and escalate with a `GUARDRAIL_ALERT`.

### 7.3 Triage agent
- **Model:** Sonnet 5 for P1 or when the intake risk hint is HIGH/CRITICAL; Haiku 4.5 otherwise (§12).
- **Tools:** `fetch_k8s_logs` and `get_metrics`, called by agent-core with the incident's **READ token** (the LLM does not call tools directly; the node gathers the evidence first).
- **Prompt structure:** Trusted instructions go in `system`. Untrusted incident data goes in `<incident_data>` tags, with an explicit instruction to treat it as data and never as instructions. Output uses structured outputs → `TriageResult`.
- **Composite confidence** (fixes v1's reliance on raw self-reported confidence):
  ```
  evidence_score = deterministic score from the runbook catalogue:
      0.90  known error signature matched AND metrics corroborate (e.g., OOMKilled + memory > 95 %)
      0.65  signature matched, metrics partially corroborate
      0.35  no catalogue match
  confidence = min(llm_confidence, evidence_score)
  ```
  The eval harness (§19.3) produces a reliability table per band from live runs. If a band's observed accuracy falls below its lower bound, that band's gate thresholds are raised.

### 7.4 Planner agent
Structured output → `RemediationPlan`:
```python
class ToolCall(BaseModel):
    tool_name: Literal["fetch_k8s_logs","get_metrics","clear_pod_cache","restart_service",
                       "scale_pods","apply_hotfix","rollback_deployment"]
    tool_args: dict                     # validated against the tool's own Pydantic arg model (§8.1)

class RemediationStep(BaseModel):
    step_id: str
    kind: Literal["tool", "manual_runbook"]
    call: ToolCall | None               # None for manual_runbook
    runbook_ref: str | None             # e.g. "RB-NODE-REPAIR" for manual steps
    llm_claims_destructive: bool        # kept ONLY for mismatch detection
    estimated_impact: str
    rollback: ToolCall | None           # executable rollback spec, not free text
    artifact: RecoveryArtifact | None

class RecoveryArtifact(BaseModel):
    artifact_type: Literal["k8s_patch", "terraform", "ansible", "runbook"]
    filename: str
    content: str                        # validated by artifact validators (§10.6); NEVER executed
    target_cloud: Literal["aws", "azure"]
```
Artifacts are **proposals for human review**. Tools execute only the structured `ToolCall`s. The artifact text is displayed and audited but never executed, which closes the v1 "LLM emits shell that gets run" path (LLM05).

### 7.5 validate_plan node (no LLM)
1. Parse every `tool_args` into that tool's argument model. There are no wildcards: `namespace` must equal `incident.namespace` and `cloud_provider` must equal `incident.cloud_provider`.
2. Run the validators on args and artifacts (§10.6).
3. **Server-side op-class** for every step (§9.1). If the class disagrees with `llm_claims_destructive` → `PLAN_TAG_MISMATCH` audit event, a GuardrailBadge in the UI, and a gate floor of APPROVAL.
4. Every DESTRUCTIVE step needs a `rollback`; if missing → gate ESCALATION.
5. On failure, re-plan once with the validation errors fed back. A second failure → `escalate`.

### 7.6 Executor & Verifier
- **Execute:** runs steps in order. Each step needs a valid exec token (§11.2). Call path: circuit breaker → MCP call (with `idempotency_key=step_id`) → audit before and after.
- **On step failure:** run that step's **pre-approved rollback** (its token was minted together with the step's), stop the remaining steps, and mark `PARTIALLY_RESOLVED` or `ESCALATED`.
- **Verify (deterministic):** poll `get_metrics` every 5 s for up to 60 s. Compare against the scenario's SLO thresholds (e.g., error_rate < 1 %, p99 < 500 ms, memory < 80 %). All metrics healthy → `RESOLVED`. At least 50 % improvement but not healthy → `PARTIALLY_RESOLVED`. Otherwise → `ESCALATED`. Haiku 4.5 writes only the human-readable `verification_summary`; it does **not** decide the outcome.

### 7.7 Supervisor
The supervisor is **deterministic code**: the conditional edges of the LangGraph `StateGraph` (`graph/supervisor.py`). It has no LLM, which makes it cheap, reproducible and auditable (ADR-001). Every routing decision is emitted as an `AGENT_DECISION` audit event with `actor=supervisor`.

---

## 8. MCP Server, Tools & Simulator

### 8.1 Tool contract — every tool takes the same scope arguments

`get_metrics` is included here; in v1 it lacked `namespace`/`cloud_provider`, so every scope check denied it.

```python
# mcp-server/main.py (sketch — verify decorator/transport names against the pinned mcp SDK at H0)
from mcp.server.fastmcp import FastMCP, Context
mcp = FastMCP("cloudscale-tools", stateless_http=True)

K8S_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"   # no wildcards, no shell metacharacters

class Scope(BaseModel):
    namespace: constr(pattern=K8S_NAME)
    cloud_provider: Literal["aws", "azure"]
    idempotency_key: str | None = None                 # required for mutating tools

@mcp.tool()
@guarded_tool(op_class="READ")
async def fetch_k8s_logs(scope: Scope, pod_name: constr(pattern=K8S_NAME), lines: conint(le=500) = 100, ctx: Context = None) -> dict: ...

@mcp.tool()
@guarded_tool(op_class="READ")
async def get_metrics(scope: Scope, service: constr(pattern=K8S_NAME), time_range: Literal["1m","5m","15m"] = "5m",
                      telemetry_source: Literal["prometheus","datadog"] = "prometheus", ctx: Context = None) -> dict: ...

@mcp.tool()
@guarded_tool(op_class="SAFE_MUTATION")
async def clear_pod_cache(scope: Scope, pod_name: constr(pattern=K8S_NAME), ctx: Context = None) -> dict: ...

@mcp.tool()
@guarded_tool(op_class="DISRUPTIVE")
async def restart_service(scope: Scope, service_name: constr(pattern=K8S_NAME), ctx: Context = None) -> dict: ...

@mcp.tool()
@guarded_tool(op_class="DYNAMIC")        # DISRUPTIVE if replicas > current, DESTRUCTIVE if < current (incl. 0)
async def scale_pods(scope: Scope, deployment: constr(pattern=K8S_NAME), replicas: conint(ge=0, le=50), ctx: Context = None) -> dict: ...

@mcp.tool()
@guarded_tool(op_class="DESTRUCTIVE")
async def apply_hotfix(scope: Scope, deployment: constr(pattern=K8S_NAME), patch: HotfixPatch, ctx: Context = None) -> dict: ...

@mcp.tool()
@guarded_tool(op_class="DESTRUCTIVE")
async def rollback_deployment(scope: Scope, deployment: constr(pattern=K8S_NAME), revision: conint(ge=1), ctx: Context = None) -> dict: ...

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

`HotfixPatch` replaces v1's free-form `config_patch: dict` with an **allowlisted schema**. Allowed keys: `resources.limits/requests` (bounded), `env` (allowlisted variable names), `connection_pool.max_size` (≤ 200), `tls.secret_ref` (must be a `Secret` in the same namespace), and `image.tag` (only from the approved registry). Everything else is rejected.

`restart_service` is classified as **DISRUPTIVE**, not "non-destructive". Restarting a production payment service causes brief unavailability, so it gets confidence-tiered gating.

### 8.2 `guarded_tool` — enforcement order inside the MCP server
1. Read `Authorization: Bearer <token>` from the HTTP request behind the MCP session (`ctx.request_context`).
2. Verify the token's EdDSA signature (gateway public key), `exp`, `aud="mcp-server"`, and that the `jti` hasn't been used (single-use exec tokens; the `jti` is stored in Redis).
3. Compute the **effective op-class** from the tool and args plus live simulator state (e.g., current replica count for `scale_pods`).
4. Build the OPA input (token claims, tool, args, `args_hash`, op-class, counters, breaker state) and call OPA. **No response, a timeout, or `allow=false` → deny** (§11.3).
5. Enforce the idempotency key: if the result is already in Redis `idem:{key}`, return it; otherwise execute.
6. Write `TOOL_CALL_STARTED` / `TOOL_CALL_FINISHED` audit events (insert-only DB role).

### 8.3 Retries, idempotency & circuit breaker (fixes the v1 `tenacity.CircuitBreaker` import)

| Op class | Retry policy (`tenacity`) | Why |
|---|---|---|
| READ | 3 attempts, exponential 0.5–4 s, only on `TransientToolError` | Side-effect-free |
| Mutating | **At most 1 retry, only on transport errors** (request may not have arrived) | Safe only because the server de-duplicates on `idempotency_key` |

```python
# agent-core/tools/circuit_breaker.py — Redis-backed so state survives restarts and is shared by replicas
class CircuitBreaker:
    """CLOSED → OPEN after `threshold` consecutive failed *logical calls* (after retries).
    OPEN → HALF_OPEN after `cooldown_s`; exactly one probe (SET NX lock) decides CLOSED or OPEN."""
    def __init__(self, redis, tool: str, cloud: str, threshold: int = 3, cooldown_s: int = 30):
        self.r, self.key, self.threshold, self.cooldown = redis, f"breaker:{tool}:{cloud}", threshold, cooldown_s

    async def before_call(self) -> None:
        state, opened_at = await self._load()
        if state == "OPEN":
            if time.time() - opened_at < self.cooldown:
                raise CircuitOpenError(self.key)
            if not await self.r.set(f"{self.key}:probe", 1, nx=True, ex=self.cooldown):
                raise CircuitOpenError(self.key)          # someone else is probing
            await self._transition("HALF_OPEN")

    async def on_success(self) -> None:
        await self.r.hset(self.key, mapping={"state": "CLOSED", "failures": 0})
        await self.r.delete(f"{self.key}:probe")
        await self._transition("CLOSED")

    async def on_failure(self) -> None:
        failures = await self.r.hincrby(self.key, "failures", 1)
        state, _ = await self._load()
        if state == "HALF_OPEN" or failures >= self.threshold:
            await self.r.hset(self.key, mapping={"state": "OPEN", "opened_at": time.time()})
            await self._transition("OPEN")                # audit CIRCUIT_STATE_CHANGED + gauge metric

async def call_tool_guarded(breaker, retrying, fn, **kwargs):
    await breaker.before_call()
    try:
        async for attempt in retrying:                    # AsyncRetrying policy chosen by op class
            with attempt:
                result = await fn(**kwargs)
    except Exception:
        await breaker.on_failure()                        # one failure per logical call
        raise
    await breaker.on_success()
    return result
```
A `CircuitOpenError` inside `execute` → the rollback for that step (if one was already applied) → `escalate`. OPA also denies calls while a breaker is OPEN (§11.3), so a buggy client can't bypass it.

### 8.4 Stateful simulator (fixes v1's static mocks, which made "verify metrics recovered" impossible)
Each scenario file has a `simulation` block that `simulator/engine.py` executes:
```json
"simulation": {
  "initial_metrics": {"memory_usage_pct": 98.7, "error_rate_pct": 23.4, "p99_ms": 4500},
  "healthy_thresholds": {"memory_usage_pct": 80, "error_rate_pct": 1, "p99_ms": 500},
  "effects": [
    {"when": {"tool": "apply_hotfix", "patch.resources.limits.memory": "2Gi"},
     "then": {"recover_to": {"memory_usage_pct": 55, "error_rate_pct": 0.4, "p99_ms": 320}, "over_seconds": 20}},
    {"when": {"tool": "restart_service"}, "then": {"recover_to": {"error_rate_pct": 8}, "over_seconds": 10, "relapse_after_seconds": 60}}
  ],
  "replicas": {"payment-service": 3},
  "faults": {"restart_service": {"fail_times": 0}}
}
```
**Fault injection:** `demo_runner.py --inject-fault restart_service:3` sets `fail_times=3` at runtime. This is how the circuit breaker and the Tool Failure Rate metric are shown in the demo.

---

## 9. HITL Gate — Per-Step, Operation-Class × Confidence

The v1 gate decided on the whole plan and let destructive operations auto-run at confidence ≥ 0.80, which contradicted its own matrix. In v2, `libs/cloudscale-common/gate_policy.yaml` is the **single source of truth**. The gate, the gateway re-check, OPA's op-class data, and the docs are all generated from or loaded from it.

### 9.1 Operation classes (computed server-side; the LLM's tag is ignored except for mismatch detection)
| Class | Operations |
|---|---|
| READ | `fetch_k8s_logs`, `get_metrics` |
| SAFE_MUTATION | `clear_pod_cache` |
| DISRUPTIVE | `restart_service`, `scale_pods` (replicas > current) |
| DESTRUCTIVE | `scale_pods` (replicas < current, incl. 0), `apply_hotfix`, `rollback_deployment` |
| MANUAL | `manual_runbook` steps (cluster failover, node repair) — no tool exists |

### 9.2 Gate matrix (confidence = composite confidence, §7.3)
| Class | ≥ 0.80 | 0.60 – 0.80 | 0.40 – 0.60 | < 0.40 |
|---|---|---|---|---|
| READ | AUTO | AUTO | AUTO | AUTO |
| SAFE_MUTATION (`clear_pod_cache`) | AUTO | AUTO | APPROVAL | ESCALATION |
| DISRUPTIVE (`restart_service`, scale up) | AUTO | REVIEW | APPROVAL | ESCALATION |
| DESTRUCTIVE (scale down/0, hotfix, rollback) | APPROVAL | APPROVAL | ESCALATION | ESCALATION |
| MANUAL (failover, node repair) | ESCALATION | ESCALATION | ESCALATION | ESCALATION |

**Overrides (applied after the matrix, and they can only raise the gate):**
| Condition | Effect |
|---|---|
| `risk_level == CRITICAL` | Every non-READ step → ESCALATION |
| Confidence < 0.60 | Every mutating step at least APPROVAL. This is the v1 "force HITL" rule, now consistent: REVIEW is never used below 0.60 |
| Result came from the semantic cache | Confidence capped at 0.79, so a cache hit can never produce AUTO for DISRUPTIVE |
| Guard degraded (LlamaGuard unavailable) | Mutating steps at least APPROVAL |
| `PLAN_TAG_MISMATCH` | Plan at least APPROVAL |
| DESTRUCTIVE step without `rollback` | ESCALATION |

**Plan gate** = the most restrictive step gate. Steps are ordered and depend on each other, so the whole plan is approved as a unit and bound to its `plan_hash`.

### 9.3 Gate evaluation (pure function — safe to re-run when LangGraph resumes a node)
```python
# libs/cloudscale-common/gate.py
ORDER = ["AUTO", "REVIEW", "APPROVAL", "ESCALATION"]
def _max(a: str, b: str) -> str: return a if ORDER.index(a) >= ORDER.index(b) else b

def step_gate(step: RemediationStep, ctx: GateContext, policy: GatePolicy) -> tuple[str, list[str]]:
    reasons: list[str] = []
    op = classify(step, ctx.sim_state)                        # server-side op class
    if op == "READ":   return "AUTO", ["read_only"]
    if op == "MANUAL": return "ESCALATION", ["manual_runbook"]
    conf = min(ctx.confidence, 0.79) if ctx.from_cache else ctx.confidence
    gate = policy.matrix[op][policy.tier(conf)]; reasons.append(f"matrix:{op}@{conf:.2f}")
    if ctx.risk_level == "CRITICAL":            gate = _max(gate, "ESCALATION"); reasons.append("critical_risk")
    if conf < 0.60:                             gate = _max(gate, "APPROVAL");   reasons.append("low_confidence_floor")
    if ctx.guard_degraded:                      gate = _max(gate, "APPROVAL");   reasons.append("guard_degraded")
    if ctx.tag_mismatch:                        gate = _max(gate, "APPROVAL");   reasons.append("tag_mismatch")
    if op == "DESTRUCTIVE" and step.rollback is None:
                                                gate = _max(gate, "ESCALATION"); reasons.append("no_rollback")
    return gate, reasons

def evaluate_plan(plan, ctx, policy) -> GateEvaluation:
    per_step = {s.step_id: step_gate(s, ctx, policy) for s in plan.steps}
    plan_gate = max((g for g, _ in per_step.values()), key=ORDER.index)
    return GateEvaluation(plan_hash=plan_hash(plan), gate=plan_gate, per_step=per_step,
                          required_role={"APPROVAL": "sre", "ESCALATION": "senior_sre"}.get(plan_gate))
```
`tests/unit/test_gate_matrix.py` is table-driven: every cell and every override from the YAML has a case.

### 9.4 Gate node with `interrupt()`
```python
# agent-core/graph/hitl_gate.py
from langgraph.types import interrupt, Command

async def hitl_gate(state: IncidentState) -> Command:
    ev = evaluate_plan(state["plan"], gate_ctx(state), POLICY)     # deterministic; no side effects before interrupt
    if ev.gate == "AUTO":
        return Command(goto="request_exec_tokens", update={"gate": ev})
    decision = interrupt({"gate": ev.gate, "plan_hash": ev.plan_hash,
                          "required_role": ev.required_role, "per_step": ev.per_step})
    # ↓ runs only after the gateway resumes the graph with Command(resume=decision)
    if decision["plan_hash"] != ev.plan_hash and not decision.get("modified_plan"):
        return Command(goto="escalate", update={"errors": ["plan_hash_mismatch"]})
    match decision["outcome"]:
        case "APPROVED" | "AUTO_PROCEEDED":
            return Command(goto="execute", update={"gate": ev, "decision": decision,
                                                    "plan": decision.get("modified_plan", state["plan"])})
        case "REJECTED":  return Command(goto="close_rejected", update={"decision": decision})
        case "HOLD":      return Command(goto="hitl_gate", update={"gate": ev.model_copy(update={"gate": "APPROVAL"})})
```
**Important LangGraph behavior:** when resumed, a node re-executes from its start. For that reason, the gate node has no side effects before `interrupt()`. Audit events and notifications are produced by the **gateway** when agent-core reports the interrupt, and they are idempotent on `(incident_id, plan_hash, gate)`.

### 9.5 Gate mechanics (the v1 `asyncio.sleep(60)` is removed)
| Gate | Mechanism | Timer | Who can decide |
|---|---|---|---|
| AUTO | No interrupt. agent-core asks the gateway for exec tokens; the **gateway re-runs `evaluate_plan` independently** and mints tokens only if it also gets AUTO | — | System |
| REVIEW | `interrupt()`. Gateway adds `(incident_id, deadline)` to Redis zset `review_deadlines`. The `review_deadlines` worker resumes with `AUTO_PROCEEDED` at the deadline. **Hold** (any SRE) removes the entry and converts the gate to APPROVAL | 60 s production / 15 s demo profile (`DEMO_PROFILE=1`) | SRE may Hold; else auto |
| APPROVAL | `interrupt()`. Blocks until Approve / Approve-with-modifications / Reject | None. After 10 min unanswered, `escalation_promoter` **promotes to ESCALATION** (reassigns to the senior queue). It **never auto-approves** | `sre` or `senior_sre` |
| ESCALATION | `interrupt()`, senior queue, pulsing banner | None | `senior_sre` only |

This resolves the v1 contradiction between "no timeout" and "auto-escalate after 10 min": APPROVAL promotes to ESCALATION after 10 minutes, and ESCALATION never times out.

### 9.6 Approve with modifications
1. The SRE edits step args in the modal. The gateway calls agent-core `POST /internal/validate-plan`, which runs the full §7.5 pipeline on the modified plan.
2. The gateway re-evaluates the gate on the modified plan. **Modifications can't lower the gate**: `new_gate = max(original_gate, evaluated_gate)`. If `new_gate` exceeds the approver's role (e.g., it became ESCALATION), the request moves to the senior queue.
3. The approval binds to the **modified `plan_hash`**, and exec tokens are minted for the modified args.

### 9.7 Roles & identity (the v1 "mock auth" undermined non-repudiation)
- `POST /auth/login` checks against users seeded from `.env` (argon2 hashes): `viewer`, `sre1`/`sre2` (role `sre`), `lead1` (role `senior_sre`). The session is a signed httpOnly, `SameSite=Strict` cookie.
- The approver ID in the audit log always comes from the **session**, never from the request body.
- **Production path (ADR-010):** OIDC (Entra ID / Okta), with group → role mapping.

### 9.8 UI indicators
🟢 AUTO chip · 🟡 REVIEW banner "Auto-proceeding in 15 s" with countdown and **Hold** button · 🔴 APPROVAL modal · 🚨 ESCALATION pulsing banner in the senior queue.

The modal shows:
- gate badge, confidence broken down as LLM vs evidence, and the reasons
- per-step op-class and gate
- affected services and impact
- rollback per step
- artifacts in a read-only code viewer
- buttons: **Approve**, **Approve with modifications**, **Reject**, **Escalate**

---

## 10. Safety & Guardrails

### 10.1 Where each control runs (v1 ran LlamaGuard over every HTTP body, including HITL approvals)
```
Gateway (HTTP)        : auth/RBAC · 1 MB body limit · per-user token-bucket rate limit · schema validation
agent-core intake     : scrubber → normalizer → heuristic injection detector → LlamaGuard (untrusted fields)
agent-core LLM output : structured-output parse → Pydantic → validate_plan (validators, op-class, mismatch)
                        → LlamaGuard on artifact text [SHOULD]
mcp-server            : token verify → OPA → strict arg schemas → idempotency
```
Structural controls come first. Tool arguments are strict schemas (k8s name regex, enums, bounded ints, allowlisted patch keys), so shell injection **through tool args is impossible by construction**. The regex and LlamaGuard checks are defense in depth on top of that.

### 10.2 Failure posture
| Component down | Behavior |
|---|---|
| LlamaGuard/Ollama | `guard.degraded=true`. Triage continues (read-only is harmless). Gate floor APPROVAL for mutating steps. `GUARD_DEGRADED` audit event |
| OPA | Deny everything (fail closed) |
| Postgres (audit) | Stop mutating operations. Can't audit → can't act |
| Anthropic API | Router failover (§12.2) → replay mode (demo) → escalate |

### 10.3 Secret scrubber (fixed: v1 redacted every IP, which destroyed the evidence needed for the network-partition RCA)
```python
class SecretScrubber:
    PATTERNS = [
        (re.compile(r'(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+'),                   r'\1=[REDACTED]'),
        (re.compile(r'(?i)\b(api[_-]?key|api[_-]?secret|token|bearer)\s*[:=]?\s*[A-Za-z0-9._\-]{12,}'), r'\1=[REDACTED]'),
        (re.compile(r'\bAKIA[0-9A-Z]{16}\b'),                                        '[AWS_KEY_REDACTED]'),
        (re.compile(r'(?i)aws_secret_access_key\s*[:=]\s*\S+'),                      'aws_secret_access_key=[REDACTED]'),
        (re.compile(r'-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----', re.S),       '[PEM_REDACTED]'),
        (re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.]+\b'),                                 '[EMAIL_REDACTED]'),
    ]
    IP = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')

    def scrub(self, text: str) -> str:
        for rx, repl in self.PATTERNS:
            text = rx.sub(repl, text)
        mapping: dict[str, str] = {}                     # stable per incident: 10.0.2.14 → IP_A
        def pseudo(m):
            return mapping.setdefault(m.group(0), f"IP_{chr(65 + len(mapping) % 26)}{len(mapping) // 26 or ''}")
        return self.IP.sub(pseudo, text)
```
Pseudonymizing keeps topology reasoning possible ("IP_A cannot reach IP_B") without leaking addresses.

### 10.4 Telemetry normalizer (v1 claimed truncation here but never implemented it)
```python
MAX_LOG_CHARS = 8_000     # ≈ 2K tokens; verified with messages.count_tokens in tests

def clean_log(text: str) -> str:
    lines, out, prev, repeat = text.splitlines(), [], None, 0
    for ln in lines:                                   # collapse repeated lines
        if ln == prev: repeat += 1; continue
        if repeat: out.append(f"  … (previous line repeated {repeat}×)")
        out.append(ln); prev, repeat = ln, 0
    if repeat: out.append(f"  … (previous line repeated {repeat}×)")
    s = "\n".join(out)
    if len(s) <= MAX_LOG_CHARS: return s
    head, tail = s[: MAX_LOG_CHARS * 2 // 3], s[-MAX_LOG_CHARS // 3 :]
    return f"{head}\n… [truncated {len(s) - MAX_LOG_CHARS} chars] …\n{tail}"
```
It also extracts `error_signature` (e.g., `OOMKilled`, `java.lang.OutOfMemoryError`, `x509: certificate has expired`), which is used for cache fingerprints and the runbook catalogue.

### 10.5 LlamaGuard (primary semantic layer)
- **Model:** `llama-guard3:1b` in Ollama. `ollama-init` runs `ollama pull llama-guard3:1b` once; v1's `OLLAMA_MODELS` env var does not pull models.
- **Categories:** Llama Guard 3 is trained on its own hazard taxonomy, and it accepts a custom category list in the prompt. v2 sends the platform categories: *S1 Prompt injection / instruction override*, *S2 Dangerous system commands*, *S3 Credential or data exfiltration*, *S4 Out-of-scope tool abuse*. Custom-category accuracy is **measured, not assumed** (targets below).
- **Heuristic pre-filter** (fast, deterministic): matches override phrases ("ignore (all|previous) instructions", "system override", "you are now", "do not trigger", role-tag spoofing such as `</incident_data>`). A heuristic hit plus a LlamaGuard `unsafe` → QUARANTINE. A heuristic hit alone → gate floor APPROVAL and a `GUARDRAIL_ALERT`.
- **Parsing:** the first line is `safe`/`unsafe`; the second line has comma-separated category codes.
- **Targets (red-team suite, §19.2):** at least 90 % of the 20 injection payloads blocked or floored; at most 5 % false positives on 40 benign real-world-style logs. Those logs deliberately contain "OOMKilled", "kill -9 by kubelet", `sudo` in audit logs, and `/etc/passwd` in file-integrity alerts, because v1's word-level blocklists would have blocked them.

### 10.6 Deterministic validators (secondary layer — they apply to **tool args and generated artifacts, not raw logs**)
| Validator | Applies to | Rules |
|---|---|---|
| `ArgSchemaValidator` | tool_args | Pydantic arg models (§8.1); namespace/cloud equal to incident; no `*` |
| `ArtifactSafetyValidator` | artifact.content | Reject `rm -rf /`, `curl … \| sh`, `chmod 777`, `privileged: true`, `hostNetwork: true`, `hostPath`, `runAsUser: 0`, `ClusterRoleBinding`, `kind: Secret` with inline data |
| `EgressValidator` | artifact.content | URLs must be in the allowlist (registry, internal docs); reject `nc -e`, raw IPs in URLs |
| `PatchBoundsValidator` | `HotfixPatch` | memory ≤ 8Gi, CPU ≤ 4, pool ≤ 200, replicas ≤ 50 |

---

## 11. Zero-Trust Architecture (3 layers)

### 11.1 Layers
| Layer | MVP implementation | Default posture |
|---|---|---|
| **Identity** | Gateway-signed **Ed25519** JWTs. **READ tokens** at ingestion; **EXEC tokens** only after a gate decision, bound to one step. MCP server holds only the public key | Deny if missing, expired, wrong `aud`, replayed `jti`, or scope mismatch |
| **Policy** | OPA `cloudscale.mcp.decision` with default deny, evaluated on every tool call | Deny if OPA unreachable or any deny rule fires |
| **Network** | Segmented Docker networks (§4.1) / K8s NetworkPolicies. mcp-server reachable only from agent-core | Deny by topology |
| Transport [STRETCH] | mTLS agent-core ↔ mcp-server (certs from `scripts/generate_certs.sh`, 7-day validity, regenerated on `make certs`) | — |

**Honest scope statement** (v1 claimed "no plaintext channels" but called OPA and Ollama over `http://`): in the MVP, internal hops are plaintext **inside isolated Docker networks**, and TLS terminates at the gateway in cloud deployments. mTLS is a stretch goal. In the cloud reference, a service mesh (Istio/Linkerd) provides mTLS for all pod-to-pod traffic, including OPA and LlamaGuard.

### 11.2 Token types & minting flow (fixes v1's 15-min TTL expiring during HITL waits, and write tools granted at ingestion)
| Token | Issued when | Claims | TTL | Reuse |
|---|---|---|---|---|
| READ | Incident accepted | `typ=read, incident_id, namespace, cloud_provider, aud=mcp-server` | 10 min, **refreshed** by agent-core via `POST /internal/tokens/refresh` while the incident is active | Multi-use |
| EXEC | After AUTO re-check, REVIEW auto-proceed, or human approval | `typ=exec, incident_id, namespace, cloud_provider, step{step_id, tool, op_class, args_hash}, approval{decision_id, gate, approver, role}, plan_hash, jti` | 10 min from **minting** (minted just before execution, so HITL wait time doesn't matter) | **Single-use** |

- The **gateway mints** tokens. agent-core never holds the signing key, so a compromised agent **can't approve itself**. For AUTO, the gateway independently re-runs `evaluate_plan` on the stored plan before minting.
- Rollback steps get their EXEC tokens in the same approval, bound to the rollback's `args_hash`.
- Permissions come from the **approved plan**, not from severity. v1's severity-based `allowed_tools` blocked scenario 5, a P2 incident that needs `apply_hotfix`.

### 11.3 OPA policy (rewritten; v1's rules were OR'd, the rate-limit package was never queried, and scale-to-0 was ignored)
```rego
# opa/policies/mcp.rego
package cloudscale.mcp
import rego.v1

default allow := false
allow if count(deny) == 0
decision := {"allow": allow, "reasons": deny}

is_read if input.tool in data.op_classes.READ

deny contains "namespace_scope"   if input.args.scope.namespace != input.token.namespace
deny contains "cloud_scope"       if input.args.scope.cloud_provider != input.token.cloud_provider
deny contains "wildcard_arg"      if { some v in walk_strings(input.args); contains(v, "*") }

deny contains "read_token_mutation" if { input.token.typ == "read"; not is_read }
deny contains "exec_tool_mismatch"  if { input.token.typ == "exec"; input.tool != input.token.step.tool }
deny contains "exec_args_mismatch"  if { input.token.typ == "exec"; input.args_hash != input.token.step.args_hash }
deny contains "op_class_escalated"  if {
    input.token.typ == "exec"
    data.op_rank[input.op_class] > data.op_rank[input.token.step.op_class]   # e.g. approved as scale-up, now scale-down
}
deny contains "missing_approval"    if {
    input.op_class in {"DESTRUCTIVE"}
    not input.token.approval.gate in {"APPROVAL", "ESCALATION"}               # destructive can never be AUTO/REVIEW
}
deny contains "rate_limited"        if input.counters.calls_last_minute >= data.limits.max_calls_per_minute
deny contains "circuit_open"        if input.counters.breaker_state == "OPEN"

walk_strings(x) := {v | walk(x, [_, v]); is_string(v)}
```
- `data.op_classes`, `data.op_rank` and `data.limits` are **generated** from `gate_policy.yaml` by `scripts/gen_opa_data.py`.
- `opa/tests/mcp_test.rego` has at least one allow case and one deny case per rule. It runs in CI with `opa test opa/ -v`.
- The MCP server queries `POST /v1/data/cloudscale/mcp/decision` with a 2 s timeout. A timeout or missing result means deny, and the `reasons` are audited as `POLICY_DECISION`.

### 11.4 Enforcement summary
| Principle | Implementation | Enforced at |
|---|---|---|
| Never trust, always verify | Signature + claims + OPA on **every** tool call | mcp-server |
| Least privilege | READ tokens can't mutate; EXEC tokens are one step, one args hash, single use | Gateway minting + OPA |
| No self-authorization | Only the gateway holds the signing key and re-checks AUTO gates | Gateway |
| Micro-segmentation | Docker networks / NetworkPolicies; namespace + cloud scope lock | Network + OPA |
| Default deny | `default allow := false`; OPA timeout = deny; audit DB down = no mutation | OPA / mcp-server |
| Immutable trail | Every allow/deny decision is audited in the hash chain | Audit |

---

## 12. Model Routing & Token Economics

### 12.1 Models & prices (Anthropic first-party list prices, per 1M tokens — re-verify at H0)
| Model | Model ID | Input | Output | Used for |
|---|---|---|---|---|
| Claude Sonnet 5 | `claude-sonnet-5` | $2.00 | $10.00 | P1 / HIGH-risk triage; planning with DESTRUCTIVE candidates |
| Claude Haiku 4.5 | `claude-haiku-4-5` | $1.00 | $5.00 | P2 triage & planning; verification summaries; failover |
| Claude Opus 5 [STRETCH] | `claude-opus-5` | $5.00 | $25.00 | Optional second opinion on CRITICAL incidents |

Thinking tokens are billed as output and are included in the output budgets below. Sonnet 5 runs adaptive thinking at `effort: "medium"`; Haiku 4.5 runs without extended thinking. ADR-005 records an experiment: Sonnet 5 at low effort for every agent vs. the two-model cascade, measured on the eval set (a single model also shares one prompt-cache namespace).

### 12.2 Router (Anthropic-only in MVP; multi-provider documented in ADR-012 as [STRETCH])
| Agent | Condition | Model | `max_tokens` (ceiling) |
|---|---|---|---|
| Triage | P1, or risk hint HIGH/CRITICAL | `claude-sonnet-5` | 8,000 |
| Triage | otherwise | `claude-haiku-4-5` | 4,000 |
| Planner | P1, or any DESTRUCTIVE candidate from triage | `claude-sonnet-5` | 8,000 |
| Planner | otherwise | `claude-haiku-4-5` | 4,000 |
| Verifier summary | all | `claude-haiku-4-5` | 1,000 |

- **Budget axis:** once `spent_usd ≥ $0.50` for the incident, downgrade to Haiku. At `≥ $1.00`, stop LLM calls and escalate.
- **Failover:** Sonnet 5 error or timeout (45 s P1 / 90 s P2) → Haiku 4.5 once → in `LLM_MODE=replay`, the recorded response → escalate. Every failover emits `PROVIDER_FAILOVER` (audit) and a span event.
- **Health:** passive only. An EWMA of observed latency and error rate from real calls, shown in `RouterStatusPanel`. v1's active probing (3 providers × 3 calls every 30 s) cost money, burned rate limits, and started `asyncio.create_task` in `__init__` before any event loop existed. All of that is removed.
- **Prompt caching:** the stable prefix (system prompt + runbook catalogue) is marked with `cache_control`. Hits are verified via `usage.cache_read_input_tokens`. Prompt caching is **not** counted in the TCO (conservative).
- **LLM modes:** `LLM_MODE=live|record|replay`. Replay serves responses keyed by `sha256(model + canonical(messages))` from `fixtures/replay/` and is used for the demo fallback and the CI integration tests.

### 12.3 Per-incident cost (expected average usage, not `max_tokens` ceilings)
| Agent | Avg input | Avg output | Cost on Sonnet 5 | Cost on Haiku 4.5 |
|---|---|---|---|---|
| Triage | 8,192 | 2,048 | $0.0369 | $0.0184 |
| Planner | 4,096 | 3,072 | $0.0389 | $0.0195 |
| Verifier summary | 2,048 | 1,024 | $0.0143 | $0.0072 |

| Step | Value |
|---|---|
| P1 incident (Sonnet triage + planner, Haiku verifier) | $0.0369 + $0.0389 + $0.0072 = **$0.0829** |
| P2 incident (all Haiku) | $0.0184 + $0.0195 + $0.0072 = **$0.0451** |
| Blended (40 % P1 / 60 % P2) | **$0.0602** |
| Semantic cache: 20 % hit rate on triage + planner (blended $0.0530) | −$0.0106 → $0.0496 |
| Overhead ×1.3 (re-plans, validation retries, approve-with-modifications) | **$0.0645 ≈ $0.065 per incident** |

At 200 incidents per month that is **≈ $13/month** of production LLM spend. The TCO adds **$100/month** for evaluation, staging and prompt iteration. v1's $810/month figure was inconsistent with its own per-incident numbers.

### 12.4 Token ledger (Postgres, fixes v1's un-serialized `rpush` and a cache-savings sum that was always 0)
```sql
CREATE TABLE token_usage (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  incident_id TEXT NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL,
  input_tokens INT NOT NULL, output_tokens INT NOT NULL,
  cache_read_tokens INT NOT NULL DEFAULT 0,
  cost_usd NUMERIC(12,6) NOT NULL,                 -- 0 when served from semantic cache
  avoided_cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0, -- original cost of the cached call on a hit
  cache_tier TEXT                                   -- NULL | tier1 | tier2
);
-- 30-day burn rate (v1 did not filter by date and multiplied an all-time total by 12)
SELECT sum(cost_usd) AS cost_30d, sum(avoided_cost_usd) AS savings_30d,
       count(DISTINCT incident_id) AS incidents,
       sum(cost_usd) / NULLIF(count(DISTINCT incident_id), 0) AS cost_per_incident,
       sum(cost_usd) * 365.0 / 30 AS projected_annual
FROM token_usage WHERE ts >= now() - interval '30 days';
```
- **Cost-per-MTTR-minute KPI** = `incident_cost_usd / max(manual_mttr − automated_mttr, 1 min)`, computed on close. Example: $0.065 / 38 min ≈ **$0.0017 per SRE-minute saved**.
- Endpoints: `GET /metrics/token-ledger/{incident_id}` and `GET /metrics/token-ledger/summary?window=30d`.
- `TokenLedgerPanel.tsx` shows per-call rows plus "avoided cost", which comes from the cache entry's recorded original cost.

---

## 13. Semantic Cache (safety-first redesign)

v1 could return **another incident's RCA and confidence** (same service and severity), and that could AUTO-execute with the wrong namespace. It also wrote cache entries at triage time, before anyone knew whether the RCA was right, used blocking `KEYS` scans, and had a feedback loop that never matched the exact-match entries.

| Aspect | v2 design |
|---|---|
| Tier 1 key [MVP] | `sha256(cloud | namespace | service | alert_name | error_signature)` in Redis, TTL 24 h |
| Tier 2 [SHOULD] | pgvector (HNSW, cosine) over `bge-small` embeddings of `error_signature + normalized metric summary`, filtered `WHERE cloud = $1 AND namespace = $2 AND service = $3`, similarity ≥ 0.92 |
| What's stored | RCA, **plan template** (tool names + arg templates with `{namespace}`/`{service}` placeholders), `original_cost_usd`, source incident ID, `verified_outcome` |
| When written | **Only after `close` with RESOLVED and verification passed.** Cache poisoning via a wrong RCA is impossible because unverified results are never cached (LLM04/LLM08) |
| On hit | RCA reused; confidence capped at 0.79; plan template **re-bound** to the current incident and run through the full `validate_plan` + gate again |
| Feedback | RESOLVED after a hit → TTL ×2 (max 7 days), `hit_count++`. PARTIALLY_RESOLVED/ESCALATED after a hit → **delete the entry** + `CACHE_INVALIDATED` audit |
| Targets | 15–25 % combined hit rate in production. v1's 44 % had no basis; 8 distinct demo scenarios would hit ≈0 % unless repeated. The demo shows the mechanism by re-running scenario 7 |

---

## 14. Telemetry, Audit & Persistence

### 14.1 Tracing
- The OTel SDK exports over **OTLP gRPC** to `otel-collector`, which feeds Jaeger (traces) and Prometheus (metrics, via the collector's `prometheus` exporter).
- **Propagation:** W3C `traceparent` is carried on gateway→agent-core REST, agent-core→MCP HTTP (httpx instrumentation), and A2A metadata.
- **HITL-safe trace design:** a trace isn't held open for hours of human wait time. Each phase is its own trace: `incident.triage_plan`, then `incident.execute_verify`, linked with a **span link**. Every span carries the `incident.id` attribute, so Jaeger's search on `incident.id=INC-…` shows the whole story. Human wait is recorded as the metric `hitl.wait.seconds` plus span events.

```
trace A: incident.triage_plan  [incident.id=INC-2026-001]
  ├─ api.incident.received
  ├─ intake.scrub_normalize ─ intake.guard (llamaguard.latency_ms, verdict)
  ├─ cache.lookup (tier)
  ├─ agent.triage ─ mcp.fetch_k8s_logs ─ mcp.get_metrics ─ llm.call (model, tokens, cost)
  ├─ agent.planner ─ llm.call
  ├─ plan.validate
  └─ hitl.gate.evaluate (gate, reasons) → event: interrupt
trace B: incident.execute_verify  [link → A]
  ├─ tokens.mint (step ids)
  ├─ agent.executor ─ mcp.apply_hotfix (opa.decision, breaker.state) ─ mcp.restart_service
  ├─ verify.poll ×n ─ llm.call (summary)
  └─ incident.close (status, mttr_s)
```

### 14.2 The 7 metrics (4 jury metrics + 3 supporting)
| # | Instrument | Type | Labels | Derived KPI |
|---|---|---|---|---|
| 1 | `incident.task.latency` (ms) | histogram | agent, phase | **LPT** — p50/p95 per agent |
| 2 | `llm.tokens` | counter | direction(in/out), model, agent | **TCR** — tokens/min, tokens/incident |
| 3 | `cache.lookups` | counter | result(tier1/tier2/miss) | **CHR** = hits / total |
| 4 | `mcp.tool.calls` | counter | tool, outcome(ok/error/denied) | **TFR** = error / total (v1 counted only failures, so no rate could be computed) |
| 5 | `llm.cost.usd` | counter | model, agent | cost/incident, burn rate |
| 6 | `hitl.wait.seconds` | histogram | gate | human latency |
| 7 | `circuit.state` | gauge (0 closed, 1 half, 2 open) | tool, cloud | breaker visibility |

`GET /metrics/live?range=1h|6h|24h` runs fixed PromQL queries against Prometheus. v1 had no metrics backend at all; Jaeger doesn't store metrics.

### 14.3 Audit log — append-only, hash-chained (v1 had no `prev_hash`, and SQLite can't enforce permissions)
```sql
CREATE TABLE audit_log (
  seq         BIGSERIAL PRIMARY KEY,
  event_id    UUID NOT NULL UNIQUE,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  incident_id TEXT,
  event_type  TEXT NOT NULL,       -- enum below
  actor_type  TEXT NOT NULL,       -- system | agent | human | policy
  actor_id    TEXT NOT NULL,       -- supervisor | triage | planner | executor | opa | <user id from session>
  payload     JSONB NOT NULL,      -- scrubbed; no raw prompts (see transcripts)
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL        -- sha256(prev_hash || canonical_json(row without hash))
);
CREATE FUNCTION audit_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_log is append-only'; END $$ LANGUAGE plpgsql;
CREATE TRIGGER no_update BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_immutable();
-- Roles: svc_gateway / svc_agent / svc_mcp get INSERT, SELECT only; no role has UPDATE/DELETE; owner role is not used at runtime.
```
- **Append:** `audit_client.append()` takes `pg_advisory_xact_lock(42)`, reads the last hash, computes the new hash, and inserts, all in one transaction. The chain is linear even with 3 writer services.
- **Verify:** `GET /audit/verify?from_seq=` recomputes the chain and returns the first broken `seq`, or `ok`. It's a button on the Audit page.
- **Event types:**
  - Ingestion & safety: `INCIDENT_RECEIVED`, `GUARD_CHECK`, `GUARD_DEGRADED`, `GUARDRAIL_ALERT`, `QUARANTINED`
  - Cache: `CACHE_HIT`, `CACHE_WRITTEN`, `CACHE_PROMOTED`, `CACHE_INVALIDATED`
  - Agent & plan: `AGENT_DECISION`, `TOKEN_USAGE`, `PLAN_VALIDATED`, `PLAN_TAG_MISMATCH`, `ARTIFACT_GENERATED`
  - HITL & tokens: `GATE_EVALUATED`, `HITL_REQUEST`, `HITL_DECISION`, `HITL_PROMOTED`, `EXEC_TOKEN_MINTED`
  - Execution: `POLICY_DECISION`, `TOOL_CALL_STARTED`, `TOOL_CALL_FINISHED`, `CIRCUIT_STATE_CHANGED`, `ROLLBACK_EXECUTED`, `PROVIDER_FAILOVER`, `INCIDENT_CLOSED`
- **LLM transcripts** (scrubbed prompts and responses) go in a separate table, `llm_transcripts`, with 30-day retention. The audit row stores only `sha256(prompt)` and `sha256(response)`. v1 stored raw prompts in the immutable log, which conflicts with scrubbing and data-retention rules.

### 14.4 Persistence map & retention
| Store | Data | Retention |
|---|---|---|
| Postgres | incidents, plans (versioned by `plan_hash`), hitl_decisions, audit_log, token_usage, llm_transcripts, semantic vectors, LangGraph checkpoints | audit: 1 year (prod) · transcripts: 30 days · checkpoints: purged 7 days after close |
| Redis | tier-1 cache, rate counters, `idem:*` (24 h), `jti:*` (token TTL), `breaker:*`, `review_deadlines`, pub/sub `events` | TTL-bound; loss is safe (fail closed) |
| `mock-data/manifest.sha256` | scenario file hashes | Checked at startup; a mismatch refuses to boot |

---

## 15. Incident Scenarios & Ground Truth

### 15.1 Incident schema (additions over v1: `alert_name`, `ground_truth`, `simulation`; `additionalProperties: false`)
```json
{
  "incident_id": "INC-2026-001",
  "title": "OOMKilled Pods in Payment Service",
  "severity": "P1",
  "alert_name": "KubePodOOMKilled",
  "affected_service": "payment-service",
  "namespace": "production",
  "cloud_provider": "aws",
  "cloud_region": "us-east-1",
  "telemetry_snapshot": { "source": "prometheus", "cpu_usage_pct": 45.2, "memory_usage_pct": 98.7,
                          "error_rate_pct": 23.4, "request_latency_p99_ms": 4500, "pod_restarts_last_1h": 12 },
  "datadog_snapshot": null,
  "log_excerpt": "FATAL: java.lang.OutOfMemoryError: Java heap space ... Reason: OOMKilled",
  "alert_source": "prometheus",
  "tags": ["kubernetes", "memory", "payment", "aws"],
  "ground_truth": {                       // stripped before any LLM call; used by tests + eval only
    "root_cause_category": "memory_limit_too_low",
    "expected_actions": ["apply_hotfix:resources.limits.memory", "restart_service"],
    "expected_gate": "APPROVAL",
    "expected_final_status": "RESOLVED"
  },
  "simulation": { "...": "see §8.4" }
}
```
Datadog-format snapshots are normalized by `TelemetryNormalizer` exactly as in v1. The Datadog `series`/`events` shape is unchanged.

### 15.2 Scenario catalogue (remediations corrected to actually address each root cause)
| # | Scenario | Cloud | Telemetry | Sev | Root cause | Remediation (tools) | Op class | Expected gate | Demo purpose |
|---|---|---|---|---|---|---|---|---|---|
| 1 | OOMKilled payment pod | AWS | Prometheus | P1 | Memory limit 1Gi too low | `apply_hotfix` (limit → 2Gi) → `restart_service`; rollback = `rollback_deployment` | DESTRUCTIVE | APPROVAL | Approval modal + artifact review; **fault-injection variant** → circuit breaker |
| 2 | Node disk pressure | AWS | Prometheus | P2 | Runaway ephemeral cache in `image-resizer` pod fills node disk | `clear_pod_cache` | SAFE_MUTATION | AUTO | Autonomous safe reset |
| 3 | API latency spike | Azure | Datadog | P2 | Thread-pool deadlock in `api` pods | `restart_service` | DISRUPTIVE | REVIEW (conf ≈ 0.65–0.75) | Review countdown + Hold |
| 4 | DB connection-pool exhaustion | AWS | Datadog | P1 | Pool size 20 too low after traffic growth | `apply_hotfix` (pool → 50); rollback = `rollback_deployment` | DESTRUCTIVE | APPROVAL | Approve-with-modifications (SRE sets 40) |
| 5 | TLS certificate expiry | Azure | Prometheus | P2 | cert-manager renewal failed; ingress references expired secret | `apply_hotfix` (`tls.secret_ref` → renewed secret); rollback = `rollback_deployment` | DESTRUCTIVE | APPROVAL | Shows the P2 hotfix now allowed (v1's severity-based tokens blocked it) |
| 6 | K8s node NotReady | AWS | Prometheus | P1 | kubelet failure; capacity loss | `scale_pods` up on healthy nodes + `manual_runbook` RB-NODE-REPAIR | DISRUPTIVE + MANUAL | ESCALATION | Senior queue; partial automation |
| 7 | Memory leak (cache bloat) | Azure | Datadog | P2 | Unbounded in-process cache | `clear_pod_cache` | SAFE_MUTATION | AUTO | Happy path; **run twice → tier-1 cache hit** |
| 8 | Network partition (AZ) | AWS | Prometheus | P1 | AZ-level partition, risk CRITICAL | `manual_runbook` RB-AZ-FAILOVER (no autonomous tool) | MANUAL | ESCALATION | Hard stop: no autonomy for failover |
| 9 | Prompt injection (adversarial) | AWS | Prometheus | P1 | `log_excerpt` contains an override instruction | none — quarantined at intake | — | QUARANTINED → ESCALATION | Security demo; attack chain in audit log |

Gate coverage: AUTO (2, 7) · REVIEW (3) · APPROVAL (1, 4, 5) · ESCALATION (6, 8) · QUARANTINE (9) · circuit breaker (1-fault).

---

## 16. Frontend & Protocol Standards

### 16.1 Dashboard pages (5 + login)
| Page | Contents |
|---|---|
| Dashboard | Live incident feed with the §4.2 status chips; severity badges; CacheHitBadge; KPI row: Active P1s, MTTR (p50), Auto-resolved %, Pending HITL, Cost today |
| Incident Detail | RCA with LLM vs evidence confidence; plan steps with op-class + gate; AgentTimeline; ArtifactViewer (copy/download); verification result with metric recovery chart; TokenLedgerPanel; **deep link to Jaeger** (`/search?tags={"incident.id":"…"}`) |
| HITL Queue | Tabs "Approval" (role `sre`) and "Escalation" (role `senior_sre`); ReviewBanner countdowns with Hold; HITLModal (§9.8) |
| Audit Log | Filter by incident, event type, actor, date; hash + prev_hash per row; **Verify chain** button; export JSON/CSV |
| Metrics | 4 jury charts (LPT, TCR, CHR, TFR) + cost + HITL wait + breaker states; range 1h/6h/24h; RouterStatusPanel |

### 16.2 WebSocket event contract (native WebSocket; typed in `libs/…/schemas/events.py` → generated `events.ts`)
```json
{ "type": "AGENT_STATE_UPDATE", "seq": 1042, "ts": "2026-09-25T10:15:02Z", "incident_id": "INC-2026-001", "payload": { } }
```
- **Event types:** `INCIDENT_CREATED`, `AGENT_STATE_UPDATE`, `TOOL_RESULT_PREVIEW`, `GUARDRAIL_ALERT`, `HITL_REQUEST`, `HITL_REVIEW_TICK`, `HITL_RESOLVED`, `CIRCUIT_STATE_CHANGED`, `INCIDENT_RESOLVED`.
- **Delivery:** agent-core publishes to Redis `events`; the gateway `event_fanout` worker forwards to authorized WS clients.
- **Reconnect:** the client sends `?since_seq=`. If there's a gap, it refetches the REST snapshot.
- **Auth:** the WS upgrade requires the session cookie, and events are filtered by role.

### 16.3 Protocol decisions (corrected descriptions)
| Protocol | Status | Correction vs v1 |
|---|---|---|
| **MCP** (ADR-002) | **Implemented**: FastMCP server with streamable-HTTP transport; agent-core uses the `mcp` client session | v1 posted to `/tools/{name}`, which is REST, not MCP. MCP provides tool discovery (`tools/list`), JSON-Schema tool inputs, and HTTP transports with an OAuth-based auth model. It has **no built-in audit hooks and no namespace enforcement**; the platform adds those in `guarded_tool` + OPA |
| **UTCP** (ADR-008) | Documented only | UTCP is a community protocol where the agent calls tools **directly over their native interfaces** (HTTP, CLI, etc.) using a JSON "manual", with no intermediary server. It's attractive when tools already have APIs. It was rejected here because the platform *needs* an enforcement point (token + OPA + idempotency + audit) between agent and tool |
| **A2A** (ADR-007) | [SHOULD] AgentCards for supervisor + 3 specialists; `message/send` for the Triage skill. [STRETCH] full task lifecycle | Internal handoff between agents is in-graph (LangGraph state), not A2A. A2A is the **external federation** interface. The card path and method names follow the A2A spec version pinned at H0 (current spec: `/.well-known/agent-card.json`; older drafts used `agent.json` and `tasks/send`) |
| **A2UI** (ADR-009) | Documented only | v1 called its WebSocket channel "A2UI-aligned". It isn't: A2UI is a declarative agent-generated-UI format. Our channel is a typed event stream (§16.2). ADR-009 evaluates A2UI for future agent-rendered approval forms |

---

## 17. 3-Year TCO / ROI Model (recomputed; all inputs stated)

> Cloud prices are **on-demand us-east-1 estimates** for planning. Validate with the AWS Pricing Calculator before external use. One primary cloud (AWS EKS) is costed; Azure AKS is a portability overlay, and a warm DR standby is a sensitivity case. v1 ran both clouds at the same time with no stated reason.

### 17.1 Assumptions
| Parameter | Value | Note |
|---|---|---|
| SRE fully-loaded rate | $150/h | |
| Incidents/month | Y1 200 · Y2 260 · Y3 338 | +30 % YoY |
| Platform adoption (share routed through platform) | Y1 70 % · Y2 90 % · Y3 95 % | Ramp. v1 assumed 100 % from day 1 |
| Outcome mix automated / HITL-approved / escalated | Y1 40/40/20 · Y2 50/35/15 · Y3 55/32/13 | Informed by the scenario gate mix |
| Manual MTTR | 45 min | Baseline |
| Minutes saved per incident | Automated 38 (45 → 7 incl. post-review) · HITL 25 (45 → 20) · Escalated 10 (RCA pre-done) | v1 applied 41 min to every incident |
| Bad-remediation allowance | 2 % (Y3 1.5 %) of automated + HITL incidents × 120 min extra | v1 ignored failures |
| LLM cost/incident | $0.0645 (§12.3) | + $100/month eval/staging |
| Maintenance | 20 h/month × $150 = **$3,000/month** (Y3: 16 h) | v1 wrote "0.5 FTE × 20 h = $1,500", which is arithmetically wrong |
| Discount rate | 10 % | |
| Savings nature | **Freed SRE capacity** valued at the loaded rate, not headcount reduction | Downtime/revenue avoided is excluded (upside) |

### 17.2 Year 0 — one-time (CAPEX)
| Item | Cost |
|---|---|
| Capstone build: 10 engineers × 12 h × $150 | $18,000 |
| **Productionization**: 2 engineers × 6 weeks. Replace simulators with real K8s/cloud adapters, OIDC, mesh mTLS, pen test, runbook catalogue | $72,000 |
| Cloud landing zone + Helm hardening | $2,000 |
| Policy authoring & review (OPA, gate policy) | $1,500 |
| **Total Year 0** | **$93,500** |

v1 omitted productionization entirely, even though the capstone runs on mocks.

### 17.3 Yearly OPEX
| Line (monthly) | Y1 | Y2 | Y3 |
|---|---|---|---|
| EKS control plane | $73 | $73 | $73 |
| App node group (m6i.large; 3 / 4 / 5 nodes) | $210 | $280 | $350 |
| Guard node (c6i.xlarge, LlamaGuard) | $124 | $124 | $124 |
| RDS PostgreSQL (db.t4g.medium, Multi-AZ, 50 GB) | $105 | $105 | $105 |
| ElastiCache Redis (cache.t4g.medium) | $47 | $47 | $47 |
| Networking (ALB + NAT + egress) | $70 | $70 | $70 |
| Observability storage (Prometheus/Jaeger, 30-day) | $40 | $40 | $40 |
| Logs, registry, secrets | $20 | $20 | $20 |
| **Infra subtotal** | **$689** | **$759** | **$829** |
| LLM production (incidents × $0.0645) | $13 | $17 | $22 |
| LLM eval/staging | $100 | $100 | $100 |
| Platform maintenance | $3,000 | $3,000 | $2,400 |
| **Total / month** | **$3,802** | **$3,876** | **$3,351** |
| **Total / year** | **$45,624** | **$46,512** | **$40,212** |

### 17.4 Benefits (SRE capacity freed)
| | Y1 | Y2 | Y3 |
|---|---|---|---|
| Incidents via platform / month | 140 | 234 | 321.1 |
| Automated × 38 min | 56 → 2,128 | 117 → 4,446 | 176.6 → 6,711 |
| HITL × 25 min | 56 → 1,400 | 81.9 → 2,047.5 | 102.8 → 2,568.8 |
| Escalated × 10 min | 28 → 280 | 35.1 → 351 | 41.7 → 417.4 |
| Bad-remediation allowance | −268.8 | −477.4 | −502.8 |
| **Net minutes / month** | **3,539.2** | **6,367.1** | **9,194.4** |
| SRE hours / month | 59.0 | 106.1 | 153.2 |
| **Annual value @ $150/h** | **$106,176** | **$191,014** | **$275,831** |

### 17.5 Summary, NPV, payback
| Year | Platform cost | Benefit | Net | Cumulative |
|---|---|---|---|---|
| Y0 | $93,500 | $0 | −$93,500 | −$93,500 |
| Y1 | $45,624 | $106,176 | +$60,552 | −$32,948 |
| Y2 | $46,512 | $191,014 | +$144,502 | +$111,554 |
| Y3 | $40,212 | $275,831 | +$235,619 | **+$347,173** |
| **3-yr total** | **$225,848** | **$573,021** | **$347,173** | |

```
PV(Y1) = 60,552 / 1.10    =  55,047
PV(Y2) = 144,502 / 1.21   = 119,423
PV(Y3) = 235,619 / 1.331  = 177,024
Σ PV = 351,494  →  NPV = 351,494 − 93,500 = $257,994

Payback: −32,948 at end of Y1; Y2 averages +12,042/month → 2.7 months → Month 15 of operations
Benefit-cost ratio (3 yr) = 573,021 / 225,848 = 2.54   ·   Net ROI = 347,173 / 225,848 = 154 %
Automated-path MTTR: 45 → 7 min (−84 %)   ·   SRE hours freed: Y1 708 h, Y3 1,839 h
```

### 17.6 Sensitivity (3-year net)
| Case | Change | 3-yr net |
|---|---|---|
| Base | — | $347,173 |
| Lower automation | 15 pts of automated shifted to HITL each year | $306,511 |
| Slower adoption | 50 / 70 / 75 % | $216,320 |
| Lower volume | −30 % incidents | ≈ $175,450 |
| LLM prices ×2 | all LLM lines doubled | $342,949 |
| Productionization +50 % | +$36,000 CAPEX | $311,173 |
| Azure warm DR | +$500/month | $329,173 |

**Takeaway for the jury:** LLM spend is under 4 % of platform cost. The ROI is driven by adoption and the automation mix, not by model pricing. That's why the gate policy and confidence calibration matter more than squeezing token costs.

---

## 18. Rubric Traceability Matrix

| Rubric | Requirement | Deliverable | Evidence shown in demo |
|---|---|---|---|
| Core Integration | Multi-agent orchestration | LangGraph graph + supervisor edges (§4.2, §7) | Agent timeline for scenario 7; graph diagram |
| Core Integration | Tool protocol | MCP streamable-HTTP server, 7 tools (§8) | `tools/list` output; MCP spans in Jaeger |
| Core Integration | Resilience | Breaker + idempotency + checkpoint resume (§8.3, §4.2) | Scenario 1 with fault → OPEN; kill agent-core mid-run → resumes |
| Telemetry & Audit | Tracing | OTLP → Jaeger, linked traces (§14.1) | Jaeger search by `incident.id` |
| Telemetry & Audit | 4 metrics | LPT, TCR, CHR, TFR (§14.2) | Metrics page |
| Telemetry & Audit | Immutable audit | Hash chain + triggers + roles (§14.3) | Verify chain ✓; attempted UPDATE fails |
| Contract Compliance | HITL for destructive ops | Per-step gate, single policy (§9) | Scenarios 3 (REVIEW), 1/4/5 (APPROVAL), 6/8 (ESCALATION) |
| Contract Compliance | Autonomous safe reset | `clear_pod_cache` AUTO (§9.2) | Scenarios 2, 7 |
| Contract Compliance | Security middleware | LlamaGuard + validators + OPA (§10, §11) | Scenario 9 quarantined; OPA deny reasons in audit |
| Contract Compliance | Zero trust | Signed scoped tokens, default deny (§11) | Replay an exec token → denied (`jti`) |
| Contract Compliance | DDD | Contexts ↔ modules (§22.1) | Container/component diagrams |
| Engineering Package | Runnable repo | Compose + README + tests (§19) | `docker compose up` → health_check green |
| Engineering Package | Architecture docs | 12 ADRs, 7 diagrams (§23) | Docs folder |
| Engineering Package | Financials | TCO/ROI + token model (§12, §17) | tco-roi-model.xlsx |

---

## 19. Testing & Evaluation (absent from v1)

### 19.1 Test pyramid
| Level | Scope | Tooling | Gate |
|---|---|---|---|
| Unit | Gate matrix (every cell and override), op-classifier, scrubber, normalizer, validators, canonical hashing, audit chain, breaker state machine, token claims | pytest | CI required |
| Policy | Every Rego deny rule (allow + deny case each) | `opa test` | CI required |
| Contract | Pydantic ↔ JSON Schema ↔ generated TS types stay in sync | pytest + `tsc --noEmit` | CI required |
| Integration | Compose up, `LLM_MODE=replay`, run all 9 scenarios; assert expected gate, final status, audit chain valid, OPA denials for forged tokens | pytest + `scripts/demo_runner.py` | CI required (from H8) |
| Red team | 20 injection payloads + 40 benign logs through intake | `tests/redteam` | Block ≥ 90 %, false positives ≤ 5 % |
| E2E UI | Login → approve scenario 1 → RESOLVED | Playwright | [SHOULD] |
| Chaos | Kill agent-core during EXECUTING; stop OPA; stop Ollama | `scripts/chaos.sh` | Resume OK / deny / degraded posture |

### 19.2 Security test cases
Each of these must be refused and audited:
- a forged token (wrong key)
- a READ token calling `apply_hotfix`
- an EXEC token replayed
- an EXEC token with changed args
- a `scale_pods` token approved as scale-up used for scale-to-0
- a wildcard namespace
- a cross-cloud call
- an UPDATE on `audit_log`

### 19.3 Eval harness (`scripts/eval_scenarios.py`, live mode, run at H8–H10)
- Runs each operational scenario 5× in live mode (8 × 5 = 40 incidents, about $2.60).
- Reports RCA accuracy vs `ground_truth.root_cause_category` (Haiku-4.5 judge + exact-match on category), action accuracy, gate correctness, and final-status accuracy.
- **Calibration table:** observed accuracy per confidence band. A band below its lower bound moves the thresholds in `gate_policy.yaml` (documented in ADR-004).
- The recorded responses become the replay fixtures.

---

## 20. Demo Plan & Offline Fallback

**Storyboard (~11 min, `docs/demo-script.md`):**
1. Architecture slide, then `docker compose up` already running with health_check green (1 min)
2. Scenario 7 → AUTO → RESOLVED; re-run → tier-1 cache hit, $0 LLM (1.5 min)
3. Scenario 3 → REVIEW countdown → Hold → converts to APPROVAL → approve (1.5 min)
4. Scenario 1 → APPROVAL modal: artifacts, per-step gates, approve → metrics recover (2 min)
5. Scenario 1 with `--inject-fault restart_service:3` → breaker OPEN → rollback → ESCALATED (1.5 min)
6. Scenario 9 → quarantined; show GUARDRAIL_ALERT and attack chain in audit (1 min)
7. Scenario 8 → ESCALATION in senior queue; log in as `lead1` (0.5 min)
8. Jaeger trace, Metrics page, Token ledger, **Audit verify** + failed UPDATE (2 min)

**Fallbacks:**
- `LLM_MODE=replay` (fixtures recorded at H9–H10) keeps the whole demo offline-capable.
- A phone hotspot is the backup network.
- A pre-recorded screen capture of the full run covers a hardware failure.

---

## 21. Cloud Reference Deployment & CI/CD

- **Helm chart** `helm/cloudscale-platform` with `values-eks.yaml` (primary) and `values-aks.yaml` (overlay). Templates:
  - the 4 app deployments, HPAs, and **NetworkPolicies** mirroring §4.1
  - an OPA sidecar in the mcp-server pod
  - a LlamaGuard deployment on a dedicated node pool
  - ExternalSecrets for the Anthropic key and the Ed25519 signing key
  - **external Postgres (RDS / Azure Database for PostgreSQL)**. SQLite is gone; v1's multi-replica HPA with SQLite could not work.
  - external Redis
  - an OTel collector
- **Service mesh (Istio/Linkerd)** provides mTLS in cloud.
- **Validation in the capstone:** `helm lint` + `helm template | kubeconform -strict` in CI. **Not deployed** during the 12 h, and the plan says so explicitly.
- **CI (`.github/workflows/ci.yml`):** ruff + mypy → unit → `opa test` → contract checks → compose integration (replay) → pip-audit / npm audit → Trivy image scan → helm lint/kubeconform.
- **CD (`deploy.yml`):** `workflow_dispatch` with input `cloud: eks|aks`, then `helm upgrade --install … -f values-${cloud}.yaml --set global.image.tag=$GITHUB_SHA`. This fixes v1's `deploy.yml` vs `deploy-eks/aks.yml` naming mismatch.

---

## 22. DDD, NFRs & Threat Model

### 22.1 Bounded contexts → code (v1 listed contexts with no mapping to modules)
| Context | Type | Code | Aggregates / key objects | Publishes | Relationship |
|---|---|---|---|---|---|
| Incident Management | Core | `api-gateway/routers/incidents.py`, `db/` | **Incident** (root), RemediationPlan (versioned), HITLDecision | IncidentReceived, PlanApproved, IncidentClosed | Upstream to Orchestration (customer/supplier) |
| Agent Orchestration | Core | `agent-core/graph`, `llm/` | AgentRun (per LangGraph thread), TriageResult | IncidentTriaged, PlanProposed, ExecutionCompleted | Consumes Incident; calls Tool Integration via ACL |
| Tool Integration | Supporting | `mcp-server/`, `agent-core/tools/mcp_client.py` (**ACL**) | ToolInvocation | ToolCallFinished | ACL translates plan steps ↔ MCP calls |
| Security & Policy | Supporting / **shared kernel** | `libs/cloudscale-common/{gate,safety,op_classifier}.py`, `opa/` | GatePolicy, GuardResult, ExecToken | GuardrailAlert, PolicyDecision | Shared kernel used by gateway, agent-core, mcp-server |
| Audit & Observability | Supporting | `audit_client.py`, `telemetry.py` | AuditEntry (append-only) | — | Conformist to all producers |
| Identity & Access, Notification, Configuration | Generic | `security/session.py`, `workers/event_fanout.py`, `pydantic-settings` | User, Role | — | — |

### 22.2 NFR matrix (`docs/nfr-matrix.md`)
| NFR | Target | Verified by |
|---|---|---|
| Latency | Triage p95 < 30 s (P1); AUTO end-to-end < 3 min | LPT histogram; integration test timing |
| Availability (prod) | 99.9 % gateway; agent crash → resume < 60 s | Chaos test |
| Safety | 0 destructive operations without APPROVAL/ESCALATION record | OPA `missing_approval` rule + integration assertions |
| Integrity | 100 % audit chain verifiable | `/audit/verify` in CI |
| Security | Red-team block ≥ 90 %, FP ≤ 5 % | §19 |
| Cost | ≤ $0.10 per incident (95th pct); hard cap $1.00 | Ledger |
| Observability | 100 % of tool calls traced + audited | Integration test checks span/audit counts |
| Scalability | 5 concurrent incidents locally; HPA in cloud | Load script (50 seeded incidents) |
| Data protection | No secrets in prompts; transcripts ≤ 30 days | Scrubber tests; retention job |

### 22.3 Threat model — OWASP Top 10 for LLM Applications **2025**
v1 used the older 2023 list (e.g., "Model Theft", "Training Data Poisoning").

| ID | Risk | Platform surface | Mitigations |
|---|---|---|---|
| LLM01 | Prompt Injection | `log_excerpt`, titles, tags | Data/instruction separation; heuristics + LlamaGuard → quarantine; structured outputs; gate + OPA ensure injected plans can't execute destructive ops without a human (scenario 9) |
| LLM02 | Sensitive Information Disclosure | Logs → prompts → transcripts/audit | Scrubber + IP pseudonymization; hashes in audit; transcript retention 30 days; RBAC on audit API |
| LLM03 | Supply Chain | Python/npm deps, container images, Ollama model | Hash-pinned lockfiles, pip-audit/npm audit, Trivy; model pulled by digest in `ollama-init` |
| LLM04 | Data & Model Poisoning | Scenario files, runbook catalogue, cache | `manifest.sha256` check at boot; cache writes only after verified resolution; invalidation on failure |
| LLM05 | Improper Output Handling | Plans, artifacts, UI rendering | Pydantic parsing; strict tool arg schemas; artifacts never executed; React escapes output; artifact validators |
| LLM06 | Excessive Agency | Executor tool access | Per-step gate; single-use step-bound exec tokens; OPA default deny; breaker; budget caps |
| LLM07 | System Prompt Leakage | Prompts in replay fixtures / transcripts | No secrets in prompts; fixtures contain scrubbed data only; transcripts RBAC-protected |
| LLM08 | Vector & Embedding Weaknesses | Tier-2 semantic cache | Scope-filtered similarity (cloud/namespace/service); verified-only writes; confidence cap on hits; re-validation of re-bound plans |
| LLM09 | Misinformation | Wrong RCA / overreliance | Composite confidence (evidence-capped); calibration table; deterministic verification; modal shows evidence vs LLM confidence |
| LLM10 | Unbounded Consumption | Large logs, loops, retries | 1 MB body limit; 8K-char log cap; `max_tokens` ceilings; per-incident $0.50/$1.00 caps; rate limits; re-plan limit 1 |

The attack trees in `docs/threat-model/` (prompt injection; tool abuse) now end at concrete controls. For example, the v1 tool-abuse tree's `namespace="*"` is rejected by the arg regex, then by OPA `wildcard_arg`, then by `namespace_scope`.

---

## 23. ADRs (12) & Diagrams (7)

| ADR | Title | Decision (one line) |
|---|---|---|
| 001 | Agent topology | Hierarchical: deterministic supervisor + 3 specialists; P2P and Swarm compared and rejected (merges v1 ADR-001/006) |
| 002 | Tool protocol | MCP (streamable HTTP) with platform-side enforcement; compared with direct REST and UTCP |
| 003 | State & persistence | Postgres (checkpoints, audit, ledger, vectors) + Redis (ephemeral coordination); SQLite rejected (multi-writer, multi-replica) |
| 004 | HITL gate | Per-step op-class × composite confidence via `interrupt()`; single policy file; calibration procedure |
| 005 | LLM selection | Sonnet 5 / Haiku 4.5 cascade; experiment vs single model at low effort; replay mode for determinism |
| 006 | Content safety layers | LlamaGuard 3 (1B) + deterministic validators; failure posture; measured targets |
| 007 | A2A | External federation via AgentCards; internal handoff stays in-graph |
| 008 | UTCP | Documented, not implemented: needs an enforcement point between agent and tool |
| 009 | A2UI | Documented, not implemented; typed WS event contract used instead |
| 010 | Zero trust | Signed scoped tokens (gateway-minted) + OPA default deny + network segmentation; mTLS via mesh in cloud |
| 011 | Semantic caching | Scope-filtered, verified-only writes, confidence cap, invalidation on failure |
| 012 | Multi-provider routing | Deferred: Anthropic-only with intra-provider failover for MVP; criteria for adding providers |

**Diagrams (`docs/architecture/`):**
1. `c4-context.excalidraw`
2. `c4-container.excalidraw`
3. `c4-component-agent-core.excalidraw`
4. `agent-sequence.excalidraw` (Triage → Planner → Gate → Executor → Verify)
5. `hitl-state-machine.excalidraw`
6. `topology-comparison.excalidraw`
7. `cloud-deployment-eks.excalidraw` (AKS differences annotated)

---

## 24. Risk Register

| # | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R1 | Anthropic API outage or venue network loss during demo | M | H | Replay mode; hotspot; recorded video | WS2 / WS7 |
| R2 | LangGraph/MCP SDK API differs from plan sketches | M | M | H0–H1 spike: minimal interrupt/resume + MCP call working before contracts freeze | WS2 / WS3 |
| R3 | LlamaGuard too slow or inaccurate on laptops | M | M | 1B model; measured at H1; degraded posture keeps safety via gate floor | WS4 |
| R4 | Integration slips | H | H | Contracts at H1; checkpoints H5/H8; freeze authority WS7 | WS7 |
| R5 | LLM confidence poorly calibrated | M | H | Evidence-capped composite confidence; calibration run; conservative matrix | WS2 / WS7 |
| R6 | Host RAM exhaustion (11 containers + Ollama) | M | M | 16 GB minimum; memory limits in compose; Jaeger/Prometheus memory caps | WS1 |
| R7 | Scope creep into stretch items | H | M | MoSCoW; nothing stretch before the H8 freeze is green | WS7 |
| R8 | Guard false positives block legitimate incidents | M | M | Benign-log suite; guards applied to untrusted fields only; heuristic-only hits floor rather than block | WS4 |

---

## 25. Execution Checklist (25 items)

1. [ ] H0: Repo scaffold, lockfiles (uv + npm), compose skeleton with 11 containers + ollama-init, health_check
2. [ ] H0: SDK spike: LangGraph `interrupt()`/`Command(resume)` with Postgres checkpointer; MCP streamable-HTTP call
3. [ ] H1: Freeze contracts in `libs/cloudscale-common` (state, schemas, WS events, audit events, gate_policy.yaml)
4. [ ] WS3: 7 MCP tools with strict arg models + `guarded_tool`
5. [ ] WS3: Stateful simulator, 9 scenario files with ground truth + simulation, fault injection, manifest.sha256
6. [ ] WS2: Graph: intake, cache_lookup, triage, planner, validate_plan, hitl_gate, execute, verify, close, escalate
7. [ ] WS2: Router (Sonnet 5 / Haiku 4.5), budgets, failover, replay/record modes, structured outputs
8. [ ] WS2: Token ledger (Postgres) + cost-per-MTTR KPI; tier-1 cache with verified-only writes
9. [ ] WS2/WS4: Gate evaluation + op-classifier + table-driven matrix tests
10. [ ] WS5: HITL endpoints, REVIEW deadline worker, APPROVAL→ESCALATION promoter, approve-with-modifications
11. [ ] WS5: Auth/RBAC (seeded users), session cookie, approver identity from session
12. [ ] WS4/WS5: Ed25519 READ/EXEC token minting (gateway) + verification (mcp-server), jti replay protection
13. [ ] WS4: OPA `mcp.rego` + generated data + `opa test` suite
14. [ ] WS4: Scrubber (IP pseudonymization), normalizer, heuristics, LlamaGuard client with degraded mode, validators
15. [ ] WS2/WS3: Circuit breaker (Redis) + retry policy by op class + idempotency keys
16. [ ] WS5: Hash-chained audit (triggers, roles, advisory lock) + `/audit/verify`
17. [ ] WS1/WS5: OTLP → collector → Jaeger + Prometheus; 7 metrics; `/metrics/live` PromQL
18. [ ] WS6: 5 pages + login, HITLModal, ReviewBanner, AgentTimeline, ArtifactViewer
19. [ ] WS6: TokenLedgerPanel, CacheHitBadge, RouterStatusPanel; native WS client with seq resume
20. [ ] H5: Integration checkpoint 1 (scenario 7 end-to-end); H8: all 9 scenarios green → **feature freeze**
21. [ ] WS7/WS4: Red-team suite + security test cases (§19.2); eval harness run + calibration table
22. [ ] WS1: CI pipeline (lint, unit, opa test, contract, integration replay, audits, Trivy, helm lint/kubeconform)
23. [ ] WS7: 12 ADRs, 7 diagrams, OWASP 2025 mapping + 2 attack trees, NFR matrix
24. [ ] WS7: `tco-roi-model.md/.xlsx` (NPV $257,994 · payback month 15 · BCR 2.54) + `token-economics-model.md` ($0.065/incident)
25. [ ] H10–H11: Record replay fixtures, 2 demo rehearsals, README, tag `v1.0`

---

## Appendix A — v1 Issue → v2 Fix

### A.1 Build/demo-breaking defects
| # | v1 issue | v2 fix |
|---|---|---|
| 1 | Retired model IDs (`claude-3-5-sonnet-20241022`, Claude 3 Haiku/Opus, Gemini 1.5, GPT-4o); stale pricing | `claude-sonnet-5` / `claude-haiku-4-5` (+ optional `claude-opus-5`), current prices (§12.1); other providers deferred (ADR-012) |
| 2 | Gate AUTO for destructive ops at ≥ 0.80; plan-level only; contradicted matrix | Per-step op-class × confidence matrix; DESTRUCTIVE never below APPROVAL; single policy file (§9) |
| 3 | REVIEW = `asyncio.sleep(60)`; Interrupt button inert | `interrupt()` + Redis deadline worker + Hold (§9.5) |
| 4 | LangGraph 0.2.28 lacks `interrupt()`; no Redis checkpointer package | LangGraph 1.x + `langgraph-checkpoint-postgres` (§5) |
| 5 | `tenacity.CircuitBreaker` doesn't exist; no HALF_OPEN; retries counted as failures; retries on non-idempotent ops | Custom Redis breaker; one failure per logical call; retries by op class; idempotency keys (§8.3) |
| 6 | REST `POST /tools/{name}` mislabeled as MCP | FastMCP streamable HTTP + MCP client session (§8.1) |
| 7 | Local `guardrails/` package shadows `guardrails-ai`; cross-service import | Package renamed `safety/` in `libs/cloudscale-common` (§5.1, §6) |
| 8 | `get_metrics` lacked scope args → always denied | Uniform `Scope` argument on all tools (§8.1) |
| 9 | Scenario 5 (P2) needed `apply_hotfix`, blocked by severity-based tokens | Permissions derive from the approved plan (§11.2) |
| 10 | 15-min JWT expires during HITL; write tools granted at ingestion | READ tokens (refreshable) + EXEC tokens minted post-decision, step-bound, single-use (§11.2) |
| 11 | Rego rules OR'd; rate-limit package never queried; scale-to-0 ignored | Single `decision` with deny-set AND semantics incl. rate, breaker, op-class escalation, missing approval (§11.3) |
| 12 | `socket.io-client` vs native FastAPI WebSocket; missing devDeps | Native WebSocket + typed events; full devDependencies (§5.1, §16.2) |
| 13 | Deprecated Jaeger exporter; missing PyJWT/numpy/openai/google deps | OTLP exporter; complete dependency list; lockfile (§5.1) |
| 14 | Cache: no `json.loads`, blocking `KEYS`, feedback never matched, missing `error_code`, cross-incident reuse | Scope-filtered Redis/pgvector cache, `error_signature`, verified-only writes, confidence cap, re-validation (§13) |
| 15 | Ledger: dict `rpush`; cache savings always 0; no date filter | Postgres `token_usage` with `avoided_cost_usd`; windowed SQL (§12.4) |
| 16 | Router: `create_task` in `__init__`; downgrade not applied; enum compare bug; costly probes | Rewritten router: budget downgrade, passive health, intra-provider failover (§12.2) |

### A.2 Internal contradictions
| v1 issue | v2 resolution |
|---|---|
| ADR count 9/10/12 | 12 everywhere; list in §23; ADR-001/006 merged, ADR-009 written |
| MCP tools 6 vs 7 | 7 everywhere |
| Services 5 vs 8 vs 9+; phantom Telemetry Sidecar :8002 | 4 app + 7 infra (+1 init); sidecar removed |
| 3 vs 4 agents | 1 deterministic supervisor + 3 specialist agents; 4 AgentCards |
| ESCALATION "no timeout" vs "auto-escalate after 10 min" | APPROVAL promotes to ESCALATION after 10 min; ESCALATION never times out (§9.5) |
| Triage "force HITL < 0.6" vs REVIEW for non-destructive | Confidence < 0.60 floors mutating steps at APPROVAL (§9.2) |
| "No plaintext channels" vs `http://` OPA/Ollama; claimed Redis TLS/browser TLS 1.3 | Honest network-segmentation MVP; mTLS stretch; mesh in cloud (§11.1) |
| "Hash-chained" without `prev_hash`; SQLite "no UPDATE permissions" | `prev_hash` chain, triggers, role grants in Postgres (§14.3) |
| $0.135 × 200 = $27/month vs $810/month; $0.054 vs $0.00632 per incident | One derivation: $0.0645/incident, ≈$13/month + $100 eval (§12.3, §17.3) |
| Gross savings $885,666 (should be $981,540); net $766,502 vs $862,376 | Fully recomputed model with a single consistent table (§17) |
| Maintenance "0.5 FTE × 20 h = $1,500" | 20 h × $150 = $3,000/month (§17.1) |

### A.3 Missing design areas now covered
| Gap | Where |
|---|---|
| Team allocation, critical path, integration checkpoints, buffer | §2, §3 |
| MVP cut line | §1 |
| Up-front interface contracts | §2 (H0–H1), `libs/cloudscale-common` |
| Demo storyboard + offline fallback | §20, §12.2 replay mode |
| Testing strategy, red team, eval & confidence calibration, ground truth | §7.3, §15.1, §19 |
| Guard false positives; IP over-redaction; LlamaGuard fail mode; Ollama model pull; hardware | §10, §4.1 |
| Static mocks, no fault injection | §8.4 |
| Scenario ↔ remediation mismatches; "cluster failover" with no tool | §15.2, MANUAL op class |
| Free-text rollback; rollback approval | `rollback: ToolCall` pre-approved with its step (§7.4, §11.2) |
| "Current replicas" unknown | Simulator state feeds op-class (§8.2) |
| Approve-with-modifications re-validation | §9.6 |
| Metrics backend; TFR not computable | Prometheus; `mcp.tool.calls` with outcome label (§14.2) |
| Audit ownership across services | Shared audit client, insert-only roles, advisory-lock chain (§14.3) |
| SQLite unusable in multi-replica cloud | Postgres everywhere (§14.4, §21) |
| Trace propagation & HITL-length traces | §14.1 |
| Supervisor logic, graph edges, error states | §4.2, §7.7 |
| Concurrency, dedup, idempotent ingestion; webhook ingestion | §4.2; Alertmanager adapter [SHOULD] |
| Mock auth, undefined `senior_sre` | §9.7 |
| Raw prompts in immutable audit; retention | §14.3, §14.4 |
| Helm lacking OPA/LlamaGuard/secrets; CI naming | §21 |
| Empty NFR matrix; DDD not mapped to code | §22.1, §22.2 |
| ROI assumed 100 % automation, no ramp, no failure cost, no productionization | §17 |
| No rubric traceability | §18 |
| Also corrected: OWASP list updated to 2025; UTCP/A2UI/MCP descriptions fixed | §22.3, §16.3 |
