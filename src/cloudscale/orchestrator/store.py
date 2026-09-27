"""Incident registry (Postgres), so the list and the resume sweeper survive restarts."""

import json
import time
from typing import Protocol

from psycopg_pool import AsyncConnectionPool


class IncidentStore(Protocol):
    async def add(self, incident_id: str, scenario_id: str) -> None: ...
    async def all(self) -> list[dict]: ...
    async def add_resolution(self, row: dict) -> None: ...
    async def add_proposal(self, proposal: dict, source_incident: str) -> None: ...
    async def proposals(self) -> list[dict]: ...


class PgIncidentStore:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def add(self, incident_id: str, scenario_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO incidents (incident_id, scenario_id) VALUES (%s, %s)",
                               (incident_id, scenario_id))

    async def all(self) -> list[dict]:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT incident_id, scenario_id, extract(epoch FROM created_at) FROM incidents "
                "ORDER BY seq DESC LIMIT 200")).fetchall()       # insertion order: immune to clock jumps
        return [{"incident_id": r[0], "scenario_id": r[1], "created_at": float(r[2])} for r in rows]

    async def add_resolution(self, row: dict) -> None:
        await _pg_add_resolution(self.pool, row)

    async def add_proposal(self, proposal: dict, source_incident: str) -> None:
        from psycopg.types.json import Jsonb
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO runbook_proposals (proposal, source_incident) VALUES (%s, %s)",
                               (Jsonb(proposal), source_incident))

    async def proposals(self) -> list[dict]:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT proposal, status FROM runbook_proposals ORDER BY seq DESC LIMIT 50")).fetchall()
        return [{**r[0], "status": r[1]} for r in rows]


async def _pg_add_resolution(pool, row: dict) -> None:
    from psycopg.types.json import Jsonb
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO manual_resolutions (incident_id, recorded_by, how_fixed, category, verified, metrics)"
            " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (incident_id) DO UPDATE SET recorded_by = EXCLUDED.recorded_by,"
            " how_fixed = EXCLUDED.how_fixed, category = EXCLUDED.category, verified = EXCLUDED.verified,"
            " metrics = EXCLUDED.metrics, ts = now()",
            (row["incident_id"], row["recorded_by"], row["how_fixed"], row["category"], row["verified"],
             Jsonb(row["metrics"])))


class MemoryIncidentStore:
    def __init__(self):
        self.rows: list[dict] = []

    async def add(self, incident_id: str, scenario_id: str) -> None:
        self.rows.insert(0, {"incident_id": incident_id, "scenario_id": scenario_id, "created_at": time.time()})

    async def all(self) -> list[dict]:
        return list(self.rows)

    async def add_resolution(self, row: dict) -> None:
        self.resolutions = {**getattr(self, "resolutions", {}), row["incident_id"]: row}

    async def add_proposal(self, proposal: dict, source_incident: str) -> None:
        self._proposals = [{**json.loads(json.dumps(proposal)), "status": "proposed"}, *getattr(self, "_proposals", [])]

    async def proposals(self) -> list[dict]:
        return list(getattr(self, "_proposals", []))
