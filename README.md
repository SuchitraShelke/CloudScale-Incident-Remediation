# CloudScale Incident Remediation Platform

A multi-agent system that triages infrastructure incidents, plans fixes, and applies them through a zero-trust tool layer. Humans approve anything risky, and every decision lands in a tamper-evident audit log. Built for the *AI for Technical Architects* capstone (see `Capstone_evalutation_rubric.txt`).

- **Agents:** LangGraph with a deterministic supervisor, plus Triage, Planner, Executor and Evaluator.
- **Tools:** a real MCP server (streamable HTTP) with 7 tools on a stateful infrastructure simulator.
- **Governance:** a per-step HITL gate (confidence, operation class, financial impact, policy violations), layered guardrails, signed single-use tool tokens and an OPA default-deny policy.
- **Operations:** OpenTelemetry traces and metrics, a hash-chained audit log in Postgres, a semantic cache (pgvector), tiered Claude routing with failover, and a 3-year TCO/ROI model.

## Quick start

Requires Docker Desktop and [uv](https://docs.astral.sh/uv/).

```powershell
Copy-Item .env.example .env                  # scripted LLM mode works without an API key
uv sync
uv run python scripts/gen_keys.py            # Ed25519 token-signing keypair (gitignored)
uv run python scripts/gen_opa_data.py        # compile the gate policy into OPA data
docker compose up -d --build                 # first run pulls images and the 1.3 GB Llama Guard model
uv run python scripts/health_check.py
```

| Open | URL | Login |
|---|---|---|
| Ops console | http://localhost:8501 | `sre1`/`sre1` (SRE) · `lead1`/`lead1` (senior SRE) · `viewer`/`viewer` |
| API docs | http://localhost:8000/docs | `POST /auth/login` |
| Grafana dashboard | http://localhost:3000 | none (local demo) |
| Traces | http://localhost:16686 | search tag `incident.id` |

To use Claude, set `ANTHROPIC_API_KEY` and `LLM_MODE=live` in `.env`, then run `docker compose up -d orchestrator`.

## Demo scenarios

| Scenario | What happens | Shows |
|---|---|---|
| `s02-cache-bloat` | AUTO → `clear_pod_cache` → metrics recover → RESOLVED. Run it twice for a semantic-cache hit | Autonomous safe fix, verification, cache |
| `s01-oom-orders` | Hotfix is destructive → waits for an SRE → approve → RESOLVED | HITL approval, exec tokens, audit |
| `s01` + `scripts/fault.py inject restart_service 1` | Restart fails → breaker opens → hotfix rolled back → ESCALATED | Circuit breaker, compensation |
| `s03-payment-pool` | $60k impact → ESCALATION; an SRE can't approve it, a senior can | Financial-value threshold, roles |
| `s04-az-partition` | No tool can fail over an AZ → manual runbook handoff | Manual escalation path |
| `s05-direct-injection` | Injection in the alert → QUARANTINED before any LLM or tool call | Direct injection |
| `s06-indirect-injection` | Clean alert, injection in fetched logs → QUARANTINED | Indirect injection |

Run one from the terminal: `uv run python scripts/run_scenario.py s02-cache-bloat`. Run everything against its ground truth: `uv run python scripts/rehearse.py --crash`. See the security checks live: `docker compose exec orchestrator python scripts/mcp_smoke.py`.

## Tests

```powershell
uv run pytest -q                              # 155 tests; 14 use the compose Postgres and skip without it
docker run --rm -v "${PWD}\opa:/work:ro" openpolicyagent/opa:latest test /work/policies /work/data /work/tests
```

## Documentation (Engineering Package)

| Deliverable | Where |
|---|---|
| Architecture diagrams: C4 container, components, agent sequence, HITL state machine | [`docs/architecture/`](docs/architecture/) (Mermaid; import into Excalidraw via *More tools → Mermaid to Excalidraw*) |
| ADRs: topology, protocol, state & caching, HITL gate, guardrails & zero trust, model routing, cloud target | [`docs/adrs/`](docs/adrs/) |
| DDD bounded contexts | [`docs/ddd-context-map.md`](docs/ddd-context-map.md) |
| NFR matrix, with measured values | [`docs/nfr-matrix.md`](docs/nfr-matrix.md) |
| Threat model: OWASP LLM Top 10 (2025) | [`docs/threat-model.md`](docs/threat-model.md) |
| 3-year TCO / ROI + token economics | [`docs/financial/tco-roi-model.md`](docs/financial/tco-roi-model.md), [`.xlsx`](docs/financial/tco-roi-model.xlsx) |
| Jury demo script, fallbacks, likely questions | [`docs/demo-script.md`](docs/demo-script.md) |
| Target architecture (blueprint) | [`CapstoneProjectPlan_v2.md`](CapstoneProjectPlan_v2.md) |

## Built vs designed

This is a 2-day prototype of a larger blueprint. What the prototype actually does:

| Built and tested | Designed only (blueprint) |
|---|---|
| LangGraph pipeline, Postgres checkpoints, crash resume | React frontend (Streamlit console instead) |
| MCP server, 7 tools, stateful simulator (not real clusters) | Separate API gateway holding the signing key (merged into the orchestrator here, ADR-005) |
| HITL gate: APPROVAL / ESCALATION, roles, plan-version binding | REVIEW auto-proceed timer, approve-with-modifications |
| Guardrails, scrubber, validators, Ed25519 tokens, OPA | mTLS / service mesh, OIDC login |
| Circuit breaker, read retries, rollback, LLM failover | Helm charts and cloud deployment (ADR-007 is the target) |
| Hash-chained audit log, OTel traces + 7 metrics, Grafana | A2A federation, multi-provider LLMs |
| Semantic cache, token ledger, Claude routing | Live Claude run: built and tested with a fake client; pending a valid key |

**Dev-machine note:** built on a VMware VM running Docker in WSL2 (nested virtualization). Llama Guard is very slow there, so the guard runs heuristics first and fails closed on high-risk text (ADR-005).
