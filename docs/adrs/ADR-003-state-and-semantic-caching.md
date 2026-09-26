# ADR-003: Postgres (pgvector) for durable state, Redis for coordination, safety-first semantic cache

**Status:** Accepted · **Rubric:** Core Integration (state management), Engineering Package (token optimization)

## Context
The system needs crash-safe agent state, an immutable audit log, a vector index for the semantic cache, and fast shared counters for breakers, rate limits and token replay protection.

## Decision
| Store | Holds | Why |
|---|---|---|
| **Postgres 16 + pgvector** | LangGraph checkpoints, incidents, hash-chained audit log, semantic cache (HNSW, cosine), token ledger | One durable, transactional store; role grants and triggers make the audit log append-only |
| **Redis 7** | Circuit breakers, `jti` single-use marks, idempotency results, rate counters, guard verdict cache | Fast shared state with TTLs; losing it is safe (calls fail closed or re-check) |

**Semantic cache design.** Embeddings run locally with `BAAI/bge-small-en-v1.5`, so no second provider is needed.
- **Key text:** alert name, matched runbook signature, and which SLOs are breached.
- **Lookup:** filtered to the same cloud, namespace and service, with cosine similarity ≥ 0.92.
- **Writes:** only after a *verified* RESOLVED outcome. An unverified diagnosis never enters the cache.
- **Hit:** no triage or planning call. The plan is re-bound to the new incident and goes through validation and the gate again, with confidence capped at 0.79 (so it never auto-runs a disruptive step).
- **Feedback:** a hit that resolves extends the entry's TTL; one that fails deletes the entry.

## Alternatives considered
- **SQLite:** no concurrent writers, no role-based permissions, can't serve multiple replicas.
- **Redis checkpointer:** weaker durability for the one thing that must survive a crash.
- **Exact-match cache only:** misses near-identical incidents. Kept implicit: identical key text gives similarity 1.0.
- **A dedicated vector DB:** one more thing to run; pgvector is enough at this scale.

## Consequences
- A re-run of a known problem costs $0 in LLM calls (see the `CACHE_HIT` / `CACHE_PROMOTED` audit events).
- Cache poisoning via a wrong diagnosis is blocked by verified-only writes and invalidation on failure (OWASP LLM04/LLM08).

**In the code:** `orchestrator/cache.py`, `common/sql/`, `tests/test_cache.py`, `tests/test_audit.py`.
