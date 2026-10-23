from psycopg.types.json import Jsonb

from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event


async def insert_binding(connection, binding, correlation):
    await set_context(connection, "tenant", binding.owner_id)
    await connection.execute(
        """insert into collective.bound_approvals
        (id,business_id,need_id,owner_id,pool_id,terms,cap,expires_at,approved_at,
         evaluation_revision,quote_id,quote_revision,state)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'valid')""",
        (
            binding.id,
            binding.business_id,
            binding.terms.need_id,
            binding.owner_id,
            binding.pool_id,
            Jsonb(binding.terms.model_dump(mode="json")),
            binding.cap,
            binding.expires_at,
            binding.approved_at,
            binding.evaluation_revision,
            binding.quote_id,
            binding.quote_revision,
        ),
    )
    await append_event(
        connection,
        Event(
            business_id=binding.business_id,
            actor_id=binding.owner_id,
            actor_kind="owner",
            event_type="approval_bound",
            entity_id=binding.id,
            entity_revision=1,
            correlation_id=correlation,
            summary="Owner bound their cap and expiry to the reviewed purchasing terms.",
        ),
    )
    await set_context(connection, "coordinator", binding.owner_id)
