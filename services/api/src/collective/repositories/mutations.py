import hashlib
import json
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from collective.api.errors import DomainError


async def previous_mutation(
    connection: AsyncConnection, subject: UUID, business: UUID, route: str, key: str, payload: dict
) -> dict | None:
    await connection.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"mutation:{subject}:{business}:{route}:{key}",),
    )
    row = await (
        await connection.execute(
            """select request_hash,response from collective.mutation_keys
        where principal_id=%s and business_id=%s and route=%s and key=%s""",
            (subject, business, route, key),
        )
    ).fetchone()
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if row and row["request_hash"] != digest:
        raise DomainError(
            "IDEMPOTENCY_CONFLICT", "This command key was used for a different request.", 409
        )
    return row["response"] if row else None


async def save_mutation(
    connection: AsyncConnection,
    subject: UUID,
    business: UUID,
    route: str,
    key: str,
    payload: dict,
    response: dict,
) -> None:
    await connection.execute(
        """insert into collective.mutation_keys
        (principal_id,business_id,route,key,request_hash,response) values (%s,%s,%s,%s,%s,%s)""",
        (
            subject,
            business,
            route,
            key,
            hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            Jsonb(response),
        ),
    )
