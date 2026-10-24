from collective.adapters.channel import ApprovedMessage, SimulatedChannel, fingerprint
from collective.api.dto import DraftView, HiddenDraft, MessageReceipt
from collective.api.errors import DomainError
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services.permissions import membership


async def load(connection, business, identity, *, lock=False):
    query = "select * from collective.message_drafts where business_id=%s and id=%s"
    if lock:
        query += " for update"
    row = await (await connection.execute(query, (business, identity))).fetchone()
    if row is None and lock:
        # Sent drafts are immutable and outside the owner's UPDATE policy.
        row = await (
            await connection.execute(
                "select * from collective.message_drafts where business_id=%s and id=%s",
                (business, identity),
            )
        ).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    return row


def view(row, access):
    summary = {k: row[k] for k in ("id", "kind", "status", "revision", "created_at")}
    if not access.can_view_prices:
        return HiddenDraft(**summary)
    return DraftView(
        **summary,
        text_content=row["text_content"],
        recipient=row["recipient"],
        approved_hash=fingerprint(row),
        sent_at=row["sent_at"],
        interpretation_mode=row["input_context"].get("interpretation_mode"),
    )


def receipt(row):
    return MessageReceipt(
        **{key: row[key] for key in MessageReceipt.model_fields if key != "simulated"}
    )


async def event(connection, subject, business, row, kind):
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind="owner",
            event_type=kind,
            entity_id=row["id"],
            entity_revision=row["revision"],
            correlation_id=row["id"],
            summary="Owner sent the exact reviewed message through the simulated channel."
            if kind == "message_sent_simulated"
            else "Owner edited a supplier message draft.",
        ),
    )


async def edit(connection, subject, business, identity, body, key):
    access = await membership(connection, subject, business, owner=True)
    route, payload = f"drafts/{identity}/edit", body.model_dump(mode="json")
    previous = await previous_mutation(connection, subject, business, route, key, payload)
    if previous:
        return DraftView.model_validate(previous)
    row = await load(connection, business, identity, lock=True)
    if row["status"] != "draft":
        raise DomainError("ALREADY_SENT", "The simulated message has already been sent.", 409)
    if row["revision"] != body.expected_revision:
        raise DomainError("STALE_REVISION", "Review the latest draft before editing.", 409)
    row = await (
        await connection.execute(
            """update collective.message_drafts set text_content=%s,recipient=%s,edited_by=%s,
        revision=revision+1 where business_id=%s and id=%s returning *""",
            (body.text_content, body.recipient, subject, business, identity),
        )
    ).fetchone()
    await event(connection, subject, business, row, "draft_edited")
    result = view(row, access)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result


async def send(connection, subject, business, identity, body, key, as_of):
    await membership(connection, subject, business, owner=True)
    route, payload = f"drafts/{identity}/send", body.model_dump(mode="json")
    previous = await previous_mutation(connection, subject, business, route, key, payload)
    if previous:
        return MessageReceipt.model_validate(previous)
    row = await load(connection, business, identity, lock=True)
    existing = await (
        await connection.execute(
            "select * from collective.messages where business_id=%s and draft_id=%s",
            (business, identity),
        )
    ).fetchone()
    revision = existing["draft_revision"] if existing else row["revision"]
    if (
        body.expected_revision != revision
        or body.recipient != row["recipient"]
        or body.approved_hash != fingerprint(row, revision=revision)
    ):
        raise DomainError(
            "STALE_DRAFT", "Review the exact current text and recipient before Send.", 409
        )
    if existing:
        result = receipt(existing)
    else:
        message = ApprovedMessage(
            business_id=business,
            conversation_id=row["conversation_id"],
            draft_id=identity,
            draft_revision=revision,
            text_content=row["text_content"],
            recipient=row["recipient"],
            sent_by=subject,
            approved_hash=body.approved_hash,
        )
        saved = await SimulatedChannel().send_approved(connection, message, key, as_of)
        row = await (
            await connection.execute(
                """update collective.message_drafts set status='sent_simulated',sent_by=%s,
            sent_at=%s,revision=revision+1 where business_id=%s and id=%s returning *""",
                (subject, as_of, business, identity),
            )
        ).fetchone()
        await event(connection, subject, business, row, "message_sent_simulated")
        result = receipt(saved)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
