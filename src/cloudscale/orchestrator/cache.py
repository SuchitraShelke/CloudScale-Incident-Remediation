"""Semantic cache (pgvector), safety-first:

- lookup is filtered to the same cloud + namespace + service, cosine similarity >= 0.92
- entries are written only after a VERIFIED resolution (unverified RCAs never enter the cache)
- a hit caps confidence at 0.79 in the gate (never AUTO for DISRUPTIVE) and the re-bound plan goes
  through validate_plan + the gate again
- a hit that doesn't resolve deletes the entry; one that does extends its TTL
"""

import asyncio
import json
from datetime import timedelta
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from cloudscale.common.runbooks import slo_thresholds

TTL = timedelta(hours=24)
MAX_TTL = timedelta(days=7)


class Embedder(Protocol):
    dim: int

    async def embed(self, text: str) -> list[float]: ...


class FastEmbedder:
    """Local BAAI/bge-small-en-v1.5 (384-dim) via fastembed; baked into the image, no API key."""
    dim = 384

    def __init__(self, cache_dir: str | None = None):
        self._model = None
        self._cache_dir = cache_dir

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=self._cache_dir)
        return self._model

    async def embed(self, text: str) -> list[float]:
        def run() -> list[float]:
            return [float(x) for x in next(iter(self._load().embed([text])))]
        return await asyncio.to_thread(run)


def cache_key_text(inc: dict, evidence: dict, metrics: dict[str, float]) -> str:
    """What makes two incidents 'the same problem': alert, matched signature, which SLOs are breached."""
    breached = sorted(k for k, t in slo_thresholds().items() if metrics.get(k, 0) >= t)
    return (f"alert={inc['alert_name']} runbook={evidence.get('runbook_id') or 'none'} "
            f"signature={evidence.get('signature') or 'none'} breached={','.join(breached) or 'none'}")


# ---------- plan templates: bind to the current incident on reuse ----------

def _walk(obj: Any, fn, key: str | None = None):
    """Apply fn(key, value) to every scalar; key is the nearest enclosing dict key."""
    if isinstance(obj, dict):
        return {k: _walk(v, fn, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, fn, key) for v in obj]
    return fn(key, obj)


def to_template(plan: dict, inc: dict, deployment: dict) -> dict:
    subs = {inc["namespace"]: "{namespace}", inc["affected_service"]: "{service}",
            inc["cloud_provider"]: "{cloud_provider}"}

    def fn(key, v):
        if key == "revision" and v == deployment.get("revision"):
            return "{revision}"
        return subs.get(v, v) if isinstance(v, str) else v
    return _walk(plan, fn)


def bind_template(template: dict, inc: dict, deployment: dict) -> dict:
    subs = {"{namespace}": inc["namespace"], "{service}": inc["affected_service"],
            "{cloud_provider}": inc["cloud_provider"], "{revision}": deployment.get("revision")}
    return _walk(template, lambda _k, v: subs.get(v, v) if isinstance(v, str) else v)


class SemanticCache:
    def __init__(self, pool: AsyncConnectionPool, embedder: Embedder, threshold: float = 0.92):
        self.pool, self.embedder, self.threshold = pool, embedder, threshold

    @staticmethod
    def _vec(v: list[float]) -> str:
        return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

    async def lookup(self, inc: dict, key_text: str) -> dict | None:
        vec = self._vec(await self.embedder.embed(key_text))
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT id, triage, plan_template, original_cost_usd, source_incident, 1 - (embedding <=> %s::vector)"
                " FROM semantic_cache WHERE cloud = %s AND namespace = %s AND service = %s AND expires_at > now()"
                " ORDER BY embedding <=> %s::vector LIMIT 1",
                (vec, inc["cloud_provider"], inc["namespace"], inc["affected_service"], vec))).fetchone()
        if row is None or row[5] < self.threshold:
            return None
        return {"entry_id": row[0], "triage": row[1], "plan_template": row[2],
                "original_cost_usd": float(row[3]), "source_incident": row[4], "similarity": round(float(row[5]), 4)}

    async def store(self, inc: dict, key_text: str, triage: dict, plan: dict, deployment: dict,
                    original_cost_usd: float) -> int:
        vec = self._vec(await self.embedder.embed(key_text))
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "INSERT INTO semantic_cache (cloud, namespace, service, key_text, embedding, triage, plan_template,"
                " original_cost_usd, source_incident, expires_at)"
                " VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, %s, now() + %s) RETURNING id",
                (inc["cloud_provider"], inc["namespace"], inc["affected_service"], key_text, vec, Jsonb(triage),
                 Jsonb(to_template(plan, inc, deployment)), original_cost_usd, inc["incident_id"], TTL))).fetchone()
        return row[0]

    async def feedback(self, entry_id: int, resolved: bool) -> str:
        async with self.pool.connection() as conn:
            if resolved:
                await conn.execute(
                    "UPDATE semantic_cache SET hit_count = hit_count + 1,"
                    " expires_at = LEAST(created_at + %s, expires_at + (expires_at - created_at)) WHERE id = %s",
                    (MAX_TTL, entry_id))
                return "promoted"
            await conn.execute("DELETE FROM semantic_cache WHERE id = %s", (entry_id,))
            return "invalidated"


def triage_for_cache(triage: dict) -> dict:
    return json.loads(json.dumps({k: triage[k] for k in ("root_cause", "root_cause_category", "llm_confidence",
                                                          "affected_services")}))
