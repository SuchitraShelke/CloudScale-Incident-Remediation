import asyncio

import psycopg
import pytest

from cloudscale.common.audit import GENESIS, AuditLog


async def test_chain_links_and_verifies(audit_pool):
    log = AuditLog(audit_pool)
    a = await log.append("INCIDENT_RECEIVED", "human", "sre1", {"scenario": "s02"}, "INC-1")
    b = await log.append("GATE_EVALUATED", "agent", "supervisor", {"gate": "AUTO", "conf": 0.88}, "INC-1")
    assert (a["seq"], a["prev_hash"]) == (1, GENESIS)
    assert (b["seq"], b["prev_hash"]) == (2, a["hash"])
    assert await log.verify() == {"ok": True, "rows_checked": 2, "head_hash": b["hash"]}


async def test_concurrent_writers_keep_one_linear_chain(audit_pool):
    log = AuditLog(audit_pool)
    await asyncio.gather(*(log.append("TOOL_CALL", "policy", "mcp-server", {"i": i}, "INC-2") for i in range(20)))
    v = await log.verify()
    assert v["ok"] and v["rows_checked"] == 20


@pytest.mark.parametrize("statement", [
    "UPDATE audit_log SET actor_id = 'someone-else' WHERE seq = 1",
    "DELETE FROM audit_log WHERE seq = 1",
    "TRUNCATE audit_log",
])
async def test_writer_role_cannot_change_history(audit_pool, statement):
    await AuditLog(audit_pool).append("HITL_DECISION", "human", "sre1", {"outcome": "APPROVED"}, "INC-3")
    async with audit_pool.connection() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(statement)


async def test_even_the_owner_is_blocked_by_the_trigger(audit_pool, clean_db):
    await AuditLog(audit_pool).append("HITL_DECISION", "human", "sre1", {"outcome": "APPROVED"}, "INC-4")
    async with await psycopg.AsyncConnection.connect(clean_db, autocommit=True) as owner:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            await owner.execute("UPDATE audit_log SET actor_id = 'forged' WHERE seq = 1")


async def test_verify_pinpoints_tampering(audit_pool, clean_db):
    log = AuditLog(audit_pool)
    for i in range(3):
        await log.append("TOOL_CALL", "policy", "mcp-server", {"i": i}, "INC-5")
    # An attacker with superuser rights disables the trigger and edits row 2.
    async with await psycopg.AsyncConnection.connect(clean_db, autocommit=True) as owner:
        await owner.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_no_modify")
        await owner.execute("""UPDATE audit_log SET payload = '{"i": 99}' WHERE seq = 2""")
        await owner.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_no_modify")
    v = await log.verify()
    assert v == {"ok": False, "rows_checked": 1, "first_broken_seq": 2, "problem": "row content does not match its hash"}


async def test_list_filters_by_incident(audit_pool):
    log = AuditLog(audit_pool)
    await log.append("A", "system", "x", {}, "INC-6")
    await log.append("B", "system", "x", {}, "INC-7")
    rows = await log.list("INC-6")
    assert [r["event_type"] for r in rows] == ["A"]
