# ADR-005: Layered guardrails and zero-trust tool access

**Status:** Accepted · **Rubric:** Contract Compliance (guardrails & security)

## Context
Alert payloads and fetched logs are attacker-controllable, and the LLM output drives real infrastructure changes. The rubric names indirect prompt injection, data exfiltration and unauthorized privileges.

## Decision
**Guardrail layers on text going *into* the LLM:**
1. **Scrubber:** redacts secrets (passwords, tokens, JWTs, AWS/Azure keys, connection strings, emails) and replaces IPs with stable per-incident pseudonyms.
2. **Heuristics:** strong injection signatures quarantine immediately; ambiguous cues pick which text windows need a model check.
3. **Llama Guard 3 (1B)** on those windows only, with platform categories S1–S4 (injection, dangerous commands, exfiltration, out-of-scope tool abuse).
4. **Fail-closed:** if Llama Guard can't answer, high-risk cues quarantine and anything else sets `degraded`, which floors the gate at APPROVAL.

These layers run on the alert payload **and on every tool output**, which is where indirect injection arrives (scenario s06).

**Validators on output *from* the LLM:** artifact rules (no `rm -rf /`, `curl | sh`, privileged containers, hostPath, inline secrets…), an egress allowlist, and hotfix bounds. The model's output is also schema-checked and never executed as text.

**Zero trust for tools:**
- **READ tokens** at intake. **EXEC tokens** only after a gate decision: single-use, bound to one step, tool, op class and args hash, and carrying the approver.
- The MCP server has only the **public key**. OPA default-denies, and a malformed input is denied too.

## Accepted trade-off
In the prototype the orchestrator both runs the agents and signs exec tokens, because a separate gateway service didn't fit a 2-day solo build. What still holds: the code mints exec tokens only after a recorded gate decision; OPA requires a human approver claim on any destructive call; and every decision and tool call is on the hash chain. What doesn't: a *compromised* orchestrator holds the key, so it could forge that approver claim. The audit chain then makes the forgery **detectable** (a destructive call with no matching `HITL_DECISION`), but not preventable. **Production:** a separate approval service holds the signing key, so a compromised agent can't approve itself.

## Why heuristics come first
On the dev VM (nested virtualization) a Llama Guard call takes 15–45 s, and under load often never finishes. Heuristics clear most benign log text instantly; the model runs only where it adds information.

**In the code:** `common/safety/`, `mcp_server/enforcement.py`, `opa/policies/mcp.rego` (17 `opa test` cases), `scripts/mcp_smoke.py`.
