# Demo screenshots

Captured 2026-09-29 from the running stack in `LLM_MODE=scripted` (deterministic answers, no API calls), following the storyboard in [`../demo-script.md`](../demo-script.md). The screens are the same in live mode; only the model names and token counts in the ledger differ.

Regenerate: `uv run python scripts/demo_prep.py`, then `uv run --with playwright python scripts/capture_screenshots.py`.

| # | Screenshot | What it shows | Storyboard step |
|---|---|---|---|
| 1 | [01-login](01-login.png) | Console login, demo users | — |
| 2 | [02-s02-auto-resolved](02-s02-auto-resolved.png) | s02: `clear_pod_cache` is SAFE_MUTATION → **AUTO**; Evaluator polls metrics back under SLO → RESOLVED; fix written to the semantic cache | 2 |
| 3 | [03-s02-semantic-cache-hit](03-s02-semantic-cache-hit.png) | s02 again: semantic cache hit (similarity 1.0), no LLM call, confidence capped at 0.79, still gated and verified | 3 |
| 4 | [04-s01-approval-pending](04-s01-approval-pending.png) | s01: `apply_hotfix` is DESTRUCTIVE → **APPROVAL**; per-step op class, gate, $ impact, plan version | 4 |
| 5 | [05-s01-approved-resolved](05-s01-approved-resolved.png) | s01 after `sre1` approves: hotfix + restart run, memory recovers, RESOLVED | 4 |
| 6 | [06-s03-escalation-sre-blocked](06-s03-escalation-sre-blocked.png) | s03: $60k impact > $50k → **ESCALATION**; `sre1` sees Approve disabled | 5 |
| 7 | [07-s03-senior-can-approve](07-s03-senior-can-approve.png) | Same incident as `lead1` (senior SRE): Approve enabled | 5 |
| 8 | [08-s05-direct-injection-quarantined](08-s05-direct-injection-quarantined.png) | s05: injection in the alert → QUARANTINED at intake, before any LLM or tool call | 7 |
| 9 | [09-s06-indirect-injection-quarantined](09-s06-indirect-injection-quarantined.png) | s06: clean alert, injection in fetched pod logs → QUARANTINED by the tool-output guard | 7 |
| 10 | [10-metrics](10-metrics.png) | Console metrics: LPT, TCR, CHR, TFR, cost, HITL wait, breakers | 9 |
| 11 | [11-audit-chain-verified](11-audit-chain-verified.png) | Audit log with **Verify chain**: hash chain intact | 10 |
| 12 | [12-report-incident](12-report-incident.png) | Report incident form (pre-filled with an unseen incident) | optional |
| 13 | [13-grafana-dashboard](13-grafana-dashboard.png) | Grafana "CloudScale Incident Platform" dashboard | 9 |
| 14 | [14-jaeger-trace-s01](14-jaeger-trace-s01.png) | Jaeger: the s01 resume trace (89 spans) from the decision request through execute/evaluate into the MCP server's own spans | 8 |

Not captured: the fault-injection / circuit-breaker run (storyboard step 6).
