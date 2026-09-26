# Jury demo script (about 12 minutes + Q&A)

Timings are from the rehearsal on the dev VM (`scripts/rehearse.py`, 12/12 passed). Each segment names the rubric category it earns.

## 30 minutes before

1. `docker compose up -d`, then `uv run python scripts/rehearse.py`. It should print **12/12 passed**, or 11/11 without `--crash`. This is your go/no-go.
2. `uv run python scripts/demo_prep.py`: health, breaker reset, empty semantic cache, embedding model warm.
3. **LLM mode.** If the Anthropic key is still invalid, set `LLM_MODE=scripted` in `.env` and run `docker compose up -d orchestrator`. Otherwise every LLM call wastes about 1 s on two 401 failovers. Scripted mode is deterministic and works offline; say so openly if asked.
4. Open these tabs:
   - console at http://localhost:8501: one normal browser window logged in as `sre1`, one private window logged in as `lead1`
   - Grafana at http://localhost:3000
   - Jaeger at http://localhost:16686
   - `docs/architecture/01-c4-container.md` (or the Excalidraw export)
5. Keep a terminal open in `D:\CapStoneProject`.

## Storyboard

| # | Time | Show | Say (key points) | Rubric |
|---|---|---|---|---|
| 1 | 1:00 | Container diagram | "Hierarchical topology: a deterministic supervisor routes four specialist agents. Tools only through MCP, and every tool call needs a signed, single-use token plus an OPA decision." | Core Integration |
| 2 | 1:30 | Console → start **s02-cache-bloat** (about 10 s) | Show the agent timeline: triage → plan → **AUTO** gate → `clear_pod_cache` → the Evaluator polls metrics until SLOs are met → RESOLVED. "It verified the fix with metrics; the LLM didn't decide the outcome." | Core Integration, Contract |
| 3 | 1:00 | Start **s02 again** | "Semantic cache hit, similarity 1.0: no LLM call, $0. But the plan still goes through validation and the gate, with confidence capped at 0.79." Point at the token ledger panel. | Engineering (token optimization) |
| 4 | 2:00 | Start **s01-oom-orders** → Approvals tab | The plan with per-step op class, gate, **$ impact** and reasons. "Destructive, so an SRE must approve, bound to this exact plan version." Click **Approve**, then watch memory recover (about 25 s). | Contract (HITL) |
| 5 | 1:30 | Start **s03-payment-pool** | "$12k/minute service × 5 min disruption = $60k → **ESCALATION**. Watch: `sre1` can't approve it (button disabled)." Switch to the `lead1` window and approve. | Contract (financial threshold, roles) |
| 6 | 1:30 | Terminal: `docker compose exec orchestrator python scripts/fault.py inject restart_service 1`, then start and approve **s01** | "Restart fails → circuit breaker opens → the completed hotfix is **rolled back** automatically → escalated." Then `fault.py status` shows the OPEN breaker. | Core Integration (resilience) |
| 7 | 1:00 | Start **s05** and **s06** | "s05: injection in the alert, quarantined before any LLM or tool call. s06: the alert is clean, the injection is in the *pod logs a tool fetched*, and it's still quarantined. That's indirect injection." | Contract (guardrails) |
| 8 | 1:30 | Jaeger: service `orchestrator`, tag `incident.id=<s01 id>` | One trace from the HTTP request through every agent and into the **MCP server's own spans**. The HITL pause ends trace 1, and the resume is trace 2. | Telemetry |
| 9 | 1:00 | Grafana dashboard | The four jury metrics: **LPT** (p95 per agent), **TCR** (tokens/min), **CHR** (cache hit ratio), **TFR** (tool failure rate, visible from the fault in step 6). Plus cost, HITL wait and breaker state. | Telemetry (metrics) |
| 10 | 1:00 | Console → Audit log → **Verify chain** | "Every agent decision, tool argument and state change, hash-chained. Even the table owner can't UPDATE it." Optional terminal: `docker compose exec -T postgres psql -U cloudscale -d cloudscale -c "DELETE FROM audit_log WHERE seq=1"` gives *append-only (DELETE blocked)*. | Telemetry (audit) |
| 11 | 1:00 | `docs/financial/tco-roi-model.xlsx` Summary tab | "3-year net **$361k**, NPV $272k, payback month 14. Routing + cache cut LLM cost 45 %, but LLM spend is only 2 % of TCO: the ROI comes from adoption and automation mix, which is why the gate policy matters more than tokens." | Engineering (TCO/ROI) |

Optional if there's time: the crash demo. Start s02 and, while it's verifying, run `docker compose kill -s SIGKILL orchestrator`, then `docker compose up -d orchestrator`. The incident resumes from its checkpoint and resolves, and `INCIDENT_RESUMED` appears in the audit log.

## If something goes wrong

| Symptom | Do this |
|---|---|
| Console or API hangs | The dev VM is overloaded: `docker compose ps`; stop Ollama if needed (`docker compose stop ollama`: the guard then fails closed or degraded, which is itself a demo point) |
| Docker returns "500 Internal Server Error for API route" | Quit Docker Desktop, `wsl --shutdown`, start Docker Desktop again (≈2 min) |
| A scenario behaves unexpectedly | `scripts/fault.py reset`, then re-run; `scripts/run_scenario.py <id>` prints the full timeline in the terminal |
| Grafana panels say "no data" right after a restart | Metrics need two export intervals (≈20 s) and a few incidents; show `/metrics/live` or the console Metrics page instead |
| Everything is down | Walk through the rehearsal output and the docs; the diagrams, ADRs, TCO and 155 tests stand on their own |

## Likely jury questions

**Why not let an LLM supervise?** Routing must be reproducible and auditable, and cheap. Where judgement is needed (root cause, plan) we use an LLM; where it isn't (routing, gating, verification) we use code. ADR-001.

**What stops a prompt injection from running a destructive command?** Five things, in order:
1. It's caught at intake or in the tool output.
2. Even if it gets through, the plan is schema-checked, and there's no free-form shell tool.
3. The gate requires a human for destructive steps.
4. OPA denies destructive calls without a human approver.
5. The exec token is bound to the exact arguments that were approved.

**Isn't the orchestrator both agent and token signer?** Yes, in this prototype. That's a stated trade-off (ADR-005). A compromised orchestrator could forge an approval, but it would show up on the hash chain as a tool call with no matching human decision. Production moves the key to a separate approval service.

**How do you know confidence means anything?** It's `min(LLM, evidence)`, and the evidence score is deterministic, from a runbook catalogue with metric corroboration. The LLM can lower confidence but never raise it above the evidence. The blueprint adds a calibration run per confidence band.

**What does the Llama Guard layer actually do on this laptop?** Honestly: on the dev VM it rarely answers in time. So the design puts deterministic heuristics first, and fails closed on high-risk cues when the model can't answer. On normal hardware a 1B guard model is expected to answer in about a second, but that wasn't measured here.

**Why is LLM cost so low in the TCO?** At 200–340 incidents a month, even Sonnet costs about $0.12 per incident. The model shows the optimizations anyway (−45 %), because they scale with volume and cut latency. The costs are modeled; the token ledger will replace them with measured values once live runs are made.

**What's simulated?** The infrastructure: tools act on a stateful simulator, not a real cluster. Everything else is real: the MCP protocol, tokens, OPA, the audit chain, Postgres/pgvector, traces and metrics.
