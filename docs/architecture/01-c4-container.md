# C4 Level 2: Container view

What runs, how the containers talk, and where the trust boundaries are. Each dashed box is a Docker network: a container can only reach the containers it shares a network with.

```mermaid
flowchart LR
    sre([SRE / senior SRE])
    alerts([Alert sources<br/>Prometheus · Datadog])

    subgraph edge [edge network]
        console[Ops console<br/>Streamlit :8501]
    end

    subgraph core [orchestration]
        orch[Orchestrator<br/>FastAPI + LangGraph :8000<br/>agents · HITL gate · token signer]
        guard[Ollama<br/>Llama Guard 3 1B]
    end

    subgraph tools [tools network: only the orchestrator can reach it]
        mcp[MCP server :8001<br/>7 tools on a stateful simulator<br/>token verify → OPA → idempotency]
        opa[OPA<br/>default-deny policy]
    end

    subgraph data [data network]
        pg[(Postgres + pgvector<br/>checkpoints · audit chain<br/>semantic cache · token ledger)]
        redis[(Redis<br/>circuit breakers · jti<br/>idempotency · rate limits)]
    end

    subgraph obs [observability]
        jaeger[Jaeger<br/>traces]
        prom[Prometheus<br/>metrics]
        grafana[Grafana<br/>dashboard]
    end

    claude[Claude API<br/>Sonnet 5 · Haiku 4.5]

    sre -->|HTTPS| console
    alerts -->|incident JSON| orch
    console -->|REST + session token| orch
    orch -->|MCP streamable HTTP<br/>scoped Ed25519 token in _meta| mcp
    mcp -->|policy decision| opa
    orch -->|untrusted text only| guard
    orch -->|Messages API| claude
    orch --> pg
    orch --> redis
    mcp --> pg
    mcp --> redis
    orch -->|OTLP| jaeger
    mcp -->|OTLP| jaeger
    orch -->|OTLP| prom
    mcp -->|OTLP| prom
    grafana --> prom
    grafana --> jaeger
```

| Boundary | What enforces it |
|---|---|
| Browser → system | Session token (HS256, 8 h), role check on every route (`orchestrator/auth.py`) |
| Agent → tools | Single-use, step-bound Ed25519 exec token + OPA default deny + strict arg schemas (`mcp_server/enforcement.py`, `opa/policies/mcp.rego`) |
| Anything → audit history | `audit_writer` role has INSERT/SELECT only; triggers block UPDATE/DELETE/TRUNCATE even for the owner (`common/sql/001_core.sql`) |
| Untrusted text → LLM | Scrubber, heuristics, Llama Guard, `<incident_data>` tagging (`common/safety/`) |

**Prototype vs production.** In the prototype the orchestrator also holds the token-signing key (ADR-005). Internal hops are plain HTTP inside isolated Docker networks; in the cloud reference a service mesh adds mTLS (ADR-007).
