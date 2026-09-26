"""Token ledger (Postgres): one row per LLM call or cache hit."""

from typing import Any

from psycopg_pool import AsyncConnectionPool


class TokenLedger:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def record(self, incident_id: str, u: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO token_usage (incident_id, agent, model, input_tokens, output_tokens, cache_read_tokens,"
                " cost_usd, avoided_cost_usd, cache_tier, failover) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (incident_id, u["agent"], u["model"], u.get("input_tokens", 0), u.get("output_tokens", 0),
                 u.get("cache_read_tokens", 0), u.get("cost_usd", 0), u.get("avoided_cost_usd", 0),
                 u.get("cache_tier"), u.get("failover")))

    async def for_incident(self, incident_id: str) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT ts, agent, model, input_tokens, output_tokens, cache_read_tokens, cost_usd, avoided_cost_usd,"
                " cache_tier, failover FROM token_usage WHERE incident_id = %s ORDER BY id", (incident_id,))).fetchall()
        keys = ("ts", "agent", "model", "input_tokens", "output_tokens", "cache_read_tokens", "cost_usd",
                "avoided_cost_usd", "cache_tier", "failover")
        return [{k: (float(v) if k.endswith("usd") else v) for k, v in zip(keys, r, strict=True)} for r in rows]

    async def summary(self, days: int = 30) -> dict[str, Any]:
        """Windowed burn rate (v1 multiplied an all-time total by 12)."""
        async with self.pool.connection() as conn:
            r = await (await conn.execute(
                "SELECT coalesce(sum(cost_usd),0), coalesce(sum(avoided_cost_usd),0), count(DISTINCT incident_id),"
                " coalesce(sum(input_tokens + output_tokens),0), count(*) FILTER (WHERE cache_tier IS NOT NULL),"
                " count(*) FILTER (WHERE agent IN ('triage','planner'))"
                " FROM token_usage WHERE ts >= now() - make_interval(days => %s)", (days,))).fetchone()
            by_model = await (await conn.execute(
                "SELECT model, count(*), coalesce(sum(cost_usd),0) FROM token_usage"
                " WHERE ts >= now() - make_interval(days => %s) GROUP BY model ORDER BY 3 DESC", (days,))).fetchall()
        cost, avoided, incidents, tokens, hits, lookups = float(r[0]), float(r[1]), r[2], r[3], r[4], r[5]
        return {"window_days": days, "cost_usd": round(cost, 6), "avoided_cost_usd": round(avoided, 6),
                "incidents": incidents, "tokens": tokens,
                "cost_per_incident_usd": round(cost / incidents, 6) if incidents else 0.0,
                "projected_annual_usd": round(cost * 365 / days, 2),
                "cache_hit_calls": hits, "llm_or_cache_calls": lookups,
                "by_model": [{"model": m, "calls": n, "cost_usd": round(float(c), 6)} for m, n, c in by_model]}
