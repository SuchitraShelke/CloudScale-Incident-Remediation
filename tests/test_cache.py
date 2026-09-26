"""Semantic cache on real pgvector (skipped without Postgres), with a deterministic fake embedder."""

import hashlib
import math

import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from cloudscale.common.scenarios import load_scenario
from cloudscale.orchestrator.cache import SemanticCache, bind_template, to_template


class FakeEmbedder:
    """Bag of hashed tokens -> 384-dim unit vector: identical text = similarity 1.0, unrelated text ~0."""
    dim = 384

    async def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in text.replace("=", " ").replace(",", " ").split():
            v[int(hashlib.sha256(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


@pytest_asyncio.fixture
async def cache(clean_db):
    pool = AsyncConnectionPool(clean_db, min_size=1, max_size=3, open=False)
    await pool.open()
    yield SemanticCache(pool, FakeEmbedder())
    await pool.close()


INC = load_scenario("s02-cache-bloat").incident.model_dump()
DEP = {"deployment": "image-resizer", "revision": 22}
KEY = "alert=ContainerMemoryHigh runbook=RB-CACHE-BLOAT signature=x breached=memory_usage_pct"
TRIAGE = {"root_cause": "unbounded cache", "root_cause_category": "unbounded_cache", "llm_confidence": 0.88,
          "affected_services": ["image-resizer"]}
PLAN = {"summary": "clear", "artifacts": [], "steps": [{
    "step_id": "clear-cache", "kind": "tool", "llm_claims_destructive": False, "estimated_impact": "",
    "runbook_ref": None, "rollback": None,
    "call": {"tool_name": "clear_pod_cache",
             "tool_args": {"scope": {"namespace": "media", "cloud_provider": "azure"}, "service": "image-resizer"}}}]}


async def test_store_then_hit_on_same_problem(cache):
    entry = await cache.store(INC, KEY, TRIAGE, PLAN, DEP, 0.0184)
    hit = await cache.lookup({**INC, "incident_id": "INC-S02-new"}, KEY)
    assert hit["entry_id"] == entry and hit["similarity"] == 1.0 and hit["original_cost_usd"] == 0.0184
    assert hit["plan_template"]["steps"][0]["call"]["tool_args"]["service"] == "{service}"


async def test_scope_filter_never_returns_another_services_entry(cache):
    await cache.store(INC, KEY, TRIAGE, PLAN, DEP, 0.01)
    assert await cache.lookup({**INC, "affected_service": "thumbnailer"}, KEY) is None
    assert await cache.lookup({**INC, "namespace": "production"}, KEY) is None


async def test_different_problem_is_a_miss(cache):
    await cache.store(INC, KEY, TRIAGE, PLAN, DEP, 0.01)
    assert await cache.lookup(INC, "alert=KubePodOOMKilled runbook=RB-MEM-OOM breached=error_rate_pct") is None


async def test_failed_reuse_invalidates_entry(cache):
    entry = await cache.store(INC, KEY, TRIAGE, PLAN, DEP, 0.01)
    assert await cache.feedback(entry, resolved=False) == "invalidated"
    assert await cache.lookup(INC, KEY) is None


def test_template_round_trip_rebinds_to_new_incident():
    plan = {"steps": [{"call": {"tool_args": {"scope": {"namespace": "media", "cloud_provider": "azure"},
                                              "deployment": "image-resizer"}},
                       "rollback": {"tool_args": {"revision": 22, "deployment": "image-resizer"}}}]}
    t = to_template(plan, INC, DEP)
    assert t["steps"][0]["rollback"]["tool_args"]["revision"] == "{revision}"
    other = {**INC, "namespace": "media-eu", "affected_service": "image-resizer"}
    bound = bind_template(t, other, {"revision": 40})
    assert bound["steps"][0]["call"]["tool_args"]["scope"]["namespace"] == "media-eu"
    assert bound["steps"][0]["rollback"]["tool_args"]["revision"] == 40


async def test_rerun_is_served_from_cache_and_still_gated(cache, mocked_backends):
    from tests.test_graph import build_env
    env = build_env(cache=cache)
    svc = env["service"]
    first = await svc.get(await svc.start("s02-cache-bloat", wait=True))
    second = await svc.get(await svc.start("s02-cache-bloat", wait=True))
    assert first["status"] == second["status"] == "RESOLVED"
    assert any(e.get("type") == "CACHE_WRITTEN" for e in first["events"])
    assert second["cache"]["hit"] and second["cache"]["similarity"] == 1.0
    assert [u["model"] for u in second["usage"][:2]] == ["semantic-cache", "semantic-cache"]
    assert "matrix:SAFE_MUTATION@0.79" in second["gate"]["steps"][0]["reasons"]      # confidence capped
    assert any(e.get("type") == "CACHE_PROMOTED" for e in second["events"])
