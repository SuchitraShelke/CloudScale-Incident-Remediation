"""Schema migration + the least-privilege audit role."""

from pathlib import Path

import psycopg
from psycopg import sql

SQL_DIR = Path(__file__).with_name("sql")


async def migrate(owner_dsn: str, audit_role: str, audit_password: str) -> None:
    async with await psycopg.AsyncConnection.connect(owner_dsn, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(7)")          # one migrator at a time
        try:
            for f in sorted(SQL_DIR.glob("*.sql")):
                await conn.execute(f.read_text())
            exists = await (await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (audit_role,))).fetchone()
            verb = sql.SQL("ALTER") if exists else sql.SQL("CREATE")
            await conn.execute(sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(
                verb, sql.Identifier(audit_role), sql.Literal(audit_password)))
            # The writer role can append and read. It can never change history.
            await conn.execute(sql.SQL("GRANT SELECT, INSERT ON audit_log TO {}").format(sql.Identifier(audit_role)))
            await conn.execute(sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {}").format(
                sql.Identifier(audit_role)))
        finally:
            await conn.execute("SELECT pg_advisory_unlock(7)")
