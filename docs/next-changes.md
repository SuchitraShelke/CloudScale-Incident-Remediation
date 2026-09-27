# Next changes: HITL timeout ladder, escalation hand-off, recorded fixes

Agreed with the user on 2026-09-27. Build A → B → C, run `uv run pytest -q` and `scripts/rehearse.py --crash`, then
ASK before committing. Never auto-approve on timeout.

## A. Timeout fallback ladder (never approves anything)
- Settings (`common/config.py`): `hitl_promote_after_s` (default 600), `hitl_expire_after_s` (default 1800).
  Compose demo profile: `HITL_PROMOTE_AFTER_S=120`, `HITL_EXPIRE_AFTER_S=240`.
- `graph.py` hitl_gate: add `requested_at` (time.time()) to the interrupt payload (only for display/timers; the
  node re-runs on resume, the payload is recomputed, harmless). New decision outcome `EXPIRE` (system only):
  `Command(goto="evaluate", update={"status": "VERIFYING", "results": [], "timed_out": True, events})`.
  State gets `timed_out: bool`.
- `graph.py` evaluate: if `state["timed_out"]`: poll metrics ONCE; healthy → RESOLVED with message
  "recovered without action; nothing was changed"; else → ESCALATED "no decision in time; nothing was changed".
- `service.py`: `async def sweep_deadlines(now=None) -> list[str]` over non-terminal incidents with a pending
  decision:
  - APPROVAL pending ≥ promote_after → resume with `{"outcome": "ESCALATE", "approver": "system:hitl-timer",
    "role": None, "plan_hash": <pending>, ...}` + audit `HITL_PROMOTED` (actor system:hitl-timer).
  - ESCALATION pending ≥ expire_after → resume with `{"outcome": "EXPIRE", ...}` + audit `HITL_EXPIRED`.
  - "overdue" = pending ≥ promote_after/2 → exposed as `overdue: true` in `list()` / queue rows (console badge).
  - The hitl_gate must accept ESCALATE/EXPIRE from `approver == "system:hitl-timer"` (plan_hash must match).
- `main.py` lifespan: background task calling `sweep_deadlines()` every 15 s (cancel on shutdown).
- Console: overdue badge in Approvals; show "auto-escalates in / expires in" countdown from `requested_at`.

## B. Escalation hand-off summary
- On close with status ESCALATED (incl. timeout), `close` node builds `handoff` in state (no LLM):
  diagnosis + LLM/evidence confidence, gate reasons, plan summary + steps, artifact filenames, what was
  executed / rolled back / nothing changed, audit link hint. Audit `HANDOFF_CREATED` with that payload
  (production: ticket + page; document as designed).
- `GET /incidents/{id}/handoff` returns it as markdown; console shows it (expander) + copy button.

## C. "Record how it was fixed" → re-verify → proposed runbook
- `POST /incidents/{id}/resolution` (sre/senior_sre) body `{how_fixed: str, root_cause_category: str}` on an
  ESCALATED incident: re-read metrics once via a READ token (tools client) → `verified: bool`.
- Store `resolution` in a new table `manual_resolutions` (incident_id PK, by, how_fixed, category, verified, ts)
  and if verified, a `runbook_proposals` row: `{id: RB-PROPOSED-<n>, root_cause_category, signatures: [error
  lines from the incident logs, escaped, up to 2], corroborate: {metric: worst breached SLO metric, above: SLO},
  source_incident, how_fixed, status: "proposed"}`. NOT added to runbooks.yaml automatically (human review).
  Audit `MANUAL_RESOLUTION_RECORDED`, `RUNBOOK_PROPOSED`. Nothing written to the semantic cache.
- `GET /runbooks/proposals` lists them; console: "Record fix" form on ESCALATED incidents + a proposals list.

## Tests (fake clock / in-process)
- promote after deadline → AWAITING_ESCALATION, audit HITL_PROMOTED by system:hitl-timer
- expire → ESCALATED, no simulator actions; expire with healthy metrics → RESOLVED, no actions
- a human decision before the deadline wins; timer never approves (no EXEC token minted by the timer)
- handoff present on ESCALATED with diagnosis + "nothing changed"
- resolution: unverified (metrics still bad) → no proposal; verified → proposal with signatures; viewer 403

## Docs
ADR-004 (timeout ladder), docs/architecture/04-hitl-state-machine.md, demo-script (show promotion), README.
