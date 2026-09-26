"""Hash-chained audit log client (shared by orchestrator and mcp-server).

append(): one transaction holding pg_advisory_xact_lock -> read chain tail -> seq = tail + 1 ->
hash = sha256(prev_hash || canonical_json(record)) -> insert. The record includes seq and a
client-set timestamp, so verify() can recompute every hash from the stored columns.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from cloudscale.common.canonical import canonical_json, sha256_hex

GENESIS = "0" * 64
LOCK_ID = 42


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def chain_hash(prev_hash: str, record: dict[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical_json(record))


def _record(seq, event_id, ts, incident_id, event_type, actor_type, actor_id, payload) -> dict[str, Any]:
    return {"seq": seq, "event_id": str(event_id), "ts": ts, "incident_id": incident_id, "event_type": event_type,
            "actor_type": actor_type, "actor_id": actor_id, "payload": payload}


class AuditLog:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def append(self, event_type: str, actor_type: str, actor_id: str, payload: dict[str, Any],
                     incident_id: str | None = None) -> dict[str, Any]:
        payload = canonical_roundtrip(payload)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            tail = await (await conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1")).fetchone()
            seq, prev = (tail[0] + 1, tail[1]) if tail else (1, GENESIS)
            rec = _record(seq, uuid.uuid4(), _ts(datetime.now(UTC)), incident_id, event_type, actor_type,
                          actor_id, payload)
            h = chain_hash(prev, rec)
            await conn.execute(
                "INSERT INTO audit_log (seq, event_id, ts, incident_id, event_type, actor_type, actor_id, payload,"
                " prev_hash, hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (seq, rec["event_id"], rec["ts"], incident_id, event_type, actor_type, actor_id, Jsonb(payload),
                 prev, h))
        return {**rec, "prev_hash": prev, "hash": h}

    async def list(self, incident_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q = ("SELECT seq, event_id, ts, incident_id, event_type, actor_type, actor_id, payload, prev_hash, hash "
             "FROM audit_log {} ORDER BY seq DESC LIMIT %s")
        where, params = ("WHERE incident_id = %s", (incident_id, limit)) if incident_id else ("", (limit,))
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(q.format(where), params)).fetchall()
        return [{**_record(r[0], r[1], _ts(r[2]), r[3], r[4], r[5], r[6], r[7]), "prev_hash": r[8], "hash": r[9]}
                for r in rows]

    async def verify(self) -> dict[str, Any]:
        """Recompute the whole chain. Returns the first broken seq, or ok."""
        prev, expected_seq, count = GENESIS, 1, 0
        async with self.pool.connection() as conn:
            cur = conn.cursor()
            await cur.execute("SELECT seq, event_id, ts, incident_id, event_type, actor_type, actor_id, payload,"
                              " prev_hash, hash FROM audit_log ORDER BY seq")
            async for r in cur:
                rec = _record(r[0], r[1], _ts(r[2]), r[3], r[4], r[5], r[6], r[7])
                problem = None
                if r[0] != expected_seq:
                    problem = f"gap: expected seq {expected_seq}"
                elif r[8] != prev:
                    problem = "prev_hash does not match the previous row"
                elif chain_hash(prev, rec) != r[9]:
                    problem = "row content does not match its hash"
                if problem:
                    return {"ok": False, "rows_checked": count, "first_broken_seq": r[0], "problem": problem}
                prev, expected_seq, count = r[9], r[0] + 1, count + 1
        return {"ok": True, "rows_checked": count, "head_hash": prev}


def canonical_roundtrip(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize to what JSONB will give back (e.g. tuples -> lists), so hashes verify later."""
    return json.loads(canonical_json(payload))
