# ADR-006: Tiered model routing with budget caps and failover

**Status:** Accepted · **Rubric:** Core Integration (fallback routes), Engineering Package (tiered model routing)

## Context
Incident severity varies widely. A P1 on a payment path deserves the strongest reasoning; a P2 cache issue doesn't. LLM spend must be bounded per incident, and an API outage must not stop the platform.

## Decision
| Agent | P1 or tier-1 service | Otherwise |
|---|---|---|
| Triage, Planner | `claude-sonnet-5` (adaptive thinking, effort `medium`) | `claude-haiku-4-5` |
| Evaluator summary | `claude-haiku-4-5` | `claude-haiku-4-5` |

- **Budget:** incident spend ≥ $0.50 → Haiku only; ≥ $1.00 → no more LLM calls, escalate to a human.
- **Failover:** Sonnet error, timeout or refusal → Haiku once → scripted fallback. Each hop is recorded as a `PROVIDER_FAILOVER` event and on the token ledger.
- **Prompting:** trusted instructions, the runbook catalogue and tool schemas go in a cached system prompt. Untrusted incident data goes only inside `<incident_data>`. Output uses structured outputs with a Pydantic schema, then strict validation.
- **Determinism for demos:** `LLM_MODE=scripted` returns canned per-alert answers, used for tests and as an offline demo fallback.

## Alternatives considered
- **One model everywhere:** simpler, and one cache namespace, but at list prices all-Sonnet costs about 1.8× the routed mix (see `docs/financial/tco-roi-model.md`).
- **Multiple providers:** more resilience, but more keys, prompts and evaluation surface. Deferred; the failover chain already reaches a no-network fallback.

## Consequences
- The router is plain code, so its choices are testable (`tests/test_claude_llm.py`) and visible in the ledger.
- Haiku 4.5 takes no `effort` parameter; the client only sends it to Sonnet.
- **Verified here:** routing, budgets, failover and cost math, with a fake client, and the full failover chain live (the configured key returned 401, so every call fell back to scripted). **Not yet measured:** real token counts and costs from live Claude calls.

**In the code:** `orchestrator/llm/claude.py`, `orchestrator/llm/scripted.py`, `orchestrator/ledger.py`.
