# C4 Level 3: Orchestrator components

The agent pipeline is a LangGraph `StateGraph` with a hierarchical topology. The supervisor is the routing code between nodes: deterministic, with no LLM. Only Triage, Planner and the Evaluator's summary call an LLM.

```mermaid
flowchart TB
    api[HTTP API<br/>api.py · auth.py] --> svc[IncidentService<br/>service.py]
    svc -->|one thread per incident| pipeline

    subgraph pipeline [LangGraph pipeline: graph.py]
        intake[Intake<br/>scrub · normalize · guard]
        triage[Triage agent<br/>READ tools · tool-output guard<br/>evidence score · semantic cache]
        planner[Planner agent<br/>structured plan · artifacts]
        validate[validate_plan<br/>arg schemas · validators · op-class]
        gate[HITL gate<br/>interrupt unless AUTO]
        execute[Executor<br/>exec tokens · breaker · rollback]
        evaluate[Evaluator<br/>SLO polling · summary]
        close[Close<br/>cache write / feedback]
        intake --> triage --> planner --> validate
        validate -->|invalid, retry once| planner
        validate --> gate --> execute --> evaluate --> close
    end

    llm[RoutedLLM: llm/claude.py · llm/openai_llm.py<br/>deep → fast → scripted]
    cache[SemanticCache<br/>cache.py · pgvector]
    tools[ToolClient: tools.py<br/>circuit breaker · READ retries · trace context]
    audit[AuditLog<br/>common/audit.py]
    ledger[TokenLedger<br/>ledger.py]
    ckpt[(Postgres checkpointer<br/>crash-safe resume)]

    triage -.-> llm
    planner -.-> llm
    evaluate -.-> llm
    triage -.-> cache
    close -.-> cache
    triage -.-> tools
    execute -.-> tools
    evaluate -.-> tools
    svc --> audit
    svc --> ledger
    pipeline --- ckpt
```

| Component | Responsibility | Key file |
|---|---|---|
| IncidentService | Starts and resumes graph runs, streams every node's events into the audit log and ledger, resumes in-flight runs after a restart | `orchestrator/service.py` |
| Gate | Pure, deterministic evaluation from `gate_policy.yaml`; safe to re-run on resume | `common/gate.py` |
| ToolClient | The anti-corruption layer between plan steps and MCP calls | `orchestrator/tools.py` |
| Guard | Heuristics first, Llama Guard only on suspicious windows, fail-closed on high-risk cues | `common/safety/llama_guard.py` |
