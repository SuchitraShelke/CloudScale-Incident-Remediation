# ADR-001: Hierarchical agent topology with a deterministic supervisor

**Status:** Accepted · **Rubric:** Core Integration (multi-agent orchestration)

## Context
Incident remediation is a fixed sequence (understand → plan → check → act → verify), but each stage needs different tools, models and trust levels. Every routing decision has to be auditable and reproducible.

## Decision
A **hierarchical** topology in LangGraph:
- **Supervisor:** the graph's routing functions. Plain code, no LLM.
- **Specialists:** Triage (LLM + read-only tools), Planner (LLM), Executor (tool calls only) and Evaluator (deterministic SLO check + LLM summary).
- **State:** one typed `IncidentState` per incident, checkpointed in Postgres after every node (`thread_id = incident_id`).

## Alternatives considered
| Option | Why not |
|---|---|
| Peer-to-peer (agents hand off freely) | Routing becomes emergent: hard to audit, and an injected agent could route around the gate |
| Swarm / LLM supervisor | Costs a model call per hop, adds latency, and routing isn't reproducible for the jury or a post-mortem |
| One agent with all tools | No separation of duties: the component that plans also executes |

## Consequences
- Every hop is cheap, deterministic and recorded as an audit event.
- The Executor can't plan and the Planner can't execute; only the gate connects them.
- Crash-safe: after a restart, the startup sweeper resumes in-flight graphs from their last checkpoint.
- Less flexible for open-ended investigations. That's acceptable for a remediation workflow.

**In the code:** `orchestrator/graph.py` (nodes + routing), `orchestrator/service.py` (`resume_in_flight`), `tests/test_graph.py`.
