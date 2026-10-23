from uuid import UUID

from psycopg import AsyncConnection


async def lock_cohorts(connection: AsyncConnection, contexts: set[tuple[UUID, UUID]]) -> None:
    for supplier, spec in sorted(contexts, key=lambda pair: tuple(map(str, pair))):
        await connection.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"cohort:{supplier}:{spec}",),
        )
