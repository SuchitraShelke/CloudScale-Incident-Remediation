"""Incident registry (Postgres), so the list and the resume sweeper survive restarts."""

import time
from typing import Protocol

from psycopg_pool import AsyncConnectionPool


class IncidentStore(Protocol):
    async def add(self, incident_id: str, scenario_id: str) -> None: ...
    async def all(self) -> list[dict]: ...


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
                "ORDER BY created_at DESC LIMIT 200")).fetchall()
        return [{"incident_id": r[0], "scenario_id": r[1], "created_at": float(r[2])} for r in rows]


class MemoryIncidentStore:
    def __init__(self):
        self.rows: list[dict] = []

    async def add(self, incident_id: str, scenario_id: str) -> None:
        self.rows.insert(0, {"incident_id": incident_id, "scenario_id": scenario_id, "created_at": time.time()})

    async def all(self) -> list[dict]:
        return list(self.rows)
