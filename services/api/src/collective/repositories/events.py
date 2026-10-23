from typing import Literal
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from pydantic import Field

from collective.domain.types import Revision, Snapshot


class Event(Snapshot):
    business_id: UUID
    actor_id: UUID | None
    actor_kind: Literal["owner", "staff", "system"]
    event_type: str = Field(min_length=1, max_length=100)
    entity_id: UUID
    entity_revision: Revision | None = None
    correlation_id: UUID
    summary: str = Field(min_length=1, max_length=500)
    restricted_details_reference: UUID | None = None


async def append_event(connection: AsyncConnection, event: Event) -> UUID:
    event_id = uuid4()
    await connection.execute(
        """
        insert into collective.events
          (id, business_id, actor_id, actor_kind, event_type, entity_id, entity_revision,
           correlation_id, summary, restricted_details_reference)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """,
        (
            event_id,
            event.business_id,
            event.actor_id,
            event.actor_kind,
            event.event_type,
            event.entity_id,
            event.entity_revision,
            event.correlation_id,
            event.summary,
            event.restricted_details_reference,
        ),
    )
    return event_id
