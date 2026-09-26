# ADR-002: MCP for tools, with the enforcement point in the MCP server

**Status:** Accepted · **Rubric:** Core Integration (protocol & tool integration), Day 2 protocols

## Context
Agents need to call infrastructure tools (metrics, logs, restart, hotfix, rollback) with strong guarantees: scoped permissions, exact-argument approval, idempotency and an audit trail.

## Decision
A real **Model Context Protocol** server (mcp SDK 2.x `MCPServer`, streamable HTTP, stateless) exposes 7 tools with strict JSON-Schema arguments. Every call carries a scoped token in the request `_meta` and passes through one enforcement path: verify token → OPA → idempotency → single-use check → execute → audit.

## Protocols compared
| Protocol | Verdict | Reason |
|---|---|---|
| **MCP** | **Chosen** | Tool discovery (`tools/list`), typed inputs, HTTP transport. MCP has no built-in policy or audit, so we add them in the server, where they can't be bypassed |
| UTCP | Rejected | The agent calls tools directly over their native interfaces. There is no point in the middle to enforce tokens, OPA and audit, and we need exactly that |
| A2A | Not needed internally | Agent handoffs happen inside one LangGraph graph. A2A fits federation with *external* agents (future: exposing Triage as a service) |
| A2UI | Not used | A declarative agent-generated UI format. Our console shows typed API data; approval forms stay hand-built so their wording can't be influenced by a model |
| Plain REST | Rejected | We'd have to reinvent discovery and schemas, and it wouldn't be MCP |

## Consequences
- Per-call tokens live in `_meta`, not HTTP headers. An exec token is bound to one step, while headers are set per session. This also carries the trace context, since the MCP client uses `httpx2`, which OTel's httpx instrumentation doesn't see.
- A stale or replayed token can never cause a second execution: the idempotency store answers first, then single-use `jti` protects the first call.

**In the code:** `mcp_server/server.py`, `mcp_server/enforcement.py`, `common/tool_args.py`, `scripts/mcp_smoke.py` (11 live security cases).
