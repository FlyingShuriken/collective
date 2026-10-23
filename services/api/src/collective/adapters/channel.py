"""The demo channel persists only an exact owner-approved message, in the caller's transaction."""

from uuid import UUID, uuid4

from collective.adapters.replay import digest
from collective.domain.types import Instant, Revision, Snapshot


class ApprovedMessage(Snapshot):
    business_id: UUID
    conversation_id: UUID
    draft_id: UUID
    draft_revision: Revision
    recipient: str
    text_content: str
    sent_by: UUID
    approved_hash: str


def fingerprint(draft, *, revision=None):
    return digest(
        {
            "draft_id": str(draft["id"]),
            "revision": revision or draft["revision"],
            "recipient": draft["recipient"],
            "text_content": draft["text_content"],
        }
    )


class SimulatedChannel:
    async def send_approved(
        self, connection, message: ApprovedMessage, idempotency_key, as_of: Instant
    ):
        # The database trigger independently checks the exact draft and tenant/owner capability.
        expected = fingerprint(
            {
                "id": message.draft_id,
                "revision": message.draft_revision,
                "recipient": message.recipient,
                "text_content": message.text_content,
            }
        )
        if message.approved_hash != expected or not idempotency_key:
            raise ValueError("Exact approved message required")
        return await (
            await connection.execute(
                """insert into collective.messages
            (id,business_id,conversation_id,direction,text_content,draft_id,draft_revision,
             recipient,sent_by,receipt_id,created_at)
            values (%s,%s,%s,'outbound_simulated',%s,%s,%s,%s,%s,%s,%s) returning *""",
                (
                    uuid4(),
                    message.business_id,
                    message.conversation_id,
                    message.text_content,
                    message.draft_id,
                    message.draft_revision,
                    message.recipient,
                    message.sent_by,
                    uuid4(),
                    as_of,
                ),
            )
        ).fetchone()
