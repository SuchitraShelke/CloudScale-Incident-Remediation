# Data stores: Postgres (+ pgvector) and Redis

**Postgres holds everything that must survive and be trusted later. Redis holds fast, short-lived coordination data that is safe to lose.**

## Postgres: the system of record

| Table | What it stores | Why it's needed |
|---|---|---|
| **LangGraph checkpoints** (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`) | The full state of every incident after **every** agent step: diagnosis, plan, gate, results | **Crash recovery and HITL pauses.** If the orchestrator is killed mid-incident, the startup sweeper resumes from the last checkpoint. An approval waiting for hours lives here too, and so does the timeout's start time |
| **`incidents`** | The ID, the scenario or source, and an insertion sequence number | The incident list, and which incidents the sweeper and timer look at. Ordered by sequence, not clock, because a VM clock can jump |
| **`audit_log`** | Every decision, tool call with its arguments, policy verdict, state change and human decision | **Compliance and post-mortems.** Hash-chained (each row includes the previous row's hash). Database triggers block UPDATE, DELETE and TRUNCATE even for the table owner ([`001_core.sql`](../src/cloudscale/common/sql/001_core.sql)) |
| **`semantic_cache`** (**pgvector**) | A 384-number **embedding** of each verified incident, plus its diagnosis and plan template | **Semantic caching:** finds a similar past incident by meaning, not exact text, and skips the LLM ($0) on a hit |
| **`token_usage`** | Every LLM call: agent, model, tokens in/out/cached, cost, cost avoided, failover | **Token economics:** cost per incident and cache savings. The TCO model uses these **measured** numbers |
| **`manual_resolutions`** | How a human fixed an escalated incident, and whether the metrics verified it | Learning from humans |
| **`runbook_proposals`** | Draft runbook entries from verified fixes, waiting for review | Autonomy that grows only through review |

The schema is in [`src/cloudscale/common/sql/`](../src/cloudscale/common/sql/); migrations are applied at startup.

### Why pgvector

The cache compares incidents **by meaning**. "Memory climbing steadily in image-resizer" and "image-resizer memory high" are different strings but mean the same thing.
- **The embedding:** each incident's key text (service, evidence, which metrics are breached) is turned into a vector by a small **local** embedding model (`BAAI/bge-small-en-v1.5`, so no API cost).
- **The lookup** ([`cache.py`](../src/cloudscale/orchestrator/cache.py)):
  - It's filtered first on **cloud, namespace and service**, so a fix is never reused on a different service.
  - Then it runs a **cosine similarity** search with an **HNSW index** (a fast nearest-neighbour index).
  - A hit needs a similarity of **≥ 0.92**.
- **Safety:** only **verified, resolved** incidents are written. A cached plan is re-validated and re-gated, with confidence capped at 0.79.

**Why not a separate vector database** (Pinecone, Weaviate)? One database means one backup, one security boundary, and transactions shared with the rest of the data. At our volume (hundreds of incidents, not millions) pgvector is plenty. See ADR-003.

## Redis: shared short-term memory

Redis is a very fast in-memory store that **every service shares**. We use it as a **shared whiteboard** for small facts that several processes need to agree on *right now*. Nothing important lives only there, and everything on it expires automatically.

**Redis is mostly not a cache.** Only one use (the guard verdicts) is a cache. The others are **shared state and coordination**: they don't store copies of answers to save work; they store facts the services must agree on.

| Key | Kind | Used by | What it's for |
|---|---|---|---|
| **`breaker:{tool}:{cloud}`** | Shared state | Orchestrator (tool client + executor pre-flight); MCP server reads it | Circuit breaker state: CLOSED / OPEN / HALF_OPEN, the failure count, when it opened |
| **`breaker:…:probe`** | Lock | Orchestrator | The HALF_OPEN test-call lock (`SET NX` with an expiry): **exactly one** test call after the cool-down |
| **`jti:{token id}`** | Security check | MCP server | Marks an exec token as used (`SET NX`, 15-min expiry) |
| **`idem:{key}`** | Safe-retry result | MCP server | The stored result of a mutation, under its idempotency key |
| **`rate:{incident}:{minute}`** | Counter | MCP server (fed to OPA) | Calls per incident per minute, for OPA's rate limit (expires after 2 minutes) |
| **`guard:{hash}`** | **A real cache** | Orchestrator (guard in intake and triage) | Cached LlamaGuard verdicts for a text window |

### Each use, with an example

**1. Circuit breaker: "is this tool broken right now?"**
Say `apply_hotfix` on AWS starts failing:
1. Each failed call raises the failure count in `breaker:apply_hotfix:aws`.
2. After 3 failures in a row (1 in the demo profile) the state becomes **OPEN**.
3. **Every other incident** checks Redis before executing, sees OPEN, and escalates immediately instead of trying a fix that can't work.
4. After 30 s, **exactly one** incident may send a test call; the `…:probe` lock ensures only one gets it, even if ten incidents try at once.
5. If the test succeeds, the breaker is CLOSED again for everyone.

Redis is needed because the state must be **shared** across all incidents and both services. A variable in one process can't do that. See [`breaker.py`](../src/cloudscale/orchestrator/breaker.py).

**2. Single-use tokens: "has this approval ticket been used already?"**
Each approved step gets a signed exec token, a one-time ticket. The MCP server writes `jti:<token id>` with `SET NX` ("set only if it doesn't exist yet"):
- **First use:** the key doesn't exist, so it's written and the call is allowed.
- **Replay:** the key exists, the write fails, and the call is **refused**.

`SET NX` is **atomic**, so two copies arriving at the same millisecond can't both succeed.

**3. Idempotency: "we already did this exact change, return the same answer"**
If the network drops the reply after a hotfix was applied, the caller may send the same request again. After a mutation succeeds, its result is saved under `idem:<key>`, built from *incident + plan + step + arguments*. A repeat returns the **saved result without running the change again**.

The difference from single-use tokens: single-use *rejects* a copied token; idempotency *safely answers* a genuine retry.

**4. Rate limit counter: "is this incident hammering the cluster?"**
Every call increments `rate:<incident>:<minute>`. The count goes to OPA, which denies calls once an incident reaches 30 per minute. That stops an agent stuck in a loop.

**5. Guard cache: "we already checked this text"**
LlamaGuard takes 15–45 seconds per check on the dev host. Each verdict is saved under `guard:<hash of the text>`, so the same log text seen again gets its answer instantly.

## The agents' caches are not in Redis

| Cache | Where | What it saves |
|---|---|---|
| **Semantic cache** | **Postgres + pgvector** | A similar, previously verified incident skips the triage and planner LLM calls: $0, faster |
| **Prompt cache** | **OpenAI, provider side** | The fixed part of each prompt (instructions + runbook catalogue) is billed at the cached rate. 76% of input tokens came from it |

**About L1 / L2:** the blueprint ([`CapstoneProjectPlan_v2.md`](../CapstoneProjectPlan_v2.md)) designed two agent cache tiers:
- **Tier 1:** an exact-match cache in Redis. The key is a hash of cloud + namespace + service + alert name + error signature, kept for 24 h.
- **Tier 2:** the semantic cache in pgvector.

The prototype built **only Tier 2**. An exact repeat already scores similarity 1.0 in pgvector (the s02 re-run shows this), so a separate exact-match tier would only save the embedding step, which is local, takes milliseconds and costs nothing. The LLM call is the expensive part, and both tiers skip it. In production at high volume, a Redis tier in front would take load off Postgres.

## If one of them is lost

| Lost | Effect |
|---|---|
| **Redis** | Nothing important is lost. Breakers reset to CLOSED, the rate counter restarts, the guard cache refills. In the short window before a token expires, a replay *could* pass the single-use check, but OPA, the argument hash and the idempotency key still limit what it could do |
| **Postgres** | **Serious:** incident state, the audit trail and the cache are gone. That's why Postgres is backed up, and why production uses a managed, replicated database (RDS / Azure Database for PostgreSQL, ADR-007) |

**In one line:** Postgres is our memory and our evidence: checkpoints for recovery, the hash-chained audit log, the pgvector semantic cache and the cost ledger. Redis is our reflexes: shared circuit breakers, single-use token checks, idempotency and rate counting, plus a LlamaGuard verdict cache. All of it expires, so losing Redis costs speed, not correctness.
