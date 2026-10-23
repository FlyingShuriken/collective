import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

ROLES = {
    "tenant": sql.SQL("SET LOCAL ROLE collective_tenant"),
    "coordinator": sql.SQL("SET LOCAL ROLE collective_coordinator"),
}


def require_local_database(url: str) -> str:
    settings = conninfo_to_dict(url)
    if (
        settings.get("host") not in {"127.0.0.1", "localhost", "::1"}
        or settings.get("hostaddr") not in {None, "127.0.0.1", "::1"}
        or settings.get("service") is not None
        or settings.get("port") != "54322"
        or settings.get("dbname") != "postgres"
    ):
        raise ValueError("Local setup/fixtures refuse a non-local or unexpected database")
    return url


async def set_context(
    connection: AsyncConnection,
    role: Literal["tenant", "coordinator"],
    subject: UUID | None,
) -> None:
    if role not in ROLES or (role == "tenant" and subject is None):
        raise ValueError("A fixed capability and verified tenant subject are required")
    await connection.execute(ROLES[role])
    await connection.execute(
        "select set_config('request.jwt.claims', %s, true)",
        (json.dumps({"sub": str(subject)} if subject else {}),),
    )
    await connection.execute("set local statement_timeout = '5s'")


class Database:
    def __init__(self, url: str, *, max_size: int = 5):
        self.pool = AsyncConnectionPool(
            url,
            open=False,
            min_size=1,
            max_size=max_size,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    async def open(self) -> None:
        await self.pool.open(wait=True)
        try:
            async with self.pool.connection() as connection:
                cursor = await connection.execute("""
                    select r.rolname, r.rolsuper, r.rolbypassrls, r.rolinherit,
                           exists (select 1 from pg_class c
                                   join pg_namespace n on n.oid=c.relnamespace
                                   where n.nspname='collective' and c.relowner=r.oid) as owns_tables
                    from pg_roles r where r.rolname=current_user
                """)
                role = await cursor.fetchone()
                if role["rolname"] != "collective_backend" or any(
                    role[key] for key in ("rolsuper", "rolbypassrls", "rolinherit", "owns_tables")
                ):
                    raise RuntimeError("API requires the restricted collective_backend login")
        except BaseException:
            await self.pool.close()
            raise

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def transaction(
        self,
        role: Literal["tenant", "coordinator"],
        subject: UUID | None,
    ) -> AsyncIterator[AsyncConnection]:
        async with self.pool.connection() as connection, connection.transaction():
            await set_context(connection, role, subject)
            yield connection
